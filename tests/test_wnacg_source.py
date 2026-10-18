# -*- coding: utf-8 -*-
"""wnacg（绅士漫画 wnacg.com）源离线单元测试。

覆盖（全部基于真实页面 HTML 快照，无网络请求）：
- 源配置加载关键形态：comic + single_chapter、详情页阅读页入口 entry_link_selector、
  阅读页内容图 root_selector=div.posselect、下一頁翻页 xpath、分页上限
- fetch_detail：single_chapter → 唯一章节 url == 详情页 URL
- fetch_comic_pages：详情页 → 解析阅读页入口 → 抓取该话内容图（保留 ?verify 签名），
  下一頁翻页跨页拼全（read1 → read2），末页停止
- 通用能力向后兼容：未配置 entry_link_selector 的漫画源行为不变（直接抓 chapter_url）
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.http import NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "wnacg")
_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources", "wnacg.json"
)
_BASE = "https://www.wnacg.com"
_DETAIL = _BASE + "/photos-index-aid-396496.html"
_READ1 = _BASE + "/photos-view-id-34797789.html"
_READ2 = _BASE + "/photos-view-id-34797790.html"
_IMG1 = "https://img5.qy0.ru/data/3964/96/001_1.webp?verify=1792260000-sASqOjp54NZ83u6L1fmlwKVLLYBQe7li4W6ii2k3VHA"
_IMG2 = "https://img5.qy0.ru/data/3964/96/002.jpg?verify=1792260000-qQAqTtjfiwTe4Yauf5ueIt3iJO6_r8_CJnuqcDu6gXQ"


class _FakeChecker:
    pass


def _read_fixture(name: str) -> str:
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class _FakeHttp:
    """离线假 HttpClient：按 URL 特征返回详情/阅读页快照。"""

    cache = None
    _sleeper = staticmethod(lambda *_a: None)

    def __init__(self, sleeper=None, defaults=None):
        self.defaults = defaults or NetworkDefaults(timeout=15, retries=0, interval_ms=0)
        self.calls = []

    def get_text(self, url, **kw):
        self.calls.append(url)
        if "photos-view-id-34797789" in url:
            return _read_fixture("read1.html")
        if "photos-view-id-34797790" in url:
            return _read_fixture("read2.html")
        if "photos-index-aid" in url:
            return _read_fixture("detail.html")
        return ""

    def close(self):
        pass


def _source() -> SourceConfig:
    with open(_SOURCE_PATH, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), _SOURCE_PATH)


def _content(http=None):
    return Content(http or _FakeHttp(), Parser(), _FakeChecker())


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_source_config_is_single_chapter_comic():
    src = _source()
    assert src.content_type == "comic"
    page = src.raw["endpoints"]["content"]["page"]
    assert page.get("single_chapter") is True


def test_entry_link_selector_config():
    src = _source()
    page = src.raw["endpoints"]["content"]["page"]
    entry = page["list"].get("entry_link_selector")
    assert entry and entry.get("css") == "li.gallary_item div.pic_box a"
    assert entry.get("attr") == "href"
    # 内容图选择器指向阅读页主图容器，不再依赖旧缩略图 → 原图的 {seq} 重写
    assert page["list"]["root_selector"]["css"] == "div.posselect"
    url_field = page["list"]["fields"]["url"]
    assert url_field["css"] == "img" and url_field["attr"] == "src"
    assert "url_replace" not in url_field


def test_next_link_and_max_pages_config():
    src = _source()
    dc = src.raw["endpoints"]["content"]["page"]["list"]
    nxt = dc["paginator"]["next_link"]["selector"]
    assert "btntuzao" in nxt["xpath"] and "下一頁" in nxt["xpath"]
    assert dc["paginator"]["max_pages"] >= 200


# --------------------------------------------------------------------------- #
# 详情 / 阅读页
# --------------------------------------------------------------------------- #
def test_fetch_detail_returns_single_chapter_at_detail_url():
    src = _source()
    d = _content().fetch_detail(src, _DETAIL)
    assert d.chapters, "single_chapter 图集应有唯一章节"
    assert len(d.chapters) == 1
    assert d.chapters[0].url == _DETAIL


def test_fetch_comic_pages_resolves_entry_and_paginates():
    """详情页 → 阅读页入口 → 内容图（保留 verify），下一頁拼全两页后停止。"""
    src = _source()
    http = _FakeHttp()
    c = _content(http)
    imgs = c.fetch_comic_pages(src, _DETAIL, use_cache=False)
    assert imgs == [_IMG1, _IMG2]
    # 抓取的是阅读页 URL，而非详情页
    assert http.calls[0].endswith("/photos-index-aid-396496.html")
    assert any(u.endswith("/photos-view-id-34797789.html") for u in http.calls)
    assert any(u.endswith("/photos-view-id-34797790.html") for u in http.calls)
    # verify 签名原样保留
    assert all("?verify=" in u for u in imgs)


def test_entry_resolution_falls_back_when_selector_misses():
    """入口选择器命中为空 → 保持原 chapter_url（不整话失败）。"""
    src = _source()
    page = src.raw["endpoints"]["content"]["page"]["list"]
    page["entry_link_selector"] = {"css": "div.no-such-entry a", "attr": "href"}
    http = _FakeHttp()
    c = _content(http)
    imgs = c.fetch_comic_pages(src, _DETAIL, use_cache=False)
    assert imgs == []  # 详情页无 div.posselect → 空；但未抛异常


# --------------------------------------------------------------------------- #
# 通用能力向后兼容
# --------------------------------------------------------------------------- #
def test_entry_selector_default_off_keeps_existing_behavior():
    """未配置 entry_link_selector 的漫画源：直接抓 chapter_url，零额外请求。"""
    raw = {
        "$schema_version": 2,
        "$id": "plain-comic",
        "$type": "comic",
        "$name": "普通漫画",
        "$enabled": True,
        "$weight": 1.0,
        "transports": {"base_url": "https://c.example"},
        "endpoints": {
            "content": {
                "page": {
                    "list": {
                        "root_selector": {"css": "div.page"},
                        "fields": {"url": {"css": "img", "attr": "src"}},
                    }
                }
            }
        },
    }
    src = SourceConfig.from_dict(raw, "<mem>")
    url = "https://c.example/ch/1.html"

    class _PlainHttp(_FakeHttp):
        def get_text(self, u, **kw):
            self.calls.append(u)
            return '<div class="page"><img src="https://c.example/1.jpg"/></div>'

    http = _PlainHttp()
    c = _content(http)
    imgs = c.fetch_comic_pages(src, url, use_cache=False)
    assert imgs == ["https://c.example/1.jpg"]
    assert http.calls == [url]  # 无入口解析 → 只请求一次


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
