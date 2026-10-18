# -*- coding: utf-8 -*-
"""三个公开 HTML 小说源的离线配置与解析测试。"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from urllib.parse import urlsplit

from framework.config import SourceConfig, load_source
from framework.content import Content
from framework.discovery import Discovery
from framework.parser import Parser
from framework.search import Search
from framework.source_manager import SourceManager


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class _FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.defaults = type(
            "Defaults",
            (),
            {"timeout": 10, "retries": 0, "interval_ms": 0},
        )()

    def get_text(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        for matcher, body in self.routes:
            if matcher(url):
                return body
        raise AssertionError(f"unexpected fake GET: {url}")


def _fixture(source_id: str, name: str) -> str:
    return (FIXTURES / source_id / name).read_text(encoding="utf-8")


def _source(source_id: str) -> SourceConfig:
    return load_source(SOURCES / f"{source_id}.json")


def _path_is(path: str):
    return lambda url: urlsplit(url).path == path


def _starts_with(prefix: str):
    return lambda url: url.startswith(prefix)


def _content(http: _FakeHttp) -> Content:
    return Content(http, Parser(), object())


def _without_browser_render(source: SourceConfig) -> SourceConfig:
    """Use fixture HTML directly; browser behavior has its own regression test."""
    raw = copy.deepcopy(source.raw)
    raw["endpoints"]["discovery"].pop("render", None)
    raw["endpoints"]["search"].pop("render", None)
    raw["endpoints"]["detail"].pop("render", None)
    raw["endpoints"]["content"]["chapter"]["body"].pop("render", None)
    return SourceConfig.from_dict(raw, source.source_path)


def test_public_sources_load_through_source_manager():
    manager = SourceManager(SOURCES)

    assert [manager.get(source_id).source_id for source_id in (
        "bookstack", "shuyous"
    )] == ["bookstack", "shuyous"]
    for source_id in ("bookstack", "shuyous"):
        source = manager.get(source_id)
        assert source.content_type == "novel"
        assert source.enabled is True


def test_bookstack_config_has_low_frequency_discovery_and_search_contract():
    source = _source("bookstack")
    discovery = source.get_discovery_config()
    search = source.get_search_config()
    detail = source.get_detail_config()

    assert source.base_url == "https://www.bookstack.cn"
    assert discovery["list_url"] == "/explore?tab=latest&lang="
    assert discovery["render"] == "playwright"
    assert discovery["render_config"]["wait_for"] == "dl.manual-item-standard"
    assert discovery["works_list_item"]["root_selector"] == {
        "css": "dl.manual-item-standard"
    }
    assert search["base_url"] == "/search/result"
    assert search["method"] == "GET"
    assert search["keyword_param"] == "wd"
    assert search["render"] == "playwright"
    assert search["item"]["render_config"]["wait_for"] == ".result-list li.clearfix"
    assert search["extra_params"] == {"tab": "book"}
    assert search["item"]["root_selector"] == {
        "css": ".result-list li.clearfix"
    }
    assert search["paginator"]["url_template"] == "/search/result?wd={keyword}&tab=book&page={page}"
    assert source.transports()["interval_ms"] == 250
    assert source.transports()["retries"] == 1
    assert source.transports()["headers"]["User-Agent"].startswith("Mozilla/5.0")
    assert source.raw["diagnostics"]["selfcheck"]["strategy"] == "off"
    detail_fields = source.raw["endpoints"]["detail"]["fields"]
    assert detail_fields["title"]["fallback"][0] == {"css": "h1.wd-book-title"}
    assert detail_fields["cover"]["fallback"][0] == {
        "css": ".wd-cover-img", "attr": "src"
    }
    assert detail["render"] == "http"
    assert detail["render_config"]["wait_for"] == (
        "h1.wd-book-title, .m-manual.manual-mode-view .article-menu-detail, "
        "article#page-content"
    )
    assert source.raw["detail_cache_version"] == 3
    assert source.raw["chapter_cache_version"] == 3
    assert source.raw["page_cache_version"] == 3
    assert source.raw["endpoints"]["content"]["chapter"]["body"]["selector"]["fallback"][0] == {
        "css": "article.article-body.markdown-body"
    }
    body = source.raw["endpoints"]["content"]["chapter"]["body"]
    assert body["render"] == "http"
    assert body["render_config"]["wait_for"] == (
        "article.article-body.markdown-body, article#page-content"
    )


def test_bookstack_discovery_search_detail_chapters_and_body():
    source = _without_browser_render(_source("bookstack"))
    http = _FakeHttp(
        [
            (
                _path_is("/explore"),
                _fixture("bookstack", "discovery.html"),
            ),
            (
                _starts_with("https://www.bookstack.cn/search/result"),
                _fixture("bookstack", "search.html"),
            ),
            (
                _path_is("/books/python-guide"),
                _fixture("bookstack", "detail.html"),
            ),
            (
                _path_is("/books/python-guide/intro"),
                _fixture("bookstack", "chapter.html"),
            ),
        ]
    )
    parser = Parser()

    works = Discovery(http, parser, object()).list_works(
        source, source.get_discovery_config()["list_url"]
    )
    assert len(works) == 1
    assert works[0].title == "Python 指南"
    assert works[0].url == "https://www.bookstack.cn/books/python-guide"
    assert works[0].cover == "https://www.bookstack.cn/static/guide.png"

    results = Search(http, parser).search_one(source, "python")
    assert len(results) == 1
    assert results[0].title == "Python 指南"
    assert results[0].url == "https://www.bookstack.cn/books/python-guide"
    search_urls = [url for method, url, _ in http.calls if method == "GET"]
    assert any(
        url == "https://www.bookstack.cn/search/result?wd=python&tab=book&page=1"
        for url in search_urls
    )

    content = _content(http)
    detail = content.fetch_detail(source, "/books/python-guide")
    assert detail.title == "Python 指南"
    assert detail.author == "文档团队"
    assert detail.cover == "https://www.bookstack.cn/static/python.png"
    assert detail.summary == "脱敏的 Python 文档简介。"
    assert [chapter.title for chapter in detail.chapters] == [
        "第1章 入门", "第2章 进阶"
    ]
    assert detail.chapters[0].url == (
        "https://www.bookstack.cn/books/python-guide/intro"
    )
    body = content.fetch_chapter(source, detail.chapters[0].url)
    assert "安装 Python" in body
    assert "开始编写代码" in body


def test_18mh_novel_chapters_keep_site_number_for_common_sorting():
    source = _source("18mh-novel")
    chapter_list = source.raw["endpoints"]["content"]["chapter"]["list"]

    assert chapter_list["chapter_order"] == "asc"
    assert chapter_list["fields"]["title"] == {
        "css": ".detail-page__catalog-left"
    }


def test_shuyous_config_has_discovery_detail_toc_body_and_same_chapter_rule():
    source = _source("shuyous")
    discovery = source.get_discovery_config()
    chapter = source.raw["endpoints"]["content"]["chapter"]
    pagination = chapter["pagination"]

    assert source.base_url == "https://www.shuyous.com"
    assert discovery["list_url"] == "/list-18-1/"
    assert discovery["works_list_item"]["root_selector"] == {
        "css": "div.list2 ul li"
    }
    assert discovery["works_list_item"]["fields"]["title"] == {
        "css": ".name a"
    }
    assert discovery["works_list_item"]["fields"]["author"] == {
        "css": ".author"
    }
    assert discovery["works_list_item"]["fields"]["update"] == {
        "css": ".date"
    }
    assert source.get_search_config() == {}
    assert chapter["list"]["root_selector"] == {
        "css": "div.chapterList div.list ul li"
    }
    assert chapter["body"]["selector"] == {"css": "div.content#content p"}
    assert pagination["same_chapter_regex"]
    assert re.search(
        pagination["same_chapter_regex"], "/book/4406993-35-2.html"
    )
    assert re.search(
        pagination["same_chapter_regex"], "/book/4406993-35.html"
    )
    assert source.transports()["interval_ms"] == 1500
    assert source.transports()["retries"] == 1
    assert source.raw["diagnostics"]["selfcheck"]["strategy"] == "off"


def test_shuyous_discovery_detail_chapters_and_paginated_body():
    source = _source("shuyous")
    first_url = "https://www.shuyous.com/book/4406993-35.html"
    continuation_url = "https://www.shuyous.com/book/4406993-35-2.html"
    http = _FakeHttp(
        [
            (
                _path_is("/list-18-1/"),
                _fixture("shuyous", "discovery.html"),
            ),
            (
                _path_is("/book/demo-book.html"),
                _fixture("shuyous", "detail.html"),
            ),
            (
                lambda url: url == first_url,
                _fixture("shuyous", "chapter-35.html"),
            ),
            (
                lambda url: url == continuation_url,
                _fixture("shuyous", "chapter-35-2.html"),
            ),
        ]
    )
    parser = Parser()

    works = Discovery(http, parser, object()).list_works(
        source, source.get_discovery_config()["list_url"]
    )
    assert len(works) == 1
    assert works[0].title == "脱敏长篇"
    assert works[0].url == "https://www.shuyous.com/book/demo-book.html"
    assert works[0].author == "作者甲"
    assert works[0].update == "2026-10-06"

    content = _content(http)
    detail = content.fetch_detail(source, "/book/demo-book.html")
    assert detail.title == "脱敏长篇"
    assert detail.author == "作者甲"
    assert detail.cover == "https://www.shuyous.com/static/demo.jpg"
    assert detail.summary == "一段脱敏简介。"
    assert [chapter.title for chapter in detail.chapters] == [
        "第35章", "第36章"
    ]
    assert detail.chapters[0].url == first_url

    body = content.fetch_chapter(source, first_url)
    assert body == "第一页正文\n续页正文"
    assert [url for method, url, _ in http.calls if method == "GET"][-2:] == [
        first_url, continuation_url
    ]

