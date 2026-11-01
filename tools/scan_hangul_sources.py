# -*- coding: utf-8 -*-
"""扫描所有小说源：正文里是否出现谚文码点（反爬子集字体混淆）。

用途：定位「换个源还是乱码」到底是哪个源在混淆，避免只盯着 timotxt。
每个源取发现页第一本 → 详情第一本 → 第一章正文，统计：
  假字 = U+AC00–U+D7A3 的字符数
  私用区 = U+E000–U+F8FF（另一种常见字体混淆区间）
用法：
    python tools/scan_hangul_sources.py [源id前缀过滤]
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import glob  # noqa: E402

from framework.config import load_source  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.decrypter import Decrypter  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.http import HttpClient, NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402


class _NC:
    def _selfcheck_url(self, source):
        return ""

    def check(self, source, url):
        return True


def counts(text):
    hangul = sum(1 for ch in text if 0xAC00 <= ord(ch) <= 0xD7A3)
    pua = sum(1 for ch in text if 0xE000 <= ord(ch) <= 0xF8FF)
    return hangul, pua


def main(argv):
    prefix = argv[1] if len(argv) > 1 else ""
    http = HttpClient(defaults=NetworkDefaults(timeout=12, retries=1, interval_ms=200))
    parser, checker = Parser(), _NC()
    content = Content(http, parser, checker, decrypter=Decrypter(http))
    discovery = Discovery(http, parser, checker)
    hits = []
    try:
        for path in sorted(glob.glob("sources/*.json")):
            name = os.path.basename(path)[:-5]
            if prefix and not name.startswith(prefix):
                continue
            try:
                source = load_source(path)
            except Exception:
                continue
            if (source.raw.get("$type") or source.raw.get("type")) != "novel":
                continue
            started = time.time()
            try:
                works = discovery.list_works(source, source.base_url, 1)
                if not works:
                    print("%-14s 发现为空" % name)
                    continue
                detail = content.fetch_detail(source, works[0].url)
                if not detail.chapters:
                    print("%-14s 目录为空" % name)
                    continue
                text = content.fetch_chapter(source, detail.chapters[0].url)
            except Exception as exc:  # noqa: BLE001
                print("%-14s 失败 %s: %s" % (name, type(exc).__name__, str(exc)[:60]))
                continue
            hangul, pua = counts(text)
            flag = "★ 混淆" if (hangul or pua) else "  "
            print("%s %-14s %-22s %5.1fs 假字=%-4d 私用区=%-4d 样本: %s"
                  % (flag, name, (works[0].title or "")[:20], time.time() - started,
                     hangul, pua,
                     "".join(ch for ch in text if ch.strip())[:26].replace("\n", " ")))
            if hangul or pua:
                hits.append((name, works[0].title, detail.chapters[0].url, hangul, pua))
    finally:
        http.close()
    print("\n=== 命中 %d 个源 ===" % len(hits))
    for name, title, url, hangul, pua in hits:
        print("  %-14s %-22s 假字=%-4d 私用区=%-4d %s" % (name, (title or "")[:20], hangul, pua, url))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
