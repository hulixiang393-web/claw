"""番茄混淆字体动态解析：PUA 码点 → 汉字。

番茄正文/标题中的文本被字体混淆：正文以 PUA 私用区码点（U+E000–U+F8FF）
下发，页面用一张专用的 woff2 子集字体把这些码点绘制成正常汉字。该字体的
hash 每本书/章节不同，任何静态映射表都会过期（framework.data.fanqie_glyph_map
即因此解出乱码）。

原理：字体的 glyph 轮廓即目标的汉字。本模块下载页面 @font-face 引用的字体，
用 Pillow 光栅化每个 PUA 码点的字形，与系统字体（微软雅黑等）逐字做 IoU
模板匹配，还原映射表，按字体 URL 全局缓存。
"""

import io
import logging
import os
import re
import time
import urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

_CACHE_TTL = 6 * 3600
_CACHE: dict = {}  # font_url -> (expire_ts, map: dict[str, str])
_CONFIDENCE = 0.66  # IoU 阈值，低于则不采纳该码点映射
_SZ = 64
_CANVAS = 140
_COARSE = 16  # 粗筛网格

_CACHE_VERSION = "v3:"  # 匹配算法版本，升级后旧表失效

_SYS_FONTS = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/simkai.ttf",
]

_FONT_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.I)

_PUNCT_DIGITS = list("，。：！？、；【】（）·“”‘’…—`~!@#$%^&*-_=+<>,.?/\\|\"'（）《》　0123456789%")


def _sys_font() -> str:
    for p in _SYS_FONTS:
        if os.path.exists(p):
            return p
    return ""


def extract_font_urls(html: str) -> list:
    """从 HTML 提取 @font-face src url 列表（woff/woff2，绝对化）。"""
    urls = []
    for m in _FONT_RE.findall(html or ""):
        u = m[1].replace(r"\u002F", "/").replace(r"\/", "/")
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("data:") or not re.search(
            r"\.(?:woff2?|ttf|otf)(?:[?#]|$)", u, re.I
        ):
            continue
        if re.search(r"[\-\s]?\d{3}(?:px)|\b(?:italic|bold|oblique)\b", u):
            continue
        if u not in urls:
            urls.append(u)
    return urls


def _gb2312_common() -> list:
    out = []
    for b1 in range(0xA1, 0xF8):
        b2max = 0xFE if b1 != 0xA1 and b1 != 0xF7 else 0xF7
        if b1 == 0xF7:
            b2max = 0xF9
        for b2 in range(0xA1, b2max + 1):
            try:
                ch = bytes([b1, b2]).decode("gb2312")
            except Exception:
                continue
            if "\u4e00" <= ch <= "\u9fff" or ch in _PUNCT_DIGITS:
                out.append(ch)
    return out


_candidates = _gb2312_common() + _PUNCT_DIGITS


def _mask(font, ch, size=_SZ, canvas=_CANVAS):
    img = Image.new("L", (canvas, canvas), 0)
    d = ImageDraw.Draw(img)
    d.text((3, 1), ch, font=font, fill=255)
    arr = np.asarray(img, dtype=np.float32)
    ys, xs = np.nonzero(arr > 8)
    if len(xs) == 0:
        return np.zeros((size, size), dtype=np.float32)
    crop = arr[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1]
    im = Image.fromarray(np.clip(crop * 255, 0, 255).astype(np.uint8)).resize(
        (size, size), Image.LANCZOS
    )
    return np.asarray(im, dtype=np.float32) / 255.0


def _iou(a, b, th=0.12):
    x = a > th
    y = b > th
    inter = int((x & y).sum())
    uni = int((x | y).sum())
    return inter / max(uni, 1)


_std_masks_cache = []
_STD_KEYS: dict = {}


def _std_masks():
    if _STD_KEYS:
        return _STD_KEYS
    font_path = _sys_font()
    if not font_path:
        return {}
    font = ImageFont.truetype(font_path, 96)
    masks = {c: _mask(font, c) for c in _candidates}
    _STD_KEYS["__chars__"] = list(masks.keys())
    _STD_KEYS["__stack__"] = np.stack(list(masks.values())) > 0.12
    _STD_KEYS["__small__"] = np.stack(
        [
            Image.fromarray((m > 0.12).astype(np.uint8) * 255).resize(
                (_COARSE, _COARSE), Image.LANCZOS
            )
            for m in masks.values()
        ]
    ) > 50
    return _STD_KEYS


def build_map(font_url: str, needed_codes=None, headers=None) -> dict:
    """下载字体并逐码点匹配，返回 {PUA字符: 汉字}（低置信不采）。

    匹配策略：全部候选先做 _COARSE 网格粗筛取 top-k，再精算全分辨率
    IoU，避免对数千候选逐个全分辨率求交（提速数十倍）。
    """
    now = time.time()
    requested = tuple(sorted({int(code) for code in (needed_codes or ())}))
    cache_key = _CACHE_VERSION + font_url + ":" + ",".join(map(str, requested))
    cached = _CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]
    std = _std_masks()
    if not std:
        return {}
    chars = std["__chars__"]
    B = std["__stack__"]  # (N,64,64) bool
    S = std["__small__"]  # (N,16,16) bool

    request_headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://fanqienovel.com/",
    }
    request_headers.update({k: v for k, v in (headers or {}).items() if v})
    req = urllib.request.Request(font_url, headers=request_headers)
    raw = urllib.request.urlopen(req, timeout=25).read()
    glyph_codes = None
    try:
        from fontTools.ttLib import TTFont

        cmap = TTFont(io.BytesIO(raw), lazy=False).getBestCmap() or {}
        glyph_codes = [
            code for code in cmap
            if 0xE000 <= code <= 0xF8FF
        ]
    except Exception:
        pass
    font = ImageFont.truetype(io.BytesIO(raw), 96)
    mapping = {}
    if requested:
        wanted = set(requested)
        glyph_codes = [code for code in (glyph_codes or wanted) if code in wanted]
    for code in glyph_codes or range(0xE000, 0xF900):
        ch = chr(code)
        try:
            a = _mask(font, ch)
        except Exception:
            continue
        if not a.any():
            continue
        am = a > 0.12
        small = np.array(
            Image.fromarray((am.astype(np.uint8)) * 255).resize(
                (_COARSE, _COARSE), Image.LANCZOS
            )
        ) > 50
        # 粗筛：按「交集/并集」粗分排序（分母以 u 网计数近似）
        hits = (S & small).sum(axis=(1, 2))
        denom = S.sum(axis=(1, 2)) + int(small.sum())
        topk = np.argsort(hits / np.maximum(denom, 1))[-24:][::-1]
        # 精算 top-k 全分辨率 IoU；不要为所有候选字形计算全尺寸交集。
        top_masks = B[topk]
        inter = (top_masks & am).sum(axis=(1, 2))
        uni = top_masks.sum(axis=(1, 2)) + int(am.sum()) - inter
        score = inter / np.maximum(uni, 1)
        best_s, best_c = 0.0, ""
        for idx, s in zip(topk, score):
            s = float(s)
            if s > best_s:
                best_s, best_c = s, chars[idx]
        if best_s >= _CONFIDENCE:
            mapping[ch] = best_c
    _CACHE[cache_key] = (now + _CACHE_TTL, mapping)
    log.info(
        "fanqie_font: %s -> %d glyphs (%.1fs)",
        font_url.rsplit("/", 1)[-1],
        len(mapping),
        time.time() - now,
    )
    return mapping


def _decode_text(text: str, mapping: dict) -> str:
    out = []
    for c in text:
        out.append(mapping.get(c, c))
    return "".join(out)


def resolve_html(html: str, text: str, headers=None):
    """用页面字体解析密文文本。无字体/全部低置信 → 返回 (None, mapping)。

    返回 (resolved_text, mapping_by_font)。调用方按需采纳或走 fallback。
    多字体时尝试全部，取命中率最高的结果（>0 则视为成功）。
    """
    if not text:
        return None, {}
    pua_total = sum(1 for c in text if 0xE000 <= ord(c) <= 0xF8FF)
    if pua_total == 0:
        return text, {}
    return resolve_urls(extract_font_urls(html), text, headers=headers)


def resolve_urls(urls: list, text: str, headers=None):
    """Resolve text with already captured @font-face URLs."""
    if not text:
        return None, {}
    pua_total = sum(1 for c in text if 0xE000 <= ord(c) <= 0xF8FF)
    if pua_total == 0:
        return text, {}
    urls = [str(u).replace(r"\u002F", "/").replace(r"\/", "/") for u in (urls or [])]
    if not urls:
        return None, {}
    needed_codes = {
        ord(c) for c in text if 0xE000 <= ord(c) <= 0xF8FF
    }
    combined_map = {}
    for u in urls:
        try:
            # 只匹配当前正文实际出现的 PUA 码位；旧实现会扫描字体中的
            # 全部私用区字形，首次打开一章可能耗时几十秒。
            remaining_codes = needed_codes.difference(combined_map)
            if not remaining_codes:
                break
            mapping = build_map(u, remaining_codes, headers=headers)
        except Exception as exc:
            log.info("fanqie_font: 字体加载失败 %s (%s)", u, exc)
            continue
        for code, char in mapping.items():
            combined_map.setdefault(code, char)
        if sum(1 for c in text if 0xE000 <= ord(c) <= 0xF8FF and c in combined_map) == pua_total:
            break
    if combined_map:
        return _decode_text(text, combined_map), combined_map
    return None, {}
