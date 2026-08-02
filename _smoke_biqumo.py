"""biqumo 真实源完整链路测试（需网络）。"""
import os, sys, pathlib
# _smoke_biqumo.py 在项目根（claw），parent 即项目根
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from framework.config import load_source
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.discovery import Discovery
from framework.content import Content
from framework.decrypter import Decrypter


def main():
    source = load_source("sources/biqumo.json")
    print("源:", source.source_id, source.source_name, "discovery:", source.has_discovery())

    http = HttpClient()
    parser = Parser()
    checker = StructureChecker(http, parser, default_strategy="soft")
    decrypter = Decrypter(http)
    discovery = Discovery(http, parser, checker)
    content = Content(http, parser, checker, decrypter)

    print("\n=== 1. 分类 ===")
    try:
        cats = discovery.list_categories(source)
        for c in cats[:8]:
            print(" ", c.title, c.url)
        assert any("/sort/" in c.url for c in cats), "分类应含 /sort/ 链接"
        print(f"共 {len(cats)} 个分类")
    except Exception as e:
        print("分类失败:", e)
        return

    print("\n=== 2. 作品列表（分类第1页） ===")
    cat_url = cats[0].url  # /sort/1/1/
    try:
        works = discovery.list_works(source, cat_url, 1)
        for w in works[:3]:
            print(" ", w.title, "|", w.url, "|", w.cover)
        assert works, "应返回作品"
        print(f"共 {len(works)} 部作品")
    except Exception as e:
        print("作品失败:", e)
        return

    print("\n=== 3. 详情页 ===")
    work = works[0]
    try:
        detail = content.fetch_detail(source, work.url)
        print(" 标题:", detail.title)
        print(" 作者:", detail.author)
        print(" 章节数:", len(detail.chapters))
        if detail.chapters:
            print(" 首章:", detail.chapters[0].title, detail.chapters[0].url)
    except Exception as e:
        print("详情失败:", e)
        return

    print("\n=== 4. 正文 ===")
    if detail.chapters:
        try:
            text = content.fetch_chapter(source, detail.chapters[0].url)
            print(" 正文前100字:", text[:100])
            assert len(text) > 50
        except Exception as e:
            print("正文失败:", e)

    print("\n=== biqumo 真实链路测试完成 ===")


if __name__ == "__main__":
    main()
