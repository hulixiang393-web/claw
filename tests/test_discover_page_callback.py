# -*- coding: utf-8 -*-
"""发现页单页回调的异常隔离（test_discover_page_callback.py）。

现场：网络抖动时 Qt 槽里抛出
    AttributeError: 'function' object has no attribute 'event'
被 Qt 包成
    SystemError: <class 'method'> returned a result with an exception set
只剩一行 traceback，发现页那一页的渲染/预加载状态停在半路。现在统一经
`_deliver_page_loaded` 投递：异常只记日志，并清在途标记、继续预加载。

运行：python -m pytest tests/test_discover_page_callback.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.pages.discover_page import _deliver_page_loaded  # noqa: E402


class _FakePage:
    def __init__(self, exc: BaseException | None = None):
        self.calls = []
        self._active_pages = {2, 3}
        self.preloaded = 0
        self._exc = exc

    def _on_page_loaded(self, works, err, page, epoch):
        self.calls.append((works, err, page, epoch))
        if self._exc is not None:
            raise self._exc

    def _maybe_preload(self):
        self.preloaded += 1


def test_normal_delivery_reaches_page():
    page = _FakePage()
    works = [{"title": "x"}]
    _deliver_page_loaded(page, works, None, 2, 7)
    assert page.calls == [(works, None, 2, 7)]
    assert page.preloaded == 0, "正常路径不该被兜底逻辑干扰"


def test_callback_exception_is_isolated_and_loading_continues(caplog):
    page = _FakePage(AttributeError("'function' object has no attribute 'event'"))
    with caplog.at_level("ERROR"):
        _deliver_page_loaded(page, [], None, 2, 7)     # 不得抛出去
    assert page.calls, "回调确实被调用过"
    assert 2 not in page._active_pages, "在途标记要清掉，避免该页永远卡在加载中"
    assert page.preloaded == 1, "预加载要继续，别的页才有机会填满视口"
    assert any("回调异常" in rec.getMessage() for rec in caplog.records), caplog.records
