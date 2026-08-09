# -*- coding: utf-8 -*-
"""dm5（动漫屋）源全链路真机验证：发现 → 搜索 → 详情 → 章节 → 正文图片。

用法：python -u tools_verify_dm5.py
（正文图片走 chapterfun.ashx 接口 + JS Packer 解包，逐页请求，请耐心等待）
"""
from __future__ import annotations

import sys

sys.path.insert(0, r"D:\code\claw")

import json

from framework.config import SourceConfig
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.discovery import Discovery
from framework.search import Search
from framework.content import Content
from framework.decrypter import Decrypter

SOURCE_PATH = r"D:\code\claw\sources\dm5.json"
FALLBACK_DETAIL_URL = "https://www.dm5.com/manhua-haizeiwang-aisi/"

PASS, FAIL = 0, 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    tag = "PASS" if cond else "FAIL"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{tag}] {label}" + (f" | {extra}" if extra else ""))


def main() -> int:
    with open(SOURCE_PATH, "r", encoding="utf-8") as f:
        src = SourceConfig.from_dict(json.load(f), SOURCE_PATH)
    print(f"[源] {src.source_name}  engine={Parser().engine()}")

    with HttpClient() as http:
        parser = Parser()
        checker = StructureChecker(http=http, parser=parser, default_strategy="off")
        disc = Discovery(http=http, parser=parser, checker=checker)

        # 1. 配置加载 OK + 分类列表
        print("\n=== 1. Config / Categories ===")
        check("配置加载", src.source_id == "dm5" and src.content_type == "comic", src.source_id)
        cats = disc.list_categories(src)
        check("分类列表非空", len(cats) >= 5, f"{len(cats)} 个")
        for c in cats[:5]:
            print(f"     - {c.title}: {c.url}")

        # 2. Discovery 作品列表
        print("\n=== 2. Discovery /manhua-list-p1/ ===")
        try:
            works = disc.list_works(src, src.raw["endpoints"]["discovery"]["list_url"].replace("{page}", "1"), 1)
            check("发现作品非空", len(works) >= 10, f"{len(works)} 条")
            ok = sum(1 for w in works[:20] if w.title and w.url and w.cover)
            check("作品 title/url/cover 非空", ok >= 10, f"{ok}/20")
            for w in works[:5]:
                print(f"     - {w.title[:30]} | {w.url} | {w.cover[:60]}")
            sample_url = works[0].url if works else ""
        except Exception as e:
            check("发现作品", False, f"{e}")
            sample_url = ""

        # 3. Search
        print("\n=== 3. Search 海贼王 ===")
        try:
            results = Search(http, parser).search_one(src, "海贼王")
            check("搜索结果 ≥ 1", len(results) >= 1, f"{len(results)} 条")
            for r in results[:5]:
                print(f"     - {r.title[:30]} | {r.url} | {r.cover[:60]}")
            if results and not sample_url:
                sample_url = results[0].url
        except Exception as e:
            check("搜索", False, f"{e}")

        # 4. Detail
        print("\n=== 4. Detail ===")
        if not sample_url:
            sample_url = FALLBACK_DETAIL_URL
        print(f"    详情 URL: {sample_url}")
        ct = Content(http=http, parser=parser, checker=checker, decrypter=Decrypter(http))
        detail = None
        try:
            detail = ct.fetch_detail(src, sample_url)
            check("详情标题非空", bool(detail.title), detail.title[:40])
            check("详情作者非空", bool(detail.author), detail.author[:40])
            check("详情封面非空", bool(detail.cover), detail.cover[:60])
            check("章节数 ≥ 1", len(detail.chapters) >= 1, f"{len(detail.chapters)} 章")
            print(f"     summary: {(detail.summary or '')[:60]}")
            if detail.chapters:
                print(f"     首章: {detail.chapters[0].title[:40]} | {detail.chapters[0].url}")
                print(f"     末章: {detail.chapters[-1].title[:40]} | {detail.chapters[-1].url}")
        except Exception as e:
            check("详情", False, f"{e}")

        # 5. 正文图片
        print("\n=== 5. Comic Pages (chapterfun.ashx + JS Packer) ===")
        if detail and detail.chapters:
            ch = detail.chapters[0]
            print(f"     章节: {ch.title[:40]} | {ch.url}")
            try:
                imgs = ct.fetch_comic_pages(src, ch.url)
                check("图片 ≥ 3 张", len(imgs) >= 3, f"{len(imgs)} 张")
                for u in imgs[:5]:
                    print(f"     img: {u[:100]}")
                # 对前 3 张做 HTTP 访问验证（源头无 Referer；dm5 CDN 对带 Referer
                # 的请求校验 key，直取不带 Referer 最稳；get_bytes 非 2xx 抛 RequestError）
                good = 0
                img_headers = {**src.request_headers()}
                for u in imgs[:3]:
                    try:
                        data = http.get_bytes(u, headers=img_headers, timeout=15, retries=0)
                        ok = len(data) > 5000 and data[:2] in (b"\xff\xd8", b"\x89P")
                        good += 1 if ok else 0
                        print(f"     check {'OK ' if ok else 'BAD'} {len(data)}B | {u.split('/')[-1][:60]}")
                    except Exception as e:
                        print(f"     check FAIL {u[:60]} | {e}")
                check("前 3 张图片可访问(200/图片)", good >= 3, f"{good}/3")
            except Exception as e:
                check("正文图片", False, f"{e}")
        else:
            check("正文图片", False, "无章节可验证")

    print(f"\n===== RESULT: PASS={PASS} FAIL={FAIL} =====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
