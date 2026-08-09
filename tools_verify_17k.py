# -*- coding: utf-8 -*-
"""17k 源全链路真机验证：搜索 → 发现 → 详情 → 目录 → 正文。

用法：python -u tools_verify_17k.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, r"D:\code\claw")

from framework.config import SourceConfig
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.discovery import Discovery
from framework.content import Content
from framework.search import Search

SOURCE_PATH = r"D:\code\claw\sources\17k.json"


def main() -> int:
    import json

    with open(SOURCE_PATH, "r", encoding="utf-8") as f:
        src = SourceConfig.from_dict(json.load(f), SOURCE_PATH)
    print(f"[源] {src.source_name}  engine={Parser().engine()}")

    with HttpClient() as http:
        checker = StructureChecker(http=http, parser=Parser())

        # 0. Search（render:playwright，走 search.17k.com/search.xhtml）
        print("\n=== 0. Search 搜索 ===")
        try:
            se = Search(http=http, parser=Parser())
            for kw in ["斗破苍穹", "凡人修仙传"]:
                res = se.search_one(src, kw)
                valid = [
                    r for r in res
                    if r.title and r.url.startswith("http") and "/book/" in r.url
                ]
                print(f"搜「{kw}」→ {len(res)} 条（有效 {len(valid)}）")
                for r in valid[:3]:
                    print(f"  - {r.title[:30]} | {r.url}")
        except Exception as e:
            print(f"Search 失败: {e}")

        # 1. Discovery 免费频道
        print("\n=== 1. Discovery /mianfei/ ===")
        try:
            disc = Discovery(http=http, parser=Parser(), checker=checker)
            list_url = (src.raw.get("endpoints", {}).get("discovery", {}) or {}).get("list_url", "/mianfei/")
            items = disc.list_works(src, list_url, 1)
            print(f"发现 {len(items)} 条")
            for it in items[:5]:
                print(f"  - {getattr(it, 'title', '')[:30]} | {getattr(it, 'url', '')}")
            if items:
                sample_url = getattr(items[0], "url", "")
            else:
                sample_url = ""
        except Exception as e:
            print(f"Discovery 失败: {e}")
            sample_url = ""
        print("\n=== 2. Detail ===")
        if not sample_url:
            sample_url = "https://www.17k.com/book/3287122.html"  # 常见书 ID 兜底
        print(f"详情 URL: {sample_url}")
        try:
            ct = Content(http=http, parser=Parser(), checker=checker)
            detail = ct.fetch_detail(src, sample_url)
            print(f"标题: {detail.title}")
            print(f"作者: {detail.author}")
            print(f"状态: {detail.status}")
            print(f"简介: {(detail.summary or '')[:60]}...")
            print(f"章节数: {len(detail.chapters)}")
            if detail.chapters:
                print(f"首章: {detail.chapters[0].title} | {detail.chapters[0].url}")
                print(f"末章: {detail.chapters[-1].title} | {detail.chapters[-1].url}")
        except Exception as e:
            print(f"Detail 失败: {e}")
            detail = None

        # 3. Chapter 正文（取第一章）
        print("\n=== 3. Chapter 正文 ===")
        if detail and detail.chapters:
            ch = detail.chapters[0]
            try:
                text = ct.fetch_chapter(src, ch.url)
                if text:
                    print(f"正文长度: {len(text)} 字符")
                    print(f"正文开头: {text[:80].replace(chr(10), ' ')}...")
                else:
                    print("正文为空！")
            except Exception as e:
                print(f"Chapter 失败: {e}")
        else:
            print("无章节可验证")

    return 0


if __name__ == "__main__":
    sys.exit(main())
