# -*- coding: utf-8 -*-
"""发现页滚动锚定回归测试（test_discover_scroll_anchor.py）。

覆盖「加载中内容随加载上下自动滚动」：
- `_finish_append_anchor` 只在用户本来就贴底时才钉到新底部续接；非贴底时
  绝不 setValue。根因：流式加载 / 多页并发返回时，每个追加批次都排队一个
  锚点，非贴底分支按追加前的位置回写 setValue，反复拉扯视口上下跳动。

离线（offscreen）：构造 DiscoverPage 时用空源列表跳过网络，作品卡片用轻量
假 Work；不触达真实站点。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtCore import QEventLoop, QTimer

from gui.pages.discover_page import DiscoverPage


class _FakeManager:
    """最小 SourceManager 替身：无源 → __init__ 不发起任何网络加载。"""

    def discoverable_sources(self):
        return []

    def get_health(self, sid):
        return SimpleNamespace(state="ok", last_error="")

    def get(self, sid):
        return None


class _Work:
    def __init__(self, i):
        self.title = f"作品{i}"
        self.url = f"http://x.example/w/{i}"
        self.source_id = "demo"
        self.cover = ""
        self.author = ""
        self.source_name = "演示站"
        self.update = ""


@pytest.fixture(scope="module")
def app(_qapp):
    return _qapp


def _make_page():
    page = DiscoverPage(_FakeManager(), None, None, None, None, None)
    page.resize(900, 700)
    page.show()
    return page


def _wait(ms: int = 50) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _dispose(page) -> None:
    """销毁页面并处理删除事件（防悬空子控件在后续测试收到事件）。"""
    from PySide6.QtWidgets import QApplication

    page.hide()
    page.setParent(None)
    page.deleteLater()
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


def test_anchor_does_not_move_scroll_when_not_at_bottom(app):
    """非贴底：_finish_append_anchor 不得改变滚动位置（修复前会回写 prev_value）。"""
    page = _make_page()
    vbar = page.scroll.verticalScrollBar()
    vbar.setRange(0, 1000)
    vbar.setValue(500)
    page._finish_append_anchor(was_at_bottom=False, prev_value=300)
    assert vbar.value() == 500, f"非贴底不应被拉走，got {vbar.value()}"
    _dispose(page)


def test_anchor_pins_to_bottom_when_at_bottom(app):
    """贴底：钉到新底部续接（无限滚动语义保留）。"""
    page = _make_page()
    vbar = page.scroll.verticalScrollBar()
    vbar.setRange(0, 1000)
    vbar.setValue(1000)
    page._finish_append_anchor(was_at_bottom=True, prev_value=1000)
    assert vbar.value() == 1000
    _dispose(page)


def test_streaming_append_keeps_scroll_position(app):
    """流式分批追加作品（不贴底时）滚动条 value 保持不变，不被自动滚动。"""
    page = _make_page()
    vbar = page.scroll.verticalScrollBar()
    page._append_works([_Work(i) for i in range(120)])
    # 等分批铺设完成、布局稳定（确保内容真正可滚动）
    deadline = 0
    while vbar.maximum() <= 200 and deadline < 50:
        _wait(20)
        deadline += 1
    assert vbar.maximum() > 200, "前置条件：内容应可滚动"

    vbar.setValue(max(1, vbar.maximum() // 2))
    before = vbar.value()

    page._append_works([_Work(i) for i in range(120, 240)])
    for _ in range(15):
        _wait(20)
    app.processEvents()

    assert vbar.value() == before, (
        f"流式追加不应改变滚动位置，got {vbar.value()}（原 {before}）"
    )
    _dispose(page)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
