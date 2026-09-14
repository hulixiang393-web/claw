# -*- coding: utf-8 -*-
"""搜索页滚动懒加载节流/防跳动回归测试（test_search_page_scroll_guard.py）。

覆盖修复「滚动加载卡顿 + 加载中内容上下自动滚动」：
1. _on_scroll 把同一事件循环内的连续滚动事件合并为**一次**铺批
   （旧代码 _more_pending 从不置位 → 每个 valueChanged 都同步铺一批并
   排队一个 _finish_more_anchor，快速滚动时多批/多锚点互相 setValue → 卡顿+跳动）；
2. 流式追加（_append_results）只补首屏，**绝不**触发 _load_more_results
   （即不主动改变滚动条位置）；
3. _maybe_preload_results 只同步补首屏，绝不调用 _load_more_results；
4. _clear_grid 复位合并标记与列拉伸缓存。

离屏运行，_make_card 用轻量 QFrame 替身。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from PySide6.QtWidgets import QApplication, QFrame

from framework.config import SourceConfig
from framework.parser import Parser
from framework.search import Search
from framework.source_manager import SourceManager
from gui.pages.search_page import SearchPage

SRC = {
    "$schema_version": 2,
    "$id": "site_a",
    "$type": "novel",
    "$name": "站点A",
    "transports": {"base_url": "http://x.com"},
    "endpoints": {
        "search": {
            "base_url": "/search",
            "method": "GET",
            "item": {
                "root_selector": {"css": ".item"},
                "fields": {"title": {"css": ".t"}, "url": {"css": ".t", "attr": "href"}},
            },
        }
    },
}


class _Result:
    def __init__(self, i):
        self.title = f"标题{i}"
        self.url = f"http://x.com/v/{i}.html"
        self.source_id = "site_a"
        self.cover = ""
        self.author = ""
        self.source_name = "站点A"
        self.update = ""


def _results(n, base=0):
    return [_Result(base + i) for i in range(n)]


class _FakeBar:
    def __init__(self, maximum):
        self._maximum = maximum

    def maximum(self):
        return self._maximum


class _FakeScroll:
    def __init__(self, maximum):
        self._bar = _FakeBar(maximum)

    def verticalScrollBar(self):
        return self._bar


@pytest.fixture(scope="module")
def app(_qapp):
    return _qapp


def _make_page():
    mgr = SourceManager()
    mgr.add(SourceConfig.from_dict(SRC))
    page = SearchPage(mgr, Search(http=None, parser=Parser()))
    page._make_card = lambda r: QFrame()
    return page


# ------------------------------------------------------------------------- #
# 1. 连续滚动事件合并为一次铺批
# ------------------------------------------------------------------------- #
def test_on_scroll_coalesces_burst_into_one_batch(app):
    page = _make_page()
    page._results = _results(page._page_size * 5)
    page._shown_count = 0
    calls = []
    page._load_more_results = lambda: calls.append(1)
    page.scroll = _FakeScroll(100)

    page._on_scroll(90)
    page._on_scroll(95)
    page._on_scroll(99)

    assert page._more_pending is True
    assert calls == []  # 延到下一事件循环，不在一帧内连续铺批

    app.processEvents()
    assert calls == [1]  # 三次滚动事件合并为一次
    assert page._more_pending is False


def test_on_scroll_below_threshold_does_nothing(app):
    page = _make_page()
    page._results = _results(page._page_size * 5)
    page._shown_count = 0
    calls = []
    page._load_more_results = lambda: calls.append(1)
    page.scroll = _FakeScroll(100)

    page._on_scroll(10)
    app.processEvents()

    assert calls == []
    assert page._more_pending is False


def test_on_scroll_suppressed_while_restoring(app):
    page = _make_page()
    page._results = _results(page._page_size * 5)
    page._shown_count = 0
    calls = []
    page._load_more_results = lambda: calls.append(1)
    page.scroll = _FakeScroll(100)
    page._restoring_scroll = True

    page._on_scroll(99)
    app.processEvents()

    assert calls == []


# ------------------------------------------------------------------------- #
# 2/3. 流式追加 / 首屏补足绝不触发带锚定的 _load_more_results
# ------------------------------------------------------------------------- #
def test_streaming_append_never_triggers_load_more(app):
    page = _make_page()
    page._results = []
    page._shown_count = 0
    calls = []
    page._load_more_results = lambda: calls.append(1)

    for pg in range(6):  # 6 页 × 6 条，远超首屏
        batch = _results(6, base=pg * 6)
        page._results.extend(batch)
        page._append_results(batch)

    app.processEvents()
    assert calls == []  # 流式追加期间不做任何滚动锚定
    assert page._shown_count <= page._first_screen_limit()


def test_maybe_preload_only_fills_first_screen_without_load_more(app):
    page = _make_page()
    page._results = _results(100)
    page._shown_count = 0
    calls = []
    page._load_more_results = lambda: calls.append(1)

    page._maybe_preload_results()
    app.processEvents()

    assert calls == []  # 不调度 _load_more_results
    assert page._shown_count == page._first_screen_limit()


def test_append_results_capped_at_first_screen(app):
    page = _make_page()
    page._results = _results(200)
    page._shown_count = 0
    page._append_results(page._results)

    assert page._shown_count == page._first_screen_limit()
    assert page.grid_layout.count() == page._shown_count


# ------------------------------------------------------------------------- #
# 4. _clear_grid 复位状态
# ------------------------------------------------------------------------- #
def test_clear_grid_resets_more_pending_and_columns(app):
    page = _make_page()
    page._results = _results(5)
    page._append_results(page._results)
    page._more_pending = True
    page._last_columns = 4

    page._clear_grid()

    assert page._more_pending is False
    assert page._last_columns == 0
    assert page.grid_layout.count() == 0
    assert page._work_count == 0


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
