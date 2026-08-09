# -*- coding: utf-8 -*-
"""manwa（漫蛙漫画）源全链路验证脚本。

验证内容：
1. 配置加载 OK
2. 分类：动态从 /booklist 过滤面板抓取（.panel-tag），数量与实际站点标签面板一致（全部列全）
3. Discovery（/getBooks JSON API，tag 分类）≥ 40 条（title/url/cover 有效）
4. Search：分页修复后 /search?keyword=&page=N 返回条数 ≈ 站点逐页实测总数（海贼王 ~640）
5. Detail（/book/{id}）title/author/cover/chapters ≥ 1
6. fetch_comic_pages ≥ 3 张图片 URL，抽样 2 张验证可访问（data URI 则验 WEBP 魔数）
7. 全部断言通过

用法：python -u tools_verify_manwa.py
"""
from __future__ import annotations

import base64
import json
import re
import sys
import time

sys.path.insert(0, r"D:\code\claw")

import requests
from urllib.parse import quote

from framework.config import SourceConfig
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.search import Search
from framework.discovery import Discovery
from framework.content import Content
from framework.decrypter import Decrypter

SOURCE_PATH = r"D:\code\claw\sources\manwa.json"
BASE = "https://manwa.me"
KEYWORD = "海贼王"
CATEGORY_URL = "/booklist?tag=古风"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": BASE + "/",
    "X-Requested-With": "XMLHttpRequest",
}


def _is_webp(data: bytes) -> bool:
    return len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _check_http_ok(url: str, referer: str = "https://manwa.me/") -> bool:
    """HTTP 验证图片可访问：data URI 解码验魔数，普通 URL 请求验 200+图片头。"""
    if url.startswith("data:"):
        try:
            b64 = url.split(",", 1)[1]
            raw = base64.b64decode(b64)
            return _is_webp(raw) or raw[:3] == b"\xff\xd8\xff" or raw[:4] == b"\x89PNG"
        except Exception:
            return False
    try:
        r = requests.get(url, timeout=20, headers={
            "User-Agent": HEADERS["User-Agent"], "Referer": referer,
        })
        return r.status_code == 200 and len(r.content) > 1000
    except Exception:
        return False


def _get(url: str):
    time.sleep(1.0)
    return requests.get(url, headers=HEADERS, timeout=15)


def _site_category_count() -> int:
    """站点 /booklist 过滤面板里的标签总数（.panel-tag 且 data-val 非空）。"""
    r = _get(BASE + "/booklist")
    return len(re.findall(r'class="panel-tag[^"]*"\s+data-val="[^"]+"', r.text))


def _site_search_total(kw: str, max_pages: int = 20) -> int:
    """站点搜索逐页实测：抓 /search?keyword=&page=N 直到内容重复（越界 clamp），
    返回去重后的真实图书总数（站点搜索结果无总数字段，只能逐页数）。"""
    unique: set = set()
    prev: set = set()
    for pg in range(1, max_pages + 1):
        r = _get(BASE + "/search?keyword=" + quote(kw) + "&page=%d" % pg)
        urls = set(re.findall(r"/book/(\d+)", r.text))
        if not urls:
            break
        unique |= urls
        if pg > 1 and urls == prev:
            break  # 越界 clamp：内容与上一页相同 → 到末尾
        prev = urls
    return len(unique)


def main() -> int:
    with open(SOURCE_PATH, encoding="utf-8") as f:
        src = SourceConfig.from_dict(json.load(f), SOURCE_PATH)

    http = HttpClient()
    parser = Parser()
    checker = StructureChecker(http, parser, default_strategy="off")
    results = []

    print("=== 1) Config load ===")
    ok1 = src.source_id == "manwa" and src.content_type == "comic" and bool(src.base_url)
    results.append(("config_load", ok1))
    print("  source_id=%s type=%s base=%s -> %s" % (src.source_id, src.content_type, src.base_url, "PASS" if ok1 else "FAIL"))

    print("=== 2) Discovery categories (dynamic from /booklist panel) ===")
    discovery = Discovery(http, parser, checker)
    cats = discovery.list_categories(src)
    site_cats = _site_category_count()
    names = [c.title for c in cats]
    key_ok = all(k in names for k in ["古风", "悬疑", "百合", "耽美"])
    ok2 = len(cats) >= 500 and key_ok and abs(len(cats) - site_cats) <= 5
    results.append(("categories", ok2))
    print("  framework_categories=%d  site_panel_tags=%d (dedup diff=%d) -> %s"
          % (len(cats), site_cats, site_cats - len(cats), "PASS" if ok2 else "FAIL"))
    print("  has 古风/悬疑/百合/耽美: %s" % key_ok)
    for c in cats[:6]:
        print("    -", c.title, "|", c.url)

    print("=== 3) Discovery works (API /getBooks) ===")
    works = discovery.list_works(src, CATEGORY_URL, page=1)
    valid = [w for w in works if w.title and w.url and w.cover]
    ok3 = len(works) >= 40 and len(valid) == len(works)
    results.append(("discovery_works", ok3))
    print("  works=%d valid=%d -> %s" % (len(works), len(valid), "PASS" if ok3 else "FAIL"))
    for w in works[:4]:
        print("    -", w.title[:50], "|", w.url, "| cover=%s" % (w.cover[:60] if w.cover else ""))
    first_work = works[0] if works else None

    print("=== 4) Search /search?keyword=&page=N (paged) ===")
    search = Search(http, parser)
    sr = search.search_one(src, KEYWORD)
    site_total = _site_search_total(KEYWORD)
    ok4 = len(sr) >= 500 and abs(len(sr) - site_total) <= 5
    results.append(("search", ok4))
    print("  framework_results=%d  site_paged_total=%d (diff=%d) -> %s"
          % (len(sr), site_total, len(sr) - site_total, "PASS" if ok4 else "FAIL"))
    for r in sr[:4]:
        print("    -", r.title[:50], "|", r.url, "| cover=%s" % (r.cover[:60] if r.cover else ""))

    print("=== 5) Detail fetch ===")
    content = Content(http, parser, checker, Decrypter(http))
    detail_url = first_work.url if first_work else (sr[0].url if sr else "")
    ok5 = False
    detail = None
    if detail_url:
        try:
            detail = content.fetch_detail(src, detail_url)
            chapters = detail.chapters or []
            ok5 = bool(detail.title and detail.author and detail.cover and len(chapters) >= 1)
            print("  url=%s" % detail_url)
            print("  title=%s" % detail.title[:60])
            print("  author=%s" % detail.author[:60])
            print("  cover=%s" % (detail.cover[:60] if detail.cover else ""))
            print("  summary=%s" % (detail.summary[:80] if detail.summary else ""))
            print("  chapters=%d" % len(chapters))
            if chapters[:3]:
                print("    first chapters:")
                for ch in chapters[:3]:
                    print("      -", ch.title[:40], "|", ch.url)
            print("  -> %s" % ("PASS" if ok5 else "FAIL"))
        except Exception as exc:
            print("  detail EXCEPTION: %r" % exc)
    results.append(("detail", ok5))

    print("=== 6) Content fetch_comic_pages ===")
    ok6 = False
    img_urls = []
    if detail and detail.chapters:
        try:
            img_urls = content.fetch_comic_pages(src, detail.chapters[0].url)
            print("  chapter=%s" % detail.chapters[0].url)
            print("  images=%d" % len(img_urls))
            for u in img_urls[:3]:
                print("    -", u[:90])
            ok6 = len(img_urls) >= 3
            results.append(("fetch_comic_pages_count", ok6))
            if img_urls:
                samples = [img_urls[0], img_urls[min(1, len(img_urls) - 1)]]
                checks = [_check_http_ok(u) for u in samples]
                print("  sample checks=%s -> %s" % (checks, "PASS" if all(checks) else "FAIL"))
                results.append(("img_accessible", all(checks)))
            else:
                results.append(("img_accessible", False))
        except Exception as exc:
            print("  fetch_comic_pages EXCEPTION: %r" % exc)
            results.append(("fetch_comic_pages_count", False))
            results.append(("img_accessible", False))
    else:
        print("  no chapters to test -> FAIL")
        results.append(("fetch_comic_pages_count", False))
        results.append(("img_accessible", False))

    http.close()

    final = all(v for _, v in results)
    print("=== Results ===")
    for name, v in results:
        print("  [%s] %s" % (name, "PASS" if v else "FAIL"))
    print("=== TOTAL: %s ===" % ("ALL PASS" if final else "FAILED"))
    return 0 if final else 1


if __name__ == "__main__":
    sys.exit(main())
