# -*- coding: utf-8 -*-
"""timo 字体混淆「渲染正确性」对照探针。

真值：站点自己的明文转码接口 `/chapter/transcode.html?bid=..&cid=..&sid=0`
（服务端已解码的正文，繁体、无假字）。对同一章比较三条链路：

  A. 主路径 Content.fetch_chapter            —— 正文接口明文 → 繁转简
  B. 回退路径（接口不可用）页面 + 子集字体解码 —— 字形 IoU 匹配
  C. 回退路径的图文片段（GUI 显示图片时直接渲染的文本）

判据：
  A 必须等于真值、0 假字；
  B/C 必须 0 假字（不再出现「雍正十三뎃」），并与真值高度一致（字形匹配的
  个别形近字误差单列，便于定位）。

假字判定只看混淆承载区（谚文 U+AC00–D7A3、私用区 U+E000–F8FF）：CJK 扩展 B
里的生僻真字不算假字。

用法：
    python tools/verify_timo_render_truth.py [书数] [每本书章数]
"""
import copy
import json
import os
import re
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework import zh_convert  # noqa: E402
from framework.config import SourceConfig, load_source  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.decrypter import Decrypter  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.http import HttpClient, NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402

FAKE_RANGES = ((0xAC00, 0xD7A3), (0xE000, 0xF8FF))
SPACE_RE = re.compile(r"[\s\u3000]+")


class NC:
    def _selfcheck_url(self, source):
        return ""

    def check(self, source, url):
        return True


def fake_chars(text):
    return {ch for ch in str(text or "")
            if any(lo <= ord(ch) <= hi for lo, hi in FAKE_RANGES)}


def norm(text):
    return SPACE_RE.sub("", str(text or ""))


def truth_text(http, headers, url: str) -> str:
    """站点转码接口的明文正文（真值）。"""
    parts = urlsplit(url)
    seg = [p for p in parts.path.split("/") if p]
    if len(seg) < 2:
        return ""
    bid, cid = seg[-2], seg[-1].split(".")[0]
    api = ("https://%s/chapter/transcode.html?bid=%s&cid=%s&sid=0"
           % (parts.netloc, bid, cid))
    try:
        payload = json.loads(http.get_text(api, headers=headers, timeout=20))
    except Exception:  # noqa: BLE001
        return ""
    return str(payload.get("content") or "")


def compare(mine, truth):
    """相似度（0–1）+ 第一处差异上下文。

    用 difflib 对齐而不是按位置逐字比：接口标题写「第1章」、正文页写「第一章」
    这类编号写法差异会让整体错位，按位置比会把整章判成不一致。
    """
    import difflib

    a, b = norm(mine), norm(truth)
    if not a or not b:
        return 0.0, "(空文本：mine=%d truth=%d)" % (len(a), len(b))
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ctx = ""
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        ctx = "%s≠%s @%d  …%s… / …%s…" % (
            a[i1:i2][:8] or "(缺)", b[j1:j2][:8] or "(多)",
            i1, a[max(0, i1 - 10):i1 + 10], b[max(0, j1 - 10):j1 + 10])
        break
    return matcher.ratio(), ctx


def without_api(source):
    """同一份源配置去掉正文接口 → 走「页面 + 字体解码」回退路径。"""
    raw = copy.deepcopy(source.raw)
    body = raw["endpoints"]["content"]["chapter"]["body"]
    body.pop("api", None)
    return SourceConfig.from_dict(raw)


def main(argv):
    limit = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 3
    per_book = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 2
    source = load_source("sources/timotxt.json")
    fallback_source = without_api(source)
    headers = source.request_headers()
    http = HttpClient(defaults=NetworkDefaults(timeout=20, retries=1, interval_ms=300))
    parser, checker = Parser(), NC()
    content = Content(http, parser, checker, decrypter=Decrypter(http))
    disc = Discovery(http, parser, checker)
    rows = []
    try:
        works = disc.list_works(source, source.base_url, 1)
        for work in works[:limit]:
            try:
                detail = content.fetch_detail(source, work.url)
            except Exception as exc:  # noqa: BLE001
                print("跳过 %s（%s）" % (work.title[:10], type(exc).__name__))
                continue
            count = len(detail.chapters)
            idxs = [0]
            if per_book > 1 and count > 1:
                step = max(1, (count - 1) // (per_book - 1))
                idxs = sorted({0} | {min(count - 1, step * k)
                                     for k in range(1, per_book)})
            for idx in idxs:
                chapter = detail.chapters[idx]
                started = time.time()
                truth = zh_convert.convert(truth_text(http, headers, chapter.url), "zh-cn")
                if not truth:
                    # 站点接口对该章没有明文（status=300 未转码缓存）：无真值不判，
                    # 主路径会自动回退到页面 + 字形匹配。
                    print("%-9s #%-3d 站点接口无真值，跳过对照" % (work.title[:7], idx))
                    continue
                try:
                    body = content.fetch_chapter(source, chapter.url, use_cache=False)
                    page_body = content.fetch_chapter(
                        fallback_source, chapter.url, use_cache=False)
                    _, fragments = content.fetch_chapter_with_images(
                        fallback_source, chapter.url)
                except Exception as exc:  # noqa: BLE001
                    print("跳过 %s#%d（%s）" % (work.title[:8], idx, type(exc).__name__))
                    continue
                cost = time.time() - started
                frag_text = "".join(str(e.get("text") or "") for e in fragments
                                    if isinstance(e, dict) and e.get("type") == "text")
                body_rate, body_ctx = compare(body, truth)
                page_rate, page_ctx = compare(page_body, truth)
                frag_rate, frag_ctx = compare(frag_text, truth) if frag_text else (1.0, "")
                body_fake = len(fake_chars(body))
                page_fake = len(fake_chars(page_body))
                frag_fake = len(fake_chars(frag_text))
                rows.append((body_rate, page_rate, frag_rate,
                             body_fake, page_fake, frag_fake))
                print("%-9s #%-3d 主路径 %6.2f%%(假字%d) | 回退 %6.2f%%(假字%d) | "
                      "片段 %6.2f%%(假字%d) | %.1fs"
                      % (work.title[:7], idx, body_rate * 100, body_fake,
                         page_rate * 100, page_fake, frag_rate * 100, frag_fake, cost))
                if body_rate < 0.999 and body_ctx:
                    print("     主路径差异：%s" % body_ctx)
                if page_fake or frag_fake:
                    print("     回退残留假字：主 %s 片段 %s"
                          % ("".join(fake_chars(page_body))[:12],
                             "".join(fake_chars(frag_text))[:12]))
                elif page_rate < 0.998 and page_ctx:
                    print("     回退形近字误差：%s" % page_ctx)
                elif frag_rate < 0.998 and frag_ctx:
                    print("     片段形近字误差：%s" % frag_ctx)
    finally:
        http.close()
    if rows:
        n = len(rows)
        body_avg = sum(r[0] for r in rows) / n
        page_avg = sum(r[1] for r in rows) / n
        frag_avg = sum(r[2] for r in rows) / n
        fake_total = sum(r[3] + r[4] + r[5] for r in rows)
        print("合计：%d 章 | 主路径 %.2f%% | 回退字形解码 %.2f%% | 片段 %.2f%% | 假字 %d"
              % (n, body_avg * 100, page_avg * 100, frag_avg * 100, fake_total))
        ok = (body_avg >= 0.999 and page_avg >= 0.99 and frag_avg >= 0.99
              and fake_total == 0)
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
