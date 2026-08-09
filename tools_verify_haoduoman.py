# -*- coding: utf-8 -*-
"""haoduoman（好多漫）源全链路验证脚本。

验证内容：
1. 配置加载 OK（SourceConfig.from_dict）
2. 分类列表非空（discovery.list_categories）
3. Discovery 列表 ≥ 10 条（title/url/cover 有效）
4. Search 搜索 1 个关键词 ≥ 1 条
5. 详情 title/author/cover/chapters ≥ 1
6. fetch_comic_pages 返回 ≥ 3 张图片 URL，抽样 2 张 HTTP 验证可访问（200，非 403/404）

用法：python -u tools_verify_haoduoman.py
"""
from __future__ import annotations

import json
import sys

import requests

sys.path.insert(0, r"D:\code\claw")

from framework.config import SourceConfig
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.search import Search
from framework.discovery import Discovery
from framework.content import Content
from framework.decrypter import Decrypter

SOURCE_PATH = r"D:\code\claw\sources\haoduoman.json"
KEYWORD = "火影"
DISCOVERY_URL = "https://www.haoduoman.com/manhua"


def check_http_img(url: str, timeout: int = 20) -> bool:
    """HTTP 验证图片可访问（200，非 403/404）。"""
    try:
        r = requests.get(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
            "Referer": "https://www.haoduoman.com/",
        }, timeout=timeout)
        return r.status_code == 200
    except Exception as exc:
        print("  img HTTP err:", repr(exc)[:120])
        return False


def main() -> int:
    with open(SOURCE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    src = SourceConfig.from_dict(data, SOURCE_PATH)

    http = HttpClient()
    parser = Parser()
    checker = StructureChecker(http, parser, default_strategy="off")

    print("=== 1) 配置加载 ===")
    ok1 = src.source_id == "haoduoman" and src.content_type == "comic"
    print("  $id=%s $type=%s -> %s" % (src.source_id, src.content_type, "PASS" if ok1 else "FAIL"))

    print("=== 2) Discovery 分类列表 ===")
    discovery = Discovery(http, parser, checker)
    cats = discovery.list_categories(src)
    ok2 = len(cats) >= 1
    print("  categories=%d -> %s" % (len(cats), "PASS" if ok2 else "FAIL"))
    for c in cats[:5]:
        print("    -", c.title, "|", c.url)

    print("=== 3) Discovery 作品列表（%s） ===" % DISCOVERY_URL)
    works = discovery.list_works(src, DISCOVERY_URL, page=1)
    valid = [w for w in works if w.title and w.url]
    ok3 = len(works) >= 10 and len(valid) == len(works) and all(w.cover for w in works[:10])
    print("  works=%d valid=%d cover_ok(first10)=%s -> %s" % (
        len(works), len(valid),
        all(w.cover for w in works[:10]),
        "PASS" if ok3 else "FAIL"))
    for w in works[:3]:
        print("    -", getattr(w, "title", "")[:60], "|", getattr(w, "url", "")[:60], "|", getattr(w, "cover", "")[:60])

    print("=== 4) Search（关键词：%s） ===" % KEYWORD)
    search = Search(http, parser)
    results = search.search_one(src, KEYWORD)
    valid_r = [r for r in results if r.title and r.url]
    ok4 = len(results) >= 1 and len(valid_r) == len(results)
    print("  results=%d valid=%d -> %s" % (len(results), len(valid_r), "PASS" if ok4 else "FAIL"))
    for r in results[:3]:
        print("    -", r.title[:60], "|", r.url)

    print("=== 5) Detail（取搜索结果首条） ===")
    content = Content(http, parser, checker, Decrypter(http))
    if results:
        first = results[0]
        try:
            detail = content.fetch_detail(src, first.url)
            ok5 = bool(detail.title) and bool(detail.author) and bool(detail.cover) and len(detail.chapters) >= 1
            print("  title=%s" % detail.title[:60])
            print("  author=%s" % detail.author[:60])
            print("  cover=%s" % detail.cover[:80])
            print("  chapters=%d" % len(detail.chapters))
            print("  -> %s" % ("PASS" if ok5 else "FAIL"))
        except Exception as exc:
            ok5 = False
            print("  详情异常: %r" % exc)
    else:
        ok5 = False
        print("  无搜索结果可测")

    print("=== 6) fetch_comic_pages（取详情章节首章） ===")
    if results:
        try:
            detail = content.fetch_detail(src, results[0].url)
        except Exception:
            detail = None
        if detail and detail.chapters:
            ch = detail.chapters[0]
            try:
                pages = content.fetch_comic_pages(src, ch.url)
                ok6a = len(pages) >= 3
                print("  chapter=%s" % ch.title[:60])
                print("  pages=%d -> %s" % (len(pages), "PASS" if ok6a else "FAIL"))
                for u in pages[:3]:
                    print("    -", u)
                # 抽样 2 张 HTTP 验证
                sample = pages[:2]
                statuses = [check_http_img(u) for u in sample]
                ok6b = all(statuses)
                print("  抽样 %d 张 HTTP 200 -> %s" % (len(sample), "PASS" if ok6b else "FAIL"))
                ok6 = ok6a and ok6b
            except Exception as exc:
                ok6 = False
                print("  fetch_comic_pages 异常: %r" % exc)
        else:
            ok6 = False
            print("  无章节可测")
    else:
        ok6 = False
        print("  无搜索结果可测")

    http.close()
    final = ok1 and ok2 and ok3 and ok4 and ok5 and ok6
    print("=== 总结果: %s ===" % ("ALL PASS" if final else "FAILED"))
    return 0 if final else 1


if __name__ == "__main__":
    sys.exit(main())
