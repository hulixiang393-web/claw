# -*- coding: utf-8 -*-
"""搜索页滚动懒加载分批/节流回归测试（test_search_page_lazyload.py）。

验证修复「下滑加载显示非常卡顿」：
- _load_more_results 一次调用最多同步铺 _page_size 张（不做整批 12+ 同步建卡）；
  剩余结果置 _more_pending，由 QTimer 续铺（flush 链），滚动 valueChanged 高频
  触发时不重入。
- _clear_grid 中止旧分批链（_more_pending 复位 + 封面节流 timer 停止）。
- 封面 pump 节流：滚动时会合并（不每帧全量 mapTo）。

离屏运行，通过记录 _append_card 调用数验证分批边界（不依赖真实 WorkCard）。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

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


def make_page():
    mgr = SourceManager()
    mgr.add(SourceConfig.from_dict(SRC))
    return SearchPage(mgr, Search(http=None, parser=Parser()))


class SearchResultLike:
    def __init__(self, i):
        self.title = f"标题{i}"
        self.url = f"http://x.com/v/{i}.html"
        self.source_id = "site_a"
        self.cover = ""


def _results(n):
    return [SearchResultLike(i) for i in range(n)]


@pytest.fixture()
def app():
    return QApplication.instance() or QApplication([])


def test_load_more_returns_when_no_more(app):
    """全部已渲染 → _load_more_results 不建卡（_more_pending 不置位）。"""
    page = make_page()
    page._results = _results(3)
    page._shown_count = 3
    calls = []
    page._append_card = lambda r, cols: calls.append(1)
    page._load_more_results()
    assert calls == []
    assert page._more_pending is False


def test_load_more_capped_per_batch(app):
    """一次 _load_more_results 只铺 _page_size 张（不链式铺完剩余）。"""
    page = make_page()
    page._results = _results(page._page_size * 3)
    page._shown_count = 0
    calls = []
    page._append_card = lambda r, cols: calls.append(1)
    page._apply_column_stretch = lambda cols: None
    page._update_batch_status = lambda: None
    page._maybe_preload_results = lambda: None
    page._load_more_results()
    assert len(calls) == page._page_size  # 只铺一批，不一次性建全部
    assert page._shown_count == page._page_size


def test_load_more_does_not_drain_all_on_one_scroll(app):
    """滚动 80% 触发一次只铺一批；再触发多次（模拟继续滚动）逐批增加，不会瞬间全部。"""
    page = make_page()
    page._results = _results(page._page_size * 5)
    page._shown_count = 0
    calls = []
    page._append_card = lambda r, cols: calls.append(1)
    page._apply_column_stretch = lambda cols: None
    page._update_batch_status = lambda: None
    page._maybe_preload_results = lambda: None
    page._load_more_results()
    assert len(calls) == page._page_size
    page._load_more_results()  # 再滚一批
    assert len(calls) == page._page_size * 2
    page._load_more_results()
    assert len(calls) == page._page_size * 3
    # 未到全部：仍剩 2 批未铺（滚动继续还能触发）
    assert page._shown_count == page._page_size * 3


def test_load_more_stops_when_all_rendered(app):
    """铺满全部后 _load_more_results 不再建卡。"""
    page = make_page()
    page._results = _results(page._page_size * 2)
    page._shown_count = 0
    calls = []
    page._append_card = lambda r, cols: calls.append(1)
    page._apply_column_stretch = lambda cols: None
    page._update_batch_status = lambda: None
    page._maybe_preload_results = lambda: None
    page._load_more_results()
    page._load_more_results()
    assert page._shown_count == page._page_size * 2
    before = len(calls)
    page._load_more_results()  # 已全部渲染 → 不再建卡
    assert len(calls) == before


def test_clear_grid_resets_pending_chain(app):
    """清网格（换关键词）→ _more_pending 复位 + 旧分批链不再续铺。"""
    page = make_page()
    page._results = _results(page._page_size * 2)
    page._shown_count = 0
    page._more_pending = True  # 模拟分批链进行中
    calls = []
    page._append_card = lambda r, cols: calls.append(1)
    page._load_more_results()  # more_pending=True 时被挡
    assert calls == []
    page._clear_grid()
    assert page._more_pending is False


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])