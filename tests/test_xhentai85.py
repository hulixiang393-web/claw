# -*- coding: utf-8 -*-
"""xhentai85（無限動漫H / 站内名「老司機禁漫·无尽漫画」xhentai85.xyz）源离线单元测试。

站点事实（2026-11 改版后实测，快照见 fixtures/）：
- GBK 编码 PHP 站，「查询串即路径」：作品 ``?comic/{id}``、章节
  ``?comic/{id}/{token}``、分类 ``?comics/{分类}/ob/{排序}/st/{进度}/page/{n}``。
- 旧路由 ``?novel{N}/``、``?latest/index_2.html`` 已 404（发现页整页空白的根因）。
- 边缘（aaPanel/nginx）对查询串里带 ``%`` 的请求一律 404，所以分类只能用 ASCII
  路由：中文分类 ``?comics/正妹/...``、中文搜索 ``?search/中文``、``/catalog.php?key=``
  当前站方不可用 → 本源不配搜索，分类全部为 ASCII + ``page/{n}``。
- 列表卡片：``div.comic-list > a[href^='?comic/']``，标题 ``div.title``，
  封面 ``picture img``（new.niaopic.com / thumb.niaopic.com）。
- 详情：标题 ``div.detail h1``、封面 ``div.detail-cover img``、
  作者 ``div.detail td a[href^='?search/']``、题材 ``div.detail td a[href^='?comics/']``、
  简介 ``div.mt-4.text-break``；章节 ``div.vol-item`` 内 ``span.vol-item-info a``。
- 正文：章节页 ``img[data-index]``（img.nnpic.xyz，无防盗链）。
- 章节在页面上是倒序（第N話 → 第1話），框架 chapter_sort 会恢复正序。

全部离线（真实页面快照 + FakeHttp），不发网络请求。
"""
from __future__ import annotations

import json
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.http import NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "xhentai85")
_BASE = "https://xhentai85.xyz"
_DETAIL_URL = _BASE + "/?comic/2687"
_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources", "xhentai85.json"
)


class _FakeChecker:
    pass


class _FakeHttp:
    """离线假 HttpClient：按 URL 特征返回对应 fixture 快照。"""

    cache = None
    _sleeper = staticmethod(lambda *_a: None)

    def __init__(self, sleeper=None, defaults=None):
        self.defaults = defaults or NetworkDefaults(timeout=15, retries=0, interval_ms=0)
        self.calls = []

    def get_text(self, url, headers=None, timeout=None, retries=None,
                 interval_ms=None, encoding=None, proxy_pool=None, direct=False):
        self.calls.append(url)
        if "/chapter" in url or re.search(r"\?comic/\d+/[A-Za-z0-9]{10,}", url):
            name = "chapter1.html"
        elif re.search(r"\?comic/\d+", url):
            name = "detail.html"
        else:
            name = "list.html"
        with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
            return f.read()

    def close(self):
        pass


def _load(name: str) -> str:
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _source() -> SourceConfig:
    with open(_SOURCE_PATH, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), _SOURCE_PATH)


def _content(http=None):
    return Content(http or _FakeHttp(), Parser(), _FakeChecker())


def _discovery(http=None):
    return Discovery(http or _FakeHttp(), Parser(), _FakeChecker())


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_source_config_loads():
    src = _source()
    assert src.source_id == "xhentai85"
    assert src.content_type == "comic"
    assert src.base_url == _BASE
    assert src.transports().get("charset") == "gbk"
    assert src.transports().get("interval_ms") == 700
    assert src.request_headers().get("User-Agent", "").startswith("Mozilla")
    assert src.get_detail_config() is not None
    assert src.get_discovery_config().get("works_list_item") is not None


def test_selfcheck_off():
    src = _source()
    assert ((src.raw.get("diagnostics") or {}).get("selfcheck") or {}).get("strategy") == "off"


def test_no_search_endpoint():
    """站方中文搜索在边缘 404 → 本源不配搜索（GUI 回空而不是报错）。"""
    src = _source()
    assert not src.get_search_config()


# --------------------------------------------------------------------------- #
# 发现：分类与分页
# --------------------------------------------------------------------------- #
def test_categories_are_ascii_and_paginated():
    src = _source()
    disc = src.get_discovery_config()
    cats = ((disc.get("list_item") or {}).get("categories") or [])
    assert len(cats) >= 8
    for c in cats:
        url = c["url"]
        # 查询串里不能出现 %（站点边缘会 404），也不能有非 ASCII
        assert "%" not in url, url
        assert all(ord(ch) < 128 for ch in url), url
        assert "/page/{page}" in url, url
    titles = [c["title"] for c in cats]
    assert "最新更新" in titles and "已完結" in titles


def test_pagination_url_build():
    """list_works(page=n) 必须请求 …/page/{n}（第 1 页也要带 page/1）。"""
    src = _source()
    http = _FakeHttp()
    cat = src.get_discovery_config()["list_item"]["categories"][0]["url"]
    disc = _discovery(http)
    disc.list_works(src, cat, 1)
    disc.list_works(src, cat, 3)
    assert http.calls[0].endswith("/page/1"), http.calls[0]
    assert http.calls[-1].endswith("/page/3"), http.calls[-1]


# --------------------------------------------------------------------------- #
# 发现：卡片解析
# --------------------------------------------------------------------------- #
def test_discovery_works_extract():
    src = _source()
    works = _discovery().list_works(
        src, src.get_discovery_config()["list_item"]["categories"][0]["url"], 1
    )
    assert len(works) == 20
    first = works[0]
    assert first.title
    assert first.url.startswith(_BASE + "?comic/"), first.url
    assert re.search(r"\?comic/\d+$", first.url), first.url
    assert first.cover.startswith("https://")
    assert "niaopic.com" in first.cover
    # update 字段经过 clean：只剩日期，不带评分
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", first.update or ""), first.update
    assert all(w.title and w.url for w in works)
    assert len({w.url for w in works}) == len(works)


def test_discovery_raw_items():
    doc = Parser().parse(_load("list.html"))
    src = _source()
    wli = src.get_discovery_config()["works_list_item"]
    items = Parser().parse_items(doc, wli["root_selector"], wli["fields"], _BASE)
    assert len(items) == 20
    assert items[0]["url"].startswith(_BASE + "?comic/")
    assert items[0]["cover"].startswith("https://")


# --------------------------------------------------------------------------- #
# 详情
# --------------------------------------------------------------------------- #
def test_detail_fields_extract():
    src = _source()
    doc = Parser().parse(_load("detail.html"))
    p = Parser()
    fields = src.get_detail_config()["fields"]
    title = p.extract_first(doc, fields["title"])
    assert title == "讓上司成為裙下之臣"
    cover = p.extract_first(doc, fields["cover"], _BASE)
    assert cover.startswith("https://new.niaopic.com/")
    author = p.extract_first(doc, fields["author"])
    assert author
    summary = p.extract_first(doc, fields["summary"])
    assert "辦公室" in summary
    tags = p.extract(doc, fields["tags"])
    assert "正妹" in tags and "巨乳" in tags


def test_fetch_detail():
    src = _source()
    d = _content().fetch_detail(src, _DETAIL_URL)
    assert d.title == "讓上司成為裙下之臣"
    assert d.cover.startswith("https://")
    assert d.author
    assert d.summary
    assert d.tags and "正妹" in d.tags
    assert len(d.chapters) >= 5


def test_fetch_detail_chapters_sorted_ascending():
    """页面章节倒序（第N話 → 第1話），框架排序后必须是正序。"""
    src = _source()
    d = _content().fetch_detail(src, _DETAIL_URL)
    nums = []
    for ch in d.chapters:
        m = re.search(r"chapter(\d+)", ch.title) or re.search(r"第(\d+)[話话]", ch.title)
        if m:
            nums.append(int(m.group(1)))
    assert len(nums) >= 5
    assert nums == sorted(nums)
    assert d.chapters[0].title.startswith("第1")
    assert all(_DETAIL_URL.split("?")[1] in ch.url for ch in d.chapters)
    assert all(ch.url.count("?comic/2687/") == 1 for ch in d.chapters)


# --------------------------------------------------------------------------- #
# 正文图片
# --------------------------------------------------------------------------- #
def test_fetch_comic_pages():
    src = _source()
    d = _content().fetch_detail(src, _DETAIL_URL)
    pages = _content().fetch_comic_pages(src, d.chapters[0].url)
    assert len(pages) >= 100
    for pg in pages[:5]:
        assert pg.startswith("https://img.nnpic.xyz/"), pg
        assert pg.endswith(".jpg")
    assert len(set(pages)) == len(pages)


def test_body_selector_hits_data_index_images():
    doc = Parser().parse(_load("chapter1.html"))
    src = _source()
    body = ((src.raw.get("endpoints") or {}).get("content") or {}).get("page", {}).get("body")
    items = Parser().parse_items(doc, body["root_selector"], body["fields"], _BASE)
    urls = [it.get("url") for it in items]
    assert len(urls) >= 100
    assert all(u and u.startswith("https://img.nnpic.xyz/") for u in urls)
