# -*- coding: utf-8 -*-
"""漫画/详情缓存重开命中测试（test_comic_pages_cache.py）。

覆盖「打开收藏的书不再重复网络加载」链路：
- fetch_comic_pages：pages: 键（7 天）二次命中零网络，URL 列表一致
- fetch_cover：复用 page: 详情缓存，重启（清内存缓存）后零网络回补封面
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from framework.config import SourceConfig
from framework.parser import Parser

COMIC_RAW = {
    "$schema_version": 2,
    "$id": "comic-src",
    "$type": "comic",
    "$name": "漫画源",
    "$enabled": True,
    "$weight": 1.0,
    "transports": {"base_url": "https://c.example"},
    "endpoints": {
        "content": {
            "page": {
                "list": {
                    "root_selector": {"css": "div.page"},
                    "fields": {"url": {"css": "img", "attr": "data-src"}},
                }
            }
        }
    },
}

DETAIL_RAW = {
    "$schema_version": 2,
    "$id": "novel-src",
    "$type": "novel",
    "$name": "小说源",
    "$enabled": True,
    "$weight": 1.0,
    "transports": {"base_url": "https://x.example"},
    "endpoints": {
        "detail": {"fields": {"cover": {"css": ".cover img", "attr": "src"}}}
    },
}


class _FakeChecker:
    pass


class _FakeHttp:
    """计数式假 HttpClient：漫画页/详情页返回固定 HTML（可区分 URL 段）。"""

    cache = None

    def __init__(self):
        self.calls = {}
        from framework.http import NetworkDefaults

        self.defaults = NetworkDefaults()

    def get_text(self, url, **kw):
        self.calls[url] = self.calls.get(url, 0) + 1
        if "/ch/" in url:
            return (
                '<div class="page"><img data-src="https://c.example/pic/1.jpg"/></div>'
                '<div class="page"><img data-src="https://c.example/pic/2.jpg"/></div>'
            )
        return '<div class="cover"><img src="/cover/c1.jpg"/></div>'

    def close(self):
        pass


def _make_store():
    from framework.cache_service import RedisLikeStore

    return RedisLikeStore(quota=1024 * 1024, persist_path=None)


def _make_content(store, http):
    from framework.content import Content

    return Content(http, Parser(), _FakeChecker(), cache=store)


# ---------------------------------------------------------------------- #
def test_comicbox_split_chapter_links_parse_from_anchor_roots():
    raw = {
        "$schema_version": 2,
        "$id": "comicbox",
        "$type": "comic",
        "$name": "漫画盒",
        "$enabled": True,
        "$weight": 1.0,
        "transports": {"base_url": "https://www.comicbox.xyz"},
        "endpoints": {
            "content": {
                "page": {
                    "list": {
                        "root_selector": {"css": ".sp-chapter-grid a.sp-chapter-item"},
                        "fields": {
                            "title": {"xpath": ".", "attr": "title"},
                            "url": {"xpath": ".", "attr": "href"},
                        },
                    }
                }
            }
        },
    }
    src = SourceConfig.from_dict(raw, "<mem>")
    parser = Parser()
    html = (
        '<div class="sp-chapter-grid">'
        '<a class="sp-chapter-item" title="第1話（1/3）" href="/free-chapter/45812?t=20260415">'
        '第1話（1/3）</a>'
        '<a class="sp-chapter-item" title="第1話（2/3）" href="/free-chapter/45813?t=20260415">'
        '第1話（2/3）</a></div>'
    )
    cfg = src.raw["endpoints"]["content"]["page"]["list"]
    rows = parser.parse_items(parser.parse(html), cfg["root_selector"], cfg["fields"], src.base_url)
    assert [row["title"] for row in rows] == ["第1話（1/3）", "第1話（2/3）"]
    assert rows[0]["url"] == "https://www.comicbox.xyz/free-chapter/45812?t=20260415"
    assert rows[1]["url"] == "https://www.comicbox.xyz/free-chapter/45813?t=20260415"


def test_fetch_comic_pages_second_hit_zero_network():
    store = _make_store()
    http = _FakeHttp()
    c = _make_content(store, http)
    src = SourceConfig.from_dict(COMIC_RAW, "<mem>")
    url = "https://c.example/ch/1.html"

    imgs = c.fetch_comic_pages(src, url)
    assert imgs == ["https://c.example/pic/1.jpg", "https://c.example/pic/2.jpg"]
    assert http.calls.get(url, 0) == 1
    key = f"pages:v3:{src.source_id}:{url}"
    assert store.get(key) == imgs  # URL 列表已落 pages: 键（7 天）

    # 二次（模拟重启后/重开同一章）：直接命中 Redis，不再下载
    imgs2 = c.fetch_comic_pages(src, url)
    assert imgs2 == imgs
    assert http.calls.get(url, 0) == 1  # 新增零网络


def test_nonfavorite_comic_cache_bypasses_reads_and_writes():
    store = _make_store()
    http = _FakeHttp()
    c = _make_content(store, http)
    src = SourceConfig.from_dict(COMIC_RAW, "<mem>")
    url = "https://c.example/ch/1.html"
    key = f"pages:v3:{src.source_id}:{url}"
    store.set(key, ["https://c.example/stale.jpg"], ttl=3600)

    imgs = c.fetch_comic_pages(src, url, use_cache=False)

    assert imgs == ["https://c.example/pic/1.jpg", "https://c.example/pic/2.jpg"]
    assert http.calls.get(url, 0) == 1
    assert store.get(key) == ["https://c.example/stale.jpg"]


def test_fetch_cover_reuses_detail_cache():
    store = _make_store()
    http = _FakeHttp()
    c = _make_content(store, http)
    src = SourceConfig.from_dict(DETAIL_RAW, "<mem>")
    url = "https://x.example/book/1.html"

    cover = c.fetch_cover(src, url)
    assert cover == "https://x.example/cover/c1.jpg"
    assert http.calls.get(url, 0) == 1
    key = f"page:{src.source_id}:{url}"
    assert store.get(key) is not None  # 详情页已落 page: 键（永久）

    # 清内存详情缓存模拟重启 → Redis 命中回补封面，零网络
    c._detail_html_cache.clear()
    cover2 = c.fetch_cover(src, url)
    assert cover2 == cover
    assert http.calls.get(url, 0) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
