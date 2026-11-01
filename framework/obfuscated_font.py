"""反爬子集字体解码：把「假码点画真字」的正文还原成正常汉字。

不少站点（如 timotxt/提莫書屋）不在服务器端替换文字，而是把正文里的某些
汉字换成谚文（Hangul，U+AC00–U+D7A3）或私用区码点下发，再用一张 150 字左右
的子集字体 `@font-face` 把这些码点**画**成正确字形——浏览器里看起来完全正常，
抓下来的文本却是「大家놆놊놆」这种乱码。

本模块不依赖静态码表：下载页面引用的子集字体 → 解析 cmap 得到字体里真实存在
的码点 → 用 Pillow 把每个码点渲染成位图 → 与系统字体（微软雅黑等）里候选汉字
的字形做 IoU 匹配 → 得到 {假字: 真字} 映射，再整体替换正文。字体和映射都按
字体内容 hash 缓存，同一字体只算一次。

与 fanqie_font 的分工：那边只处理番茄的 PUA 码点 + 内置静态表；这里处理
任意码点区间的子集字体（配置驱动），供「某个源特有」的字体混淆使用，
可由源配置 `decryption.targets.*.strategy = "obfuscated_font"` 声明。
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import struct
import time
import urllib.request
from urllib.parse import urljoin, urlsplit

import numpy as np
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# 帧参数：与 fanqie_font 一致，64 全分辨率 + 16 粗筛
_SZ = 64
_CANVAS = 140
_COARSE = 16
_TOP_K = 24
_CONFIDENCE = 0.55
_THRESHOLD = 0.12

_STACK_VERSION = "v1"
_MAP_TTL = 7 * 86400.0

_SYS_FONTS = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/simkai.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]

# 默认候选字符集：CJK 统一表意文字 + 常用标点/数字
_PUNCT = "，。、：；！？（）【】《》·“”‘’…—,.:;!?()[]{}<>\"'/\\|-_=+*&%$#@0123456789"

_FONT_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.I)
_LINK_CSS_RE = re.compile(r"<link[^>]+href\s*=\s*(['\"])([^'\"]+\.css[^'\"]*)\1", re.I)
_FACE_RE = re.compile(r"@font-face\s*\{(.*?)\}", re.S | re.I)

_MAP_CACHE: dict = {}
_STACK: dict = {}
_LOCK = __import__("threading").Lock()


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def _cache_dir() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "cache", "font")
    os.makedirs(path, exist_ok=True)
    return path


def _sys_font() -> str:
    for path in _SYS_FONTS:
        if os.path.exists(path):
            return path
    return ""


def codepoints_in(text: str, ranges) -> set:
    """文本里落在 ranges（[[lo,hi],…]）内的码点集合。"""
    out = set()
    if not text:
        return out
    spans = []
    for item in ranges or []:
        try:
            lo, hi = int(item[0]), int(item[1])
        except (TypeError, IndexError, ValueError):
            continue
        spans.append((lo, hi))
    if not spans:
        return out
    for ch in text:
        code = ord(ch)
        for lo, hi in spans:
            if lo <= code <= hi:
                out.add(code)
                break
    return out


def font_urls_from_css(css: str, base_url: str = "") -> list:
    """从 CSS 文本里取 @font-face 的字体 URL（相对路径按 base_url 补全）。

    图标字体（iconfont 之类）不是正文混淆字体，直接跳过。
    """
    urls = []
    for block in _FACE_RE.findall(css or ""):
        if "src" not in block.lower():
            continue
        family = re.search(r"font-family\s*:\s*['\"]?([\w\-\s]+)", block, re.I)
        if family and re.search(r"icon|awesome|glyphicon", family.group(1), re.I):
            continue
        for _, url in _FONT_URL_RE.findall(block):
            url = url.strip().replace(r"\/", "/")
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/") and base_url:
                parts = urlsplit(base_url)
                url = "%s://%s%s" % (parts.scheme or "https", parts.netloc, url)
            elif base_url and not re.match(r"https?://", url, re.I):
                url = urljoin(base_url, url)
            if url.startswith("data:"):
                continue
            if not re.search(r"\.(?:ttf|otf|woff2?)(?:[?#]|$)", url, re.I):
                continue
            if url not in urls:
                urls.append(url)
    return urls


def stylesheet_urls_from_html(html: str) -> list:
    """页面里引用的外链 CSS（字体常常只声明在外部样式表里）。"""
    urls = []
    for _, url in _LINK_CSS_RE.findall(html or ""):
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            base = re.search(r"<link[^>]+href\s*=\s*['\"]https?://([^/'\"]+)", html or "")
            if base:
                url = "https://" + base.group(1) + url
        if url.lower().startswith("http") and url not in urls:
            urls.append(url)
    return urls


def cmap_codes(raw: bytes) -> set:
    """解析 sfnt(ttf/otf) 的 cmap，返回字体实际包含的码点（支持 format 4/12）。"""
    codes = set()
    try:
        num = struct.unpack(">H", raw[4:6])[0]
        tables = {}
        for i in range(num):
            off = 12 + i * 16
            tag = raw[off:off + 4].decode("latin-1")
            t_off, t_len = struct.unpack(">II", raw[off + 8:off + 16])
            tables[tag] = (t_off, t_len)
        if "cmap" not in tables:
            return codes
        base0 = tables["cmap"][0]
        count = struct.unpack(">H", raw[base0 + 2:base0 + 4])[0]
        for i in range(count):
            ent = base0 + 4 + i * 8
            sub = base0 + struct.unpack(">I", raw[ent + 4:ent + 8])[0]
            fmt = struct.unpack(">H", raw[sub:sub + 2])[0]
            if fmt == 4:
                segx2 = struct.unpack(">H", raw[sub + 6:sub + 8])[0]
                seg = segx2 // 2
                ends = struct.unpack(">%dH" % seg, raw[sub + 14:sub + 14 + segx2])
                starts = struct.unpack(
                    ">%dH" % seg, raw[sub + 16 + segx2:sub + 16 + segx2 * 2]
                )
                for start, end in zip(starts, ends):
                    if start == 0xFFFF:
                        continue
                    codes.update(range(start, min(end, 0xFFFE) + 1))
            elif fmt == 12:
                ngroups = struct.unpack(">I", raw[sub + 12:sub + 16])[0]
                pos = sub + 16
                for _ in range(ngroups):
                    start, end, _gid = struct.unpack(">III", raw[pos:pos + 12])
                    pos += 12
                    if end - start > 0x20000:
                        continue
                    codes.update(range(start, end + 1))
    except Exception:  # noqa: BLE001 —— 字体结构损坏就当没有 cmap
        return codes
    return codes


def font_bytes(url: str, headers: dict = None) -> bytes:
    """下载字体；woff2/woff 只有同名 ttf/otf 时优先取后者（Pillow 不能直接渲染 woff）。"""
    request_headers = {"User-Agent": "Mozilla/5.0"}
    request_headers.update({k: v for k, v in (headers or {}).items() if v})
    candidates = [url]
    if re.search(r"\.woff2?(?:[?#]|$)", url, re.I):
        candidates = [
            re.sub(r"\.woff2?(?=[?#]|$)", ".ttf", url, flags=re.I),
            re.sub(r"\.woff2?(?=[?#]|$)", ".otf", url, flags=re.I),
            url,
        ]
    last_error = None
    for candidate in candidates:
        try:
            req = urllib.request.Request(candidate, headers=request_headers)
            return urllib.request.urlopen(req, timeout=30).read()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise last_error or OSError("字体下载失败")


# ----------------------------------------------------------------------------
# 参考字形
# ----------------------------------------------------------------------------
def _mask(font, ch: str) -> np.ndarray:
    img = Image.new("L", (_CANVAS, _CANVAS), 0)
    ImageDraw.Draw(img).text((3, 1), ch, font=font, fill=255)
    arr = np.asarray(img, dtype=np.float32)
    ys, xs = np.nonzero(arr > 8)
    if len(xs) == 0:
        return np.zeros((_SZ, _SZ), dtype=np.float32)
    crop = arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    im = Image.fromarray(np.clip(crop, 0, 255).astype(np.uint8)).resize(
        (_SZ, _SZ), Image.LANCZOS
    )
    return np.asarray(im, dtype=np.float32) / 255.0


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    x = a > _THRESHOLD
    y = b > _THRESHOLD
    inter = int((x & y).sum())
    uni = int((x | y).sum())
    return inter / max(uni, 1)


def _candidates() -> list:
    chars = [chr(code) for code in range(0x4E00, 0xA000)]
    chars.extend(_PUNCT)
    return chars


def _reference():
    """系统字体对所有候选字的粗筛位图（磁盘缓存，首次约 9s，之后约 50ms）。"""
    if _STACK:
        return _STACK
    with _LOCK:
        if _STACK:
            return _STACK
        font_path = _sys_font()
        if not font_path:
            log.info("obfuscated_font: 找不到系统字体，跳过字形匹配")
            return {}
        tag = hashlib.sha1(
            ("%s|%s|%s" % (_STACK_VERSION, font_path, os.path.getsize(font_path))).encode()
        ).hexdigest()[:16]
        path = os.path.join(_cache_dir(), "ref_%s.npz" % tag)
        chars = _candidates()
        if os.path.exists(path):
            try:
                data = np.load(path, allow_pickle=False)
                if data["chars"].shape[0] == len(chars):
                    _STACK["chars"] = [str(c) for c in data["chars"]]
                    _STACK["small"] = data["small"].astype(bool)
                    return _STACK
            except Exception:  # noqa: BLE001 —— 缓存坏了就重建
                pass
        font = ImageFont.truetype(font_path, 96)
        small = np.zeros((len(chars), _COARSE, _COARSE), dtype=np.uint8)
        for idx, ch in enumerate(chars):
            img = Image.new("L", (_CANVAS, _CANVAS), 0)
            ImageDraw.Draw(img).text((3, 1), ch, font=font, fill=255)
            arr = np.asarray(img)
            ys, xs = np.nonzero(arr > 8)
            if len(xs) == 0:
                continue
            crop = arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            small[idx] = np.asarray(
                Image.fromarray(crop).resize((_COARSE, _COARSE), Image.LANCZOS)
            ) > 50
        try:
            np.savez_compressed(path, chars=np.array(chars, dtype="U1"), small=small)
        except Exception as exc:  # noqa: BLE001
            log.info("obfuscated_font: 参考位图缓存写入失败 %s", exc)
        _STACK["chars"] = chars
        _STACK["small"] = small.astype(bool)
        return _STACK


def build_map(raw: bytes, codes, confidence: float = _CONFIDENCE,
              prior=None) -> dict:
    """把字体里的码点映射成真汉字：{假字: 真字}。

    prior：同一段正文里**正常出现**的汉字集合。混淆字体常把「也/是/不」这类
    高频字换掉，而它们的字形与「乜/昰」极像（实测 也 0.51 vs 乜 0.54），
    单看 IoU 会选偏；同段文本里出现过的字显然更可能是答案，用它做同分优先。
    """
    ref = _reference()
    if not ref:
        return {}
    chars = ref["chars"]
    small_ref = ref["small"]
    font = ImageFont.truetype(io.BytesIO(raw), 96)
    present = cmap_codes(raw)
    wanted = sorted({int(code) for code in (codes or ())})
    if present:
        wanted = [code for code in wanted if code in present]
    prior_set = set(prior or ())
    common = _common_chars()
    scored_by_code = {}
    for code in wanted:
        ch = chr(code)
        try:
            mask = _mask(font, ch)
        except Exception:  # noqa: BLE001
            continue
        if not mask.any():
            continue
        binary = mask > _THRESHOLD
        small = np.asarray(
            Image.fromarray((binary.astype(np.uint8)) * 255).resize(
                (_COARSE, _COARSE), Image.LANCZOS
            )
        ) > 50
        hits = (small_ref & small).sum(axis=(1, 2))
        denom = small_ref.sum(axis=(1, 2)) + int(small.sum())
        top = np.argsort(hits / np.maximum(denom, 1))[-_TOP_K:][::-1]
        scored = [(_iou(mask, _ref_mask(chars[int(idx)])), chars[int(idx)])
                  for idx in top]
        scored.sort(key=lambda item: item[0], reverse=True)
        scored_by_code[ch] = scored[:8]
    # 子集字体是「150 个假字 ↔ 150 个真字」的一一映射：全局按分数降序贪心分配，
    # 一个真字只认领一次。逐字取 top-1 会出现两个字映射到同一个字（实测 也/乜、
    # 五/丑 互抢），也会把高频字让给字形更像的生僻字。
    pool = []
    for ch, scored in scored_by_code.items():
        # 子集字体只有一两百个字，答案几乎必定落在常用字（简/繁）或本段正文里；
        # 把候选限制在这个范围内，能挡掉「扡/昰/孒」这类字形更像的生僻字。
        allowed = [(score, candidate) for score, candidate in scored
                   if candidate in prior_set or candidate in common]
        for score, candidate in (allowed or scored):
            bonus = 0.06 if candidate in prior_set else 0.0
            bonus += 0.03 if candidate in common else 0.0
            pool.append((score + bonus, score, ch, candidate))
    pool.sort(key=lambda item: item[0], reverse=True)
    mapping = {}
    claimed = {}
    for _, score, ch, candidate in pool:
        if ch in mapping or candidate in claimed:
            continue
        floor = confidence - (0.12 if candidate in prior_set else 0.0)
        if score < max(0.42, floor):
            continue
        mapping[ch] = candidate
        claimed[candidate] = ch
    # 二轮补齐：一一映射里抢输的码点不能留空，否则正文里那几个字仍是谚文。
    # 放宽到「未占用的候选、分数 ≥ 0.40」；实在没有就用它自己的最优候选（允许
    # 与别的码点同字——重复一次真字，总比留一个看不懂的假字强）。
    for ch, scored in scored_by_code.items():
        if ch in mapping or not scored:
            continue
        pick = next(((score, cand) for score, cand in scored
                     if cand not in claimed and cand in common and score >= 0.40), None)
        if pick is None:
            # 宁可和别的码点撞同一个常用字（重复一个真字只影响个别字），
            # 也不要让生僻字（扡/昰/孒）混进正文，「滴在扡上」这种就是它。
            pick = next(((score, cand) for score, cand in scored
                         if cand in common and score >= 0.40), None)
        if pick is None:
            pick = next(((score, cand) for score, cand in scored
                         if cand not in claimed and score >= 0.40), None)
        if pick is None:
            pick = next(((score, cand) for score, cand in scored if score >= 0.40), None)
        if pick is not None:
            mapping[ch] = pick[1]
            claimed.setdefault(pick[1], ch)
    return mapping


_FONT_REF = {}
_REF_MASKS: dict = {}
_COMMON: dict = {}
_FONT_HINTS: dict = {}


def hint_fonts(key: str) -> list:
    """某源上次成功解码时用到的字体地址（缓存/离线正文没 html 时复用）。"""
    return list(_FONT_HINTS.get(str(key or "")) or [])


def _common_chars() -> set:
    """常用字集合（GB2312 简繁两套）：字形近似时用来压过生僻字。

    「地 0.555 vs 扡 0.620」这类只差 0.06 的候选里，生僻字经常赢（扡 与 地
    共享「也」部件，笔画更像；子集字体是另一款字模，跨字体 IoU 本身有噪声）。
    常用字集合（含繁体，站是繁體）能把这部分噪声压回去。
    """
    if _COMMON:
        return _COMMON["set"]
    chars = set()
    try:
        from .fanqie_font import _gb2312_common

        simplified = "".join(_gb2312_common())
        chars.update(simplified)
        try:
            from . import zh_convert

            chars.update(zh_convert.convert(simplified, "zh-tw"))
        except Exception:  # noqa: BLE001 —— 繁简转换不可用就只用简体表
            pass
    except Exception:  # noqa: BLE001
        pass
    _COMMON["set"] = chars
    return chars


def font_ref_global():
    """共享的系统字体句柄（用于精算阶段按需渲染少量候选）。"""
    if "font" not in _FONT_REF:
        path = _sys_font()
        _FONT_REF["font"] = ImageFont.truetype(path, 96) if path else None
    return _FONT_REF["font"]


def _ref_mask(ch: str) -> np.ndarray:
    """参考字形按字缓存：20k 字全渲染太慢，只渲染粗筛出的 top-k。"""
    mask = _REF_MASKS.get(ch)
    if mask is None:
        font = font_ref_global()
        mask = _mask(font, ch) if font else np.zeros((_SZ, _SZ), dtype=np.float32)
        if len(_REF_MASKS) > 6000:  # 防止长跑进程无限增长
            _REF_MASKS.clear()
        _REF_MASKS[ch] = mask
    return mask


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def _load_cached(key: str):
    cached = _MAP_CACHE.get(key)
    if cached and cached[0] > time.time():
        return cached[1]
    path = os.path.join(_cache_dir(), "map_%s.json" % key)
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < _MAP_TTL:
            with open(path, "r", encoding="utf-8") as handle:
                mapping = json.load(handle)
            if isinstance(mapping, dict) and mapping:
                _MAP_CACHE[key] = (os.path.getmtime(path) + _MAP_TTL, mapping)
                return mapping
    except Exception:  # noqa: BLE001
        pass
    return {}


def _store_cached(key: str, mapping: dict):
    _MAP_CACHE[key] = (time.time() + _MAP_TTL, mapping)
    try:
        path = os.path.join(_cache_dir(), "map_%s.json" % key)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(mapping, handle, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        log.info("obfuscated_font: 映射缓存写入失败 %s", exc)


def decode(text: str, cfg: dict, headers: dict = None) -> tuple:
    """按配置解码文本，返回 (新文本, 映射表)。cfg 见模块头注释。"""
    ranges = cfg.get("codepoint_ranges") or [[0xAC00, 0xD7A3], [0xE000, 0xF8FF]]
    needed = codepoints_in(text, ranges)
    if not needed:
        return text, {}
    confidence = float(cfg.get("confidence") or _CONFIDENCE)
    # 本段正文里正常出现的汉字：给「字形近似」的候选做同分优先（也 vs 乜）
    prior = {ch for ch in text if "\u4e00" <= ch <= "\u9fff"}

    urls = list(cfg.get("font_urls") or [])
    html = cfg.get("html") or ""
    if not urls and html:
        urls = font_urls_from_css(html)
        if not urls:
            for css_url in stylesheet_urls_from_html(html):
                try:
                    req = urllib.request.Request(
                        css_url, headers={"User-Agent": "Mozilla/5.0",
                                          **(headers or {})}
                    )
                    css = urllib.request.urlopen(req, timeout=20).read().decode(
                        "utf-8", "ignore"
                    )
                except Exception as exc:  # noqa: BLE001
                    log.info("obfuscated_font: CSS 拉取失败 %s (%s)", css_url, exc)
                    continue
                urls.extend(font_urls_from_css(css, base_url=css_url))
    for css_url in cfg.get("font_css") or []:
        try:
            req = urllib.request.Request(
                css_url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})}
            )
            css = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            log.info("obfuscated_font: CSS 拉取失败 %s (%s)", css_url, exc)
            continue
        urls.extend(font_urls_from_css(css, base_url=css_url))
    seen = []
    for url in urls:
        if url not in seen:
            seen.append(url)
    hint = cfg.get("hint_key")
    if hint and seen:
        # 记住「这个源用哪些字体」：之后命中缓存/离线文本时没有 html 也能解
        _FONT_HINTS[str(hint)] = list(seen)

    mapping = {}
    for url in seen:
        have = {ord(k) for k in mapping}
        if needed.issubset(have):
            break
        try:
            raw = font_bytes(url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            log.info("obfuscated_font: 字体下载失败 %s (%s)", url, exc)
            continue
        key = hashlib.sha1(raw).hexdigest()[:16]
        cached = dict(_load_cached(key) or {})
        # 字体按内容缓存：子集字体只有 150 个字形，首次就把整个字体一次解完
        # （一一映射才有一致性）；之后新章节只补算缓存里还缺的码点。
        if not cached:
            whole = cmap_codes(raw) or needed
            cached = build_map(raw, whole, confidence=confidence, prior=prior)
            if cached:
                _store_cached(key, cached)
        missing = needed.difference({ord(k) for k in cached} | have)
        if missing:
            fresh = build_map(raw, missing, confidence=confidence, prior=prior)
            if fresh:
                cached.update(fresh)
                _store_cached(key, cached)
        for fake, real in cached.items():
            mapping.setdefault(fake, real)
    if not mapping:
        log.info("obfuscated_font: 未解析出映射（%d 个待解码点）", len(needed))
        return text, {}
    out = "".join(mapping.get(ch, ch) for ch in text)
    left = sorted(needed.difference({ord(k) for k in mapping}))
    log.info("obfuscated_font: 解出 %d 个字（剩余 %d 个码点未命中%s）",
             len(mapping), len(left),
             ("：" + ",".join("U+%04X" % code for code in left[:8])) if left else "")
    return out, mapping
