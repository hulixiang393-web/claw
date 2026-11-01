# -*- coding: utf-8 -*-
"""验证 timotxt 字体混淆解码：真实章节页原文 → 解码后是否还有假字。

用法：
    python tools/verify_timo_font.py [书数]
输出每本书第一章的「原文假字 → 解码后假字」、耗时与解码后正文样本；
合计行给出总量对比，假字归零即视为该源字体混淆已解决。
"""
import html as html_lib
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import framework.obfuscated_font as of  # noqa: E402
from framework.config import load_source  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.decrypter import Decrypter  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.http import HttpClient, NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402

BODY_RE = re.compile(r'<div class="content">(.*?)</div>\s*<div', re.S)
HANGUL = ((0xAC00, 0xD7A3),)


class NC:
    def _selfcheck_url(self, source):
        return ""

    def check(self, source, url):
        return True


def raw_body(page: str) -> str:
    match = BODY_RE.search(page or "")
    if not match:
        return ""
    inner = re.sub(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", "",
                   match.group(1), flags=re.S)
    inner = re.sub(r"<br\s*/?>|</p>", "\n", inner, flags=re.I)
    text = re.sub(r"<[^>]+>", "", inner)
    return html_lib.unescape(text).strip()


def main(argv) -> int:
    limit = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 8
    source = load_source("sources/timotxt.json")
    targets = (source.raw.get("decryption") or {}).get("targets") or {}
    step = next(
        (item for item in ((targets.get("content") or {}).get("steps") or [])
         if item.get("strategy") == "obfuscated_font"),
        {"codepoint_ranges": [list(r) for r in HANGUL],
         "font_css": ["https://i1.timotxt.com/min/read.38.css"]},
    )
    http = HttpClient(defaults=NetworkDefaults(timeout=15, retries=1, interval_ms=300))
    parser, checker = Parser(), NC()
    content = Content(http, parser, checker, decrypter=Decrypter(http))
    disc = Discovery(http, parser, checker)
    stats = []
    try:
        works = disc.list_works(source, source.base_url, 1)
        for work in works[:limit]:
            try:
                detail = content.fetch_detail(source, work.url)
                page = http.get_text(detail.chapters[0].url,
                                     headers=source.request_headers(), timeout=15)
            except Exception as exc:  # noqa: BLE001
                print("跳过 %s（%s）" % (work.title[:10], type(exc).__name__))
                continue
            body = raw_body(page)
            if not body:
                continue
            before = sum(1 for ch in body if 0xAC00 <= ord(ch) <= 0xD7A3)
            started = time.time()
            decoded, mapping = of.decode(body, dict(step),
                                         headers=source.request_headers())
            cost = time.time() - started
            after = sum(1 for ch in decoded if 0xAC00 <= ord(ch) <= 0xD7A3)
            stats.append((before, after))
            print("%-12s 原文假字 %-4d → 解码后 %-3d | 命中 %d 字 | %.1fs"
                  % (work.title[:10], before, after, len(mapping), cost))
            for line in [ln for ln in decoded.splitlines() if ln.strip()][:2]:
                print("   >", line[:56])
            if after:
                left = "".join(dict.fromkeys(
                    ch for ch in decoded if 0xAC00 <= ord(ch) <= 0xD7A3))
                print("   未解:", left[:18])
    finally:
        http.close()
    if stats:
        print("合计：原文假字 %d → 解码后 %d（%d 本）"
              % (sum(item[0] for item in stats), sum(item[1] for item in stats), len(stats)))
        return 0 if all(item[1] == 0 for item in stats) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
