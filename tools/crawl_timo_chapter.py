# -*- coding: utf-8 -*-
"""真机复核：用 GUI 同款链路（Content + Decrypter + 正文缓存）实爬一章并打印。

用法：
    python tools/crawl_timo_chapter.py [书序]
做三件事：
1. 直连抓原始 HTML，数一遍正文里的假字（证明站点确实还在下发混淆码点）；
2. 走 Content.fetch_chapter（与 GUI 相同的 decrypter/cache 组合）拿正文，数解码后假字；
3. 往缓存里塞一段「解码上线前缓存的」假字文本，再读一次，验证缓存命中也会就地重解。
"""
import html as html_lib
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.config import load_source  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.decrypter import Decrypter  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.http import HttpClient, NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402

BODY_RE = re.compile(r'<div class="content">(.*?)</div>\s*<div', re.S)
HANGUL = (0xAC00, 0xD7A3)


class _NC:
    def _selfcheck_url(self, source):
        return ""

    def check(self, source, url):
        return True


class _MemCache:
    """RedisLikeStore 的最小替身：与 GUI 的 shelf_cache 同为 get/set(ttl)。"""

    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ttl=None):
        self.data[key] = value


def fake_count(text: str) -> int:
    return sum(1 for ch in text if HANGUL[0] <= ord(ch) <= HANGUL[1])


def raw_body(page: str) -> str:
    match = BODY_RE.search(page or "")
    if not match:
        return ""
    inner = re.sub(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", "",
                   match.group(1), flags=re.S)
    inner = re.sub(r"<br\s*/?>|</p>", "\n", inner, flags=re.I)
    return html_lib.unescape(re.sub(r"<[^>]+>", "", inner)).strip()


def main(argv) -> int:
    index = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 0
    source = load_source("sources/timotxt.json")
    http = HttpClient(defaults=NetworkDefaults(timeout=15, retries=1, interval_ms=300))
    parser, checker = Parser(), _NC()
    cache = _MemCache()
    content = Content(http, parser, checker, Decrypter(http))
    content._cache = cache  # 与 gui/app.py 里 content._cache = shelf_cache 一致
    discovery = Discovery(http, parser, checker)
    try:
        works = discovery.list_works(source, source.base_url, 1)
        work = works[index]
        detail = content.fetch_detail(source, work.url)
        chapter = detail.chapters[0]
        page = http.get_text(chapter.url, headers=source.request_headers(), timeout=15)
        before = raw_body(page)
        started = time.time()
        decoded = content.fetch_chapter(source, chapter.url)
        cost = time.time() - started
        again = content.fetch_chapter(source, chapter.url)  # 命中缓存
        print("书：%s" % work.title)
        print("章：%s" % chapter.url)
        print("① 站上下发的原文：假字 %d（前两段见下）" % fake_count(before))
        for line in [ln for ln in before.splitlines() if ln.strip()][:2]:
            print("   #", line[:64])
        print("② Content.fetch_chapter：假字 %d | %.2fs" % (fake_count(decoded), cost))
        for line in [ln for ln in decoded.splitlines() if ln.strip()][1:4]:
            print("   >", line[:64])
        print("③ 缓存重读：假字 %d | 与首次一致 %s"
              % (fake_count(again), again == decoded))
        # ④ 模拟「解码上线前缓存的正文」：当前版本键下塞入假字文本，再读
        key = content._chapter_cache_key(source, chapter.url)
        stale = before
        cache.data[key] = stale
        healed = content.fetch_chapter(source, chapter.url)
        print("④ 旧缓存自愈：塞入假字 %d → 读出假字 %d | 已回写 %s"
              % (fake_count(stale), fake_count(healed),
                 fake_count(cache.data[key]) == 0))
        ok = (fake_count(decoded) == 0 and fake_count(healed) == 0
              and again == decoded)
        print("结论：", "全部解码正确" if ok else "仍有假字，需要继续查")
        return 0 if ok else 1
    finally:
        http.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
