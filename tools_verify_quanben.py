# -*- coding: utf-8 -*-
"""全本小说网（quanben-xiaoshuo.com）源全链路验证：发现 → 搜索 → 详情 → 目录 → 正文。

用法：python -u tools_verify_quanben.py
通过条件（全部断言）：
  - 配置加载 OK
  - 分类列表 >= 3
  - discovery >= 10 条（title/url 有效）
  - search >= 1 条（Playwright 渲染，JS 生成 b 参数）
  - 详情 title/author/cover 有效、chapters >= 3
  - fetch_chapter 正文 >= 100 字符
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, r"D:\code\claw")

from framework.config import SourceConfig
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.discovery import Discovery
from framework.search import Search
from framework.content import Content
from framework.decrypter import Decrypter

SOURCE_PATH = r"D:\code\claw\sources\quanben.json"

PASS = 0
FAIL = []


def check(name: str, ok: bool, extra: str = ""):
    global PASS
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}" + (f"  {extra}" if extra else ""))


def main() -> int:
    with open(SOURCE_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    src = SourceConfig.from_dict(raw, SOURCE_PATH)
    print(f"[1] 配置加载: {src.source_name} ({src.source_id}) type={src.content_type}")
    check("配置加载", src.source_id == "quanben" and src.content_type == "novel")
    check("transports.base_url", src.base_url.startswith("https://quanben-xiaoshuo.com"))

    with HttpClient() as http:
        parser = Parser()
        checker = StructureChecker(http=http, parser=parser, default_strategy="off")

        # --- 2. 分类列表 ---
        print("\n[2] Discovery 分类")
        disc = Discovery(http=http, parser=parser, checker=checker)
        cats = []
        try:
            cats = disc.list_categories(src)
        except Exception as e:
            print(f"  分类抓取失败: {e}")
        print(f"  分类数: {len(cats)}")
        for c in cats[:6]:
            print(f"    - {c.title} | {c.url}")
        check("分类 >= 3", len(cats) >= 3, f"got {len(cats)}")
        check("分类 url 有效",
              all("/category/" in c.url for c in cats))

        # --- 3. Discovery 作品列表 ---
        print("\n[3] Discovery 作品列表")
        works = []
        if cats:
            try:
                works = disc.list_works(src, cats[0].url, 1)
            except Exception as e:
                print(f"  作品列表抓取失败: {e}")
        print(f"  作品数: {len(works)}")
        for w in works[:5]:
            print(f"    - {w.title[:30]} | {w.url} | cover={w.cover[:50]}")
        check("作品 >= 10", len(works) >= 10, f"got {len(works)}")
        check("作品 title 有效", all(w.title for w in works[:10]))
        check("作品 url 有效", all("/n/" in w.url for w in works[:10]))
        # 列表页为纯文本（站点无封面）——如实记录，不强断 cover
        no_cover = sum(1 for w in works if not w.cover)
        print(f"  [INFO] 列表页无封面 {no_cover}/{len(works)} 条（站点纯文本列表，详情页有封面）")

        # --- 4. 搜索（Playwright 渲染） ---
        print("\n[4] 搜索")
        s = Search(http=http, parser=parser)
        results = []
        try:
            results = s.search_one(src, "都市")
        except Exception as e:
            print(f"  搜索失败: {e}")
        print(f"  结果数: {len(results)}")
        for r in results[:5]:
            print(f"    - {r.title[:30]} | {r.url}")
        check("搜索 >= 1", len(results) >= 1, f"got {len(results)}")
        check("搜索结果 title/url 有效",
              all(r.title and r.url for r in results[:5]))

        # --- 5. 详情 ---
        print("\n[5] 详情")
        sample_url = ""
        for w in works:
            if w.url:
                sample_url = w.url
                break
        if not sample_url and results:
            sample_url = results[0].url
        print(f"  详情 URL: {sample_url}")
        ct = Content(http=http, parser=parser, checker=checker, decrypter=Decrypter(http))
        detail = None
        if sample_url:
            try:
                detail = ct.fetch_detail(src, sample_url)
            except Exception as e:
                print(f"  详情抓取失败: {e}")
        if detail:
            print(f"  标题: {detail.title}")
            print(f"  作者: {detail.author}")
            print(f"  封面: {detail.cover[:60]}")
            print(f"  简介: {(detail.summary or '')[:60]}...")
            print(f"  章节数: {len(detail.chapters)}")
            if detail.chapters:
                print(f"  首章: {detail.chapters[0].title} | {detail.chapters[0].url}")
                print(f"  末章: {detail.chapters[-1].title} | {detail.chapters[-1].url}")
            check("详情 title 有效", bool(detail.title), detail.title[:30])
            check("详情 author 有效", bool(detail.author), detail.author[:20])
            check("详情 cover 有效", bool(detail.cover), detail.cover[:40])
            check("章节 >= 3", len(detail.chapters) >= 3, f"got {len(detail.chapters)}")
        else:
            for n in ("详情 title 有效", "详情 author 有效", "详情 cover 有效", "章节 >= 3"):
                check(n, False, "详情为空")

        # --- 6. 正文 ---
        print("\n[6] 正文")
        if detail and detail.chapters:
            ch = detail.chapters[0]
            try:
                text = ct.fetch_chapter(src, ch.url)
                print(f"  章节: {ch.title}")
                print(f"  正文长度: {len(text)} 字符")
                print(f"  开头: {text[:80].replace(chr(10), ' ')}...")
                check("正文 >= 100", len(text) >= 100, f"got {len(text)}")
            except Exception as e:
                print(f"  正文抓取失败: {e}")
                check("正文 >= 100", False, f"{e}")
        else:
            check("正文 >= 100", False, "无章节")

    print(f"\n==== 结果: {PASS} PASS / {len(FAIL)} FAIL ====")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
