# -*- coding: utf-8 -*-
"""搜索页来源筛选 + WorkCard 来源可点接口测试。

覆盖：
- WorkCard 默认（发现页）source_filterable=False，无来源可点 label；
- 启用后来源 label 可点击且发射 source_clicked(source_id)；
- SearchPage._set_filter 从卡片来源点击接线：筛选只看该源 → 再次点击取消 → 清除恢复。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from PySide6.QtCore import Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from framework.config import SourceConfig
from framework.parser import Parser
from framework.search import Search, SearchResult
from framework.source_manager import SourceManager
from gui.components.work_card import WorkCard
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


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _make_page(app):
    # 排空上一用例遗留的已删除 HoverTitle 触发的过期事件，避免构造时
    # 进入 hover_title.eventFilter 未初始化 _popup 的既有告警路径
    app.processEvents()
    app.processEvents()
    mgr = SourceManager()
    mgr.add(SourceConfig.from_dict(SRC))
    mgr.add(SourceConfig.from_dict({**SRC, "$id": "site_b", "$name": "站点B"}))
    search = Search(http=None, parser=Parser())
    page = SearchPage(mgr, search)
    page.show()
    return page


def _results():
    return [
        SearchResult(title="A1", url="u-a1", source_id="site_a", source_name="站点A"),
        SearchResult(title="B1", url="u-b1", source_id="site_b", source_name="站点B"),
        SearchResult(title="A2", url="u-a2", source_id="site_a", source_name="站点A"),
    ]


def _sheet_ids(page):
    return {c.work.source_id for c in page.grid_container.findChildren(WorkCard)}


def test_workcard_default_not_source_filterable(app):
    """发现页默认卡片：source_filterable=False，无来源可点 label。"""
    card = WorkCard(SearchResult(title="X", url="u-x", source_id="site_a", source_name="站点A"))
    assert card._source_filterable is False
    assert card.source_clicked is not None
    card.set_source_filterable(True)
    assert card._source_filterable is True
    assert card._source_label is not None


def test_workcard_source_label_click_emits(app):
    """启用后可点击来源 label，点击发射 source_clicked(source_id)。"""
    card = WorkCard(
        SearchResult(title="X", url="u-x", source_id="site_a", source_name="站点A"),
        source_filterable=True,
    )
    triggered = []
    card.source_clicked.connect(triggered.append)
    assert card._source_label.cursor().shape() == Qt.PointingHandCursor
    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        card._source_label.rect().center(),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
    )
    card.eventFilter(card._source_label, ev)
    assert triggered == ["site_a"]


def test_filter_on_source_click(app):
    """点击来源角标 → 只看该源，其余消失；再次点击同源取消；清除恢复全量。"""
    page = _make_page(app)
    page._results = _results()
    page._results_display = list(page._results)
    page._shown_count = len(page._results)
    page._show_results()
    assert _sheet_ids(page) == {"site_a", "site_b"}

    page._set_filter("site_a")
    assert page._filter_source == "site_a"
    assert page.filter_label.text() == "仅看 站点A"
    assert page.filter_bar_widget.isVisible() is True
    assert _sheet_ids(page) == {"site_a"}

    # 再次点击同源 → 取消筛选
    page._set_filter("site_a")
    assert page._filter_source == ""
    assert page.filter_bar_widget.isVisible() is False
    assert _sheet_ids(page) == {"site_a", "site_b"}

    # 重新筛选后通过 × 清除筛选恢复
    page._set_filter("site_b")
    assert _sheet_ids(page) == {"site_b"}
    page._clear_filter()
    assert page._filter_source == ""
    assert _sheet_ids(page) == {"site_a", "site_b"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
