# -*- coding: utf-8 -*-
"""搜索合并语义测试：验证「结果被吞」修复（单页失败不再吞后续页）。

对应 framework/search.py _search_html 合并段的终止策略：
- 以「最后一个有结果的页」为真实终点，中段失败页跳过、后续页照常并入；
- 尾部空页（超出站点总页数）自然排除；
- max_results 为跨页总数上限，本页完整并入后再停。

通过 mock _http_get 返回预设 HTML 驱动真实 Search._search_html，
保证测试覆盖框架实际代码路径。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.config import SourceConfig
from framework.parser import Parser
from framework.search import Search

SOURCE_JSON = {
    "$schema_version": 2,
    "$id": "test-search-src",
    "$type": "video",
    "$name": "测试搜索源",
    "$enabled": True,
    "$weight": 1.0,
    "$metadata": {"homepage": "https://example.com/", "lang": "zh-CN", "region": "cn"},
    "transports": {"base_url": "https://example.com"},
    "endpoints": {
        "search": {
            "base_url": "/s/",
            "method": "GET",
            "keyword_param": "q",
            "paginator": {"url_template": "/s/?q={keyword}&p={page}"},
            "item": {
                "root_selector": {"css": "div.result-card"},
                "fields": {
                    "title": {"css": "h3 a"},
                    "url": {"css": "h3 a", "attr": "href"},
                },
            },
        }
    },
    "constraints": {"search": {"max_pages": 10, "max_results": 0}},
}


def _page_html(page: int, n: int = 24) -> str:
    """第 page 页的 n 条结果 HTML（URL 跨页不冲突）。"""
    cards = "".join(
        f'<div class="result-card"><h3><a href="/v/{page * 100000 + i}.html">'
        f"title-p{page}-{i}</a></h3></div>"
        for i in range(n)
    )
    return f"<html><body>{cards}</body></html>"


class _FakeHttp:
    """按 URL 中的 p= 页码返回预设 HTML；缺页抛异常模拟网络失败。"""

    def __init__(self, pages: dict):
        self._pages = pages  # page → html
        from framework.http import NetworkDefaults

        self.defaults = NetworkDefaults()

    def get_text(self, url, **kwargs):
        m = re.search(r"[?&]p=(\d+)", url)
        page = int(m.group(1)) if m else 1
        html = self._pages.get(page)
        if html is None:
            raise RuntimeError(f"page {page} fetch failed")
        return html

    def post_form(self, url, form_data=None, **kwargs):
        return self.get_text(url)


def _run_search(pages_with_items: set, max_pages: int = 10, max_results: int = 0,
                n: int = 24, empty_html: str = "<html><body></body></html>") -> list:
    """驱动真实 Search._search_html：有内容的页返回 n 条，其余页返回空 HTML。"""
    src = SourceConfig.from_dict(SOURCE_JSON, "<test>")
    src.raw["constraints"]["search"]["max_pages"] = max_pages
    src.raw["constraints"]["search"]["max_results"] = max_results
    pages = {
        p: (_page_html(p, n) if p in pages_with_items else empty_html)
        for p in range(1, max_pages + 1)
    }
    search = Search(http=_FakeHttp(pages), parser=Parser())
    return search._search_html(src, "kw")


def test_single_failed_page_does_not_swallow_tail():
    """第 4 页瞬时失败（请求异常）→ 后续页保留（修复前只出 3 页=72 条）。"""
    pages_with_items = {1, 2, 3, 5, 6, 7, 8, 9, 10}
    # 第 4 页缺失 → _FakeHttp.get_text 抛异常 → 该页 []，但 5-10 页必须保留
    src = SourceConfig.from_dict(SOURCE_JSON, "<test>")
    src.raw["constraints"]["search"]["max_pages"] = 10
    src.raw["constraints"]["search"]["max_results"] = 240
    pages = {p: _page_html(p) for p in pages_with_items}
    search = Search(http=_FakeHttp(pages), parser=Parser())
    results = search._search_html(src, "kw")
    assert len(results) == 216  # 9 页 × 24


def test_true_end_of_results_stops_at_last_nonempty():
    """站点只有 25 页，max_pages=30 → 尾部空页排除，出 600 条。"""
    results = _run_search(set(range(1, 26)), max_pages=30, max_results=0)
    assert len(results) == 600


def test_middle_failure_then_more_results_not_truncated():
    """第 4、5 页失败但第 6 页有内容 → 不被截断，出 192 条。"""
    src = SourceConfig.from_dict(SOURCE_JSON, "<test>")
    src.raw["constraints"]["search"]["max_pages"] = 10
    src.raw["constraints"]["search"]["max_results"] = 0
    pages = {p: _page_html(p) for p in (1, 2, 3, 6, 7, 8, 9, 10)}
    search = Search(http=_FakeHttp(pages), parser=Parser())
    results = search._search_html(src, "kw")
    assert len(results) == 192


def test_no_results_returns_empty():
    """关键词无结果（第 1 页即空）→ 返回空列表。"""
    results = _run_search(set(), max_pages=10, max_results=0)
    assert results == []


def test_max_results_cap_applied_after_full_page():
    """max_results=240 上限：整页并入后停，出 240 条（10 页整）。"""
    results = _run_search(set(range(1, 31)), max_pages=30, max_results=240)
    assert len(results) == 240


def test_first_page_only_success():
    """仅第 1 页成功，后续全失败 → 保留第 1 页 24 条。"""
    src = SourceConfig.from_dict(SOURCE_JSON, "<test>")
    src.raw["constraints"]["search"]["max_pages"] = 10
    src.raw["constraints"]["search"]["max_results"] = 0
    pages = {1: _page_html(1)}
    search = Search(http=_FakeHttp(pages), parser=Parser())
    results = search._search_html(src, "kw")
    assert len(results) == 24


def test_url_dedup_across_pages():
    """跨页 URL 去重：重复 URL 只保留一条。"""
    src = SourceConfig.from_dict(SOURCE_JSON, "<test>")
    src.raw["constraints"]["search"]["max_pages"] = 2
    src.raw["constraints"]["search"]["max_results"] = 0
    html1 = _page_html(1)
    html2 = _page_html(2)
    html2 = html2.replace(
        'href="/v/200000.html"', 'href="/v/100000.html"', 1
    )  # 第 2 页第 1 条与第 1 页第 1 条重复
    search = Search(http=_FakeHttp({1: html1, 2: html2}), parser=Parser())
    results = search._search_html(src, "kw")
    assert len(results) == 47  # 24 + 24 - 1 去重


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
