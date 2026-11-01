# -*- coding: utf-8 -*-
"""timo 字体混淆「视觉对照表」：站点子集字体画的字 vs 我们解出来的字。

字体混淆只改字形不改码点，所以任何「文本层面」的比较都无法证明解对了。
这里直接取站点子集字体，用 @font-face 把假码点画出来，和我们解码出的真字
并排截屏——图里左边是站点真实显示的字形，右边是我们的答案。

用法：
    python tools/probe_timo_font_sheet.py [书序号] [章序号] [字数]
输出：
    data/tmp/timo_font_sheet.png
"""
import base64
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import framework.obfuscated_font as of  # noqa: E402
from framework.config import load_source  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.decrypter import Decrypter  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.http import HttpClient, NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402

FAKE_RANGES = ((0xAC00, 0xD7A3), (0xE000, 0xF8FF))
OUT = os.path.join("data", "tmp", "timo_font_sheet.png")


class NC:
    def _selfcheck_url(self, source):
        return ""

    def check(self, source, url):
        return True


def fake_chars(text):
    return [ch for ch in text if any(lo <= ord(ch) <= hi for lo, hi in FAKE_RANGES)]


def main(argv):
    # 用法：probe_timo_font_sheet.py [章节URL] 或 [书序号] [章序号] [字数]
    url_arg = argv[1] if len(argv) > 1 and argv[1].startswith("http") else ""
    book_idx = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 0
    chap_idx = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 0
    limit = int(argv[3]) if len(argv) > 3 and argv[3].isdigit() else 12

    source = load_source("sources/timotxt.json")
    headers = source.request_headers()
    http = HttpClient(defaults=NetworkDefaults(timeout=20, retries=1, interval_ms=300))
    parser = Parser()
    content = Content(http, parser, NC(), decrypter=Decrypter(http))
    disc = Discovery(http, parser, NC())
    try:
        works = disc.list_works(source, source.base_url, 1)
        if url_arg:
            chapter_url = url_arg
            detail = content.fetch_detail(
                source, chapter_url.rsplit("/", 1)[0] + "/dir")
            chapter = next((ch for ch in detail.chapters if ch.url == chapter_url),
                           detail.chapters[0])
        else:
            detail = content.fetch_detail(source, works[book_idx].url)
            chapter = detail.chapters[chap_idx]
        chapter_url = chapter.url
        page = http.get_text(chapter.url, headers=headers, timeout=20)
        # 取**未解码**的原始段落（_fetch_chapter_page 返回的是解码后文本）
        doc = parser.parse(page)
        selector = (source.raw["endpoints"]["content"]["chapter"]["body"]
                    ["selector"])
        raw = "\n".join(parser.extract(doc, content._with_keep_breaks(selector)))
        decoded = content._decode_body_text(source, chapter.url, raw, page)
        print("原文假字 %d 个（共 %d 字）" % (len(fake_chars(raw)), len(raw)))
        # 字体：取站点的子集字体（read.38.css 里的 @font-face）
        css_url = (source.raw["decryption"]["targets"]["content"]["steps"][0]
                   .get("font_css") or ["https://i1.timotxt.com/min/read.38.css"])[0]
        import urllib.request
        css = urllib.request.urlopen(
            urllib.request.Request(css_url, headers={"User-Agent": "Mozilla/5.0",
                                                    **headers}), timeout=20
        ).read().decode("utf-8", "ignore")
        urls = of.font_urls_from_css(css, base_url=css_url)
        if not urls:
            print("未从 CSS 解析出字体 URL")
            return 2
        raw_font = of.font_bytes(urls[0], headers=headers)
        b64 = base64.b64encode(raw_font).decode("ascii")
        fmt = "woff2" if urls[0].lower().endswith(".woff2") else "truetype"
        print("字体 %s（%d 字节，%s）" % (urls[0], len(raw_font), fmt))

        rows = []
        seen = set()
        idx = 0
        while idx < len(raw) and len(rows) < limit:
            if raw[idx] in fake_chars(raw) and raw[idx] not in seen:
                seen.add(raw[idx])
                ours = decoded[idx] if idx < len(decoded) else "?"
                lo, hi = max(0, idx - 6), min(len(raw), idx + 7)
                rows.append((raw[idx], ours, raw[lo:hi], decoded[lo:idx]
                             + "[" + ours + "]" + decoded[idx + 1:hi]))
            idx += 1

        cells = "".join(
            '<div class="cell"><div class="big">%s</div>'
            '<div class="ans">%s</div><div class="cxt">%s</div>'
            '<div class="cid">U+%04X</div></div>'
            % (fake, ours, ctx_ours.replace("&", "&amp;").replace("<", "&lt;"),
               ord(fake))
            for fake, ours, _ctx_raw, ctx_ours in rows
        )
        html = """<html><head><meta charset="utf-8"><style>
@font-face { font-family: timo; src: url(data:font/%s;base64,%s) format('%s'); }
body { background:#fff; margin:0; padding:16px; font-family:"Microsoft YaHei",sans-serif; }
.grid { display:flex; flex-wrap:wrap; gap:14px; }
.cell { width:150px; border:1px solid #ddd; border-radius:6px; padding:8px; text-align:center; }
.big { font-family: timo; font-size:72px; line-height:1.1; color:#000; }
.ans { font-size:26px; margin-top:4px; color:#0a0; }
.cxt { font-size:11px; color:#666; margin-top:4px; word-break:break-all; }
.cid { font-size:10px; color:#999; }
</style></head><body><h3>左大字号 = 站点子集字体画出的字形；绿色 = 我们解出的字</h3>
<div class="grid">%s</div></body></html>""" % (fmt, b64, fmt, cells)

        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel="chrome")
            try:
                page_obj = browser.new_context(viewport={"width": 1100, "height": 900}).new_page()
                page_obj.set_content(html, wait_until="load")
                page_obj.evaluate("() => document.fonts.ready.then(() => true)")
                page_obj.wait_for_timeout(400)
                page_obj.screenshot(path=OUT, full_page=True)
            finally:
                browser.close()
        print("章节 %s" % chapter.url)
        for fake, ours, _raw_ctx, ctx_ours in rows:
            print("  U+%04X %s → 我们解作 %s ｜ %s" % (ord(fake), fake, ours, ctx_ours))
        print("截图：%s" % os.path.abspath(OUT))
        return 0
    finally:
        http.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
