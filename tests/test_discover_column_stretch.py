# -*- coding: utf-8 -*-
"""发现页懒加载不闪屏回归测试（test_discover_column_stretch.py）。

根因：DiscoverPage._apply_column_stretch 无条件调用 apply_column_stretch
（setColumnStretch 会触发整网格重排）；懒加载分批铺设时每 16ms 一批都调一次
→ 每批全量重排 → 卡片反复跳动/闪屏（与 search_page 同因，后者已有列数守卫）。

修复：_apply_column_stretch 在列数未变时直接返回；_last_columns 由它统一维护
（_reflow/_append_works/_render_restored 不再提前赋值）。列数变化（窗口缩放）
仍正常重排。

离线（offscreen）：空源列表跳过网络，轻量假 Work。
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


def _wait(ms: int = 20) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _dispose(page) -> None:
    from PySide6.QtWidgets import QApplication

    page.hide()
    page.setParent(None)
    page.deleteLater()
    a = QApplication.instance()
    if a is not None:
        a.processEvents()


def test_apply_column_stretch_skips_when_unchanged(app):
    page = _make_page()
    calls = []
    page.apply_column_stretch = lambda layout, cols: calls.append(cols)
    page._last_columns = 0
    page._apply_column_stretch(5)
    page._apply_column_stretch(5)  # 相同列数 → 跳过
    page._apply_column_stretch(5)
    assert calls == [5], f"列数未变应只重排一次，got {calls}"
    assert page._last_columns == 5
    _dispose(page)


def test_apply_column_stretch_reapplies_on_change(app):
    page = _make_page()
    calls = []
    page.apply_column_stretch = lambda layout, cols: calls.append(cols)
    page._last_columns = 0
    page._apply_column_stretch(5)
    page._apply_column_stretch(6)  # 列数变化 → 重排
    assert calls == [5, 6], f"列数变化应重排，got {calls}"
    assert page._last_columns == 6
    _dispose(page)


def test_append_works_does_not_relayout_repeatedly(app):
    """同一列数下连续追加两批：apply_column_stretch 只应触发一次。"""
    page = _make_page()
    calls = []
    page.apply_column_stretch = lambda layout, cols: calls.append(cols)
    page._last_columns = 0
    page._append_works([_Work(i) for i in range(20)])
    for _ in range(6):
        _wait(20)
    first = len(calls)
    page._append_works([_Work(i) for i in range(20, 40)])
    for _ in range(6):
        _wait(20)
    assert first == 1, f"首批应只重排一次，got {calls}"
    assert len(calls) == 1, f"同列数第二批不应再次全量重排，got {calls}"
    _dispose(page)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
