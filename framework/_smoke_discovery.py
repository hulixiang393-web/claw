"""发现内核冒烟测试（mock HTTP，不依赖外网）。"""
import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from framework.config import SourceConfig
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.discovery import Discovery

# 假站点 HTML
CATEGORY_HTML = """<html><body>
<div class="site-header">demo</div>
<div class="category-list">
  <a class="cat" href="/cat/fantasy">玄幻</a>
  <a class="cat" href="/cat/martial">武侠</a>
  <a class="cat" href="/cat/urban">都市</a>
</div>
</body></html>"""

WORKS_HTML = """<html><body>
<div class="site-header">demo</div>
<div class="book-list">
  <div class="book-item">
    <a class="title" href="/book/1">书一</a>
    <img class="cover" src="/cover/1.jpg">
    <span class="author">作者甲</span>
  </div>
  <div class="book-item">
    <a class="title" href="/book/2">书二</a>
    <img class="cover" src="/cover/2.jpg">
    <span class="author">作者乙</span>
  </div>
</div>
</body></html>"""

SOURCE_RAW = {
    "$schema_version": 2,
    "$id": "demo",
    "$type": "novel",
    "$name": "演示站",
    "transports": {"base_url": "http://example.com"},
    "endpoints": {
        "discovery": {
            "list_url": "/category",
            "list_item": {
                "root_selector": {"css": ".category-list a"},
                "fields": {"title": {"css": "a"}, "url": {"css": "a", "attr": "href"}},
            },
        },
        "search": {
            "base_url": "/search",
            "item": {
                "root_selector": {"css": ".book-item"},
                "fields": {
                    "title": {"css": "a.title"},
                    "url": {"css": "a.title", "attr": "href"},
                    "cover": {"css": "img.cover", "attr": "src"},
                    "author": {"css": ".author"},
                },
            },
        },
    },
}


class MockHttp(HttpClient):
    """mock：按 URL 返回固定 HTML。"""

    def get_text(self, url, headers=None, proxy=None, timeout=10, retries=3, interval_ms=0):
        if "/category" in url:
            return CATEGORY_HTML
        if "/search" in url or "/book" in url:
            return WORKS_HTML
        return ""


def main():
    source = SourceConfig.from_dict(SOURCE_RAW)
    http = MockHttp(sleeper=lambda _: None)
    parser = Parser()
    checker = StructureChecker(http, parser, default_strategy="off")
    disc = Discovery(http, parser, checker)

    print("解析引擎:", parser.engine())

    # 分类
    cats = disc.list_categories(source)
    assert [c.title for c in cats] == ["玄幻", "武侠", "都市"], cats
    assert cats[0].url == "http://example.com/cat/fantasy", cats[0].url
    print("分类:", [(c.title, c.url) for c in cats])

    # 作品列表
    works = disc.list_works(source, "/search")
    assert len(works) == 2, works
    assert works[0].title == "书一"
    assert works[0].url == "http://example.com/book/1"
    assert works[0].cover == "http://example.com/cover/1.jpg"
    print("作品:", [(w.title, w.url, w.cover) for w in works])

    print("\n=== 发现内核冒烟测试通过 ===")


if __name__ == "__main__":
    main()
