# -*- coding: utf-8 -*-
"""搜索页源多选菜单交互回归测试（test_search_page_source_menu.py）。

覆盖用户反馈的三点：
1. 勾选/取消源后**不自动搜索**（必须点「搜索」）；
2. 勾选/取消源后就地清空旧结果并提示「已更换源，请点击搜索」；
3. 勾选源时**不重建菜单**（不重置菜单滚动位置、无刷新闪烁）；
4. 源按钮文字随选择正确变化（全部 / N个 / 无）。

离屏运行，不依赖真实 WorkCard/网络（_make_card 用轻量 QFrame 替身）。
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


@pytest.fixture(scope="module")
def app(_qapp):
    return _qapp


def _make_page():
    mgr = SourceManager()
    mgr.add(SourceConfig.from_dict(SRC))
    mgr.add(SourceConfig.from_dict({**SRC, "$id": "site_b", "$name": "站点B"}))
    mgr.add(SourceConfig.from_dict({**SRC, "$id": "site_c", "$name": "站点C"}))
    page = SearchPage(mgr, Search(http=None, parser=Parser()))
    # 轻量卡片替身，避免真实 WorkCard 的封面加载/网络
    page._make_card = lambda r: QFrame()
    return page


def _source(page, sid):
    return next(s for s in page._manager.enabled_sources() if s.source_id == sid)


def _populate_results(page, n=5):
    page._results = _results(n)
    page._shown_count = 0
    page._append_results(page._results)


# ------------------------------------------------------------------------- #
# 1. 勾选/取消源不自动搜索
# ------------------------------------------------------------------------- #
def test_source_toggle_does_not_trigger_search(app):
    page = _make_page()
    calls = []
    page._on_search = lambda: calls.append(1)

    page._on_source_toggled(_source(page, "site_a"), False)
    page._on_source_toggled(_source(page, "site_b"), False)
    page._on_source_toggled(_source(page, "site_a"), True)
    page._on_all_source_toggled(False)
    page._on_all_source_toggled(True)

    assert calls == []  # 任何源选择变更都不得自动搜索


def test_all_source_toggle_does_not_trigger_search(app):
    page = _make_page()
    calls = []
    page._on_search = lambda: calls.append(1)

    page._on_all_source_toggled(False)
    page._on_all_source_toggled(True)

    assert calls == []


# ------------------------------------------------------------------------- #
# 2. 换源后清空旧结果 + 提示
# ------------------------------------------------------------------------- #
def test_source_toggle_clears_previous_results(app):
    page = _make_page()
    _populate_results(page, 5)
    assert page.grid_layout.count() > 0
    assert page._results
    epoch_before = page._search_epoch

    page._on_source_toggled(_source(page, "site_a"), False)

    assert page._results == []
    assert page._results_display is None
    assert page._shown_count == 0
    assert page._work_count == 0
    assert page.grid_layout.count() == 0
    assert page._streamed == set()
    assert page._search_epoch == epoch_before + 1  # 旧搜索任务作废
    assert "请点击搜索" in page.status_label.text()


def test_all_source_toggle_clears_previous_results(app):
    page = _make_page()
    _populate_results(page, 5)
    epoch_before = page._search_epoch

    page._on_all_source_toggled(False)

    assert page._results == []
    assert page.grid_layout.count() == 0
    assert page._work_count == 0
    assert page._search_epoch == epoch_before + 1
    assert "请点击搜索" in page.status_label.text()


def test_source_toggle_hides_status_bar_and_clears_chips(app):
    page = _make_page()
    page.status_bar.setVisible(True)
    page._status_chips = {"site_a": object()}

    page._on_source_toggled(_source(page, "site_a"), False)

    assert page._status_chips == {}
    assert page.status_bar.isVisible() is False


# ------------------------------------------------------------------------- #
# 3. 勾选源不重建菜单
# ------------------------------------------------------------------------- #
def test_source_toggle_does_not_rebuild_menu(app):
    page = _make_page()
    rebuilds = []
    orig = page._rebuild_sources_menu

    def spy():
        rebuilds.append(1)
        orig()

    page._rebuild_sources_menu = spy

    page._on_source_toggled(_source(page, "site_a"), False)
    page._on_source_toggled(_source(page, "site_b"), True)
    page._on_all_source_toggled(False)
    page._on_all_source_toggled(True)

    assert rebuilds == []  # 就地更新，绝不重建（否则菜单滚动位置被重置）


def test_source_toggle_keeps_other_rows_state(app):
    page = _make_page()
    # 取消 site_a：其余行应保持勾选，且被点行已由按钮自身切换
    page._on_source_toggled(_source(page, "site_a"), False)
    rows = page._src_rows
    assert set(rows) == {"site_a", "site_b", "site_c"}
    assert rows["site_b"].isChecked() is True
    assert rows["site_c"].isChecked() is True


# ------------------------------------------------------------------------- #
# 4. 源按钮文字
# ------------------------------------------------------------------------- #
def test_button_label_default_all(app):
    page = _make_page()
    assert page.src_btn.text() == "源：全部"


def test_button_label_partial_then_none(app):
    page = _make_page()
    page._on_source_toggled(_source(page, "site_a"), False)
    assert page.src_btn.text() == "源：2个"  # 3 源取消 1 个
    page._on_source_toggled(_source(page, "site_b"), False)
    assert page.src_btn.text() == "源：1个"
    page._on_source_toggled(_source(page, "site_c"), False)
    assert page.src_btn.text() == "源：无"


def test_button_label_all_action_toggle(app):
    page = _make_page()
    page._on_all_source_toggled(False)
    assert page.src_btn.text() == "源：无"
    page._on_all_source_toggled(True)
    assert page.src_btn.text() == "源：全部"


def test_select_all_rows_restores_all_label(app):
    page = _make_page()
    # 逐个取消再逐个勾回 → 全选态恢复
    page._on_source_toggled(_source(page, "site_a"), False)
    page._on_source_toggled(_source(page, "site_b"), False)
    page._on_source_toggled(_source(page, "site_c"), False)
    page._on_source_toggled(_source(page, "site_a"), True)
    page._on_source_toggled(_source(page, "site_b"), True)
    page._on_source_toggled(_source(page, "site_c"), True)
    assert page._all_selected is True
    assert page.src_btn.text() == "源：全部"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
