# -*- coding: utf-8 -*-
"""搜索页多选源联动测试：验证 _on_search 只对选中的源发起搜索。

通过捕获 _build_status_bar(sources) 传入的源列表（主线程同步调用），
断言源范围选取逻辑正确，避免 QThreadPool 异步时序（已知既有问题）：
- 初始（_all_selected=True）→ 所有启用源；
- 只勾选某几个源（_all_selected=False）→ 只有这几个源（其余不请求）；
- 类型筛选叠加时仍在选中源内过滤。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

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


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _make_page(app):
    mgr = SourceManager()
    mgr.add(SourceConfig.from_dict(SRC))
    mgr.add(SourceConfig.from_dict({**SRC, "$id": "site_b", "$name": "站点B"}))
    mgr.add(SourceConfig.from_dict({**SRC, "$id": "site_c", "$name": "站点C"}))
    search = Search(http=None, parser=Parser())
    return SearchPage(mgr, search)


def _captured_sources(page, selected, selected_type="", all_selected=None):
    """返回 _on_search 内部构造的源列表（捕获 _build_status_bar 参数）。"""
    if all_selected is None:
        all_selected = not selected
    page._all_selected = all_selected
    page._selected_sources = selected
    page.type_combo.setCurrentIndex(
        page.type_combo.findData(selected_type) if selected_type else 0
    )
    captured = []
    orig = page._build_status_bar

    def spy(sources):
        captured.append(list(sources))
        orig(sources)

    page._build_status_bar = spy
    page.keyword_input.setText("测试")
    try:
        page._on_search()
    finally:
        page._build_status_bar = orig
    return captured[-1] if captured else []


def test_default_all_sources(app):
    """默认（_selected_sources 空）→ 对所有启用源发起搜索。"""
    page = _make_page(app)
    sources = _captured_sources(page, set())
    assert sorted(s.source_id for s in sources) == ["site_a", "site_b", "site_c"]


def test_selected_two_sources_only(app):
    """只勾选 site_a + site_b → 只对这两个源发起搜索。"""
    page = _make_page(app)
    sources = _captured_sources(page, {"site_a", "site_b"})
    assert sorted(s.source_id for s in sources) == ["site_a", "site_b"]


def test_selected_source_filtered_by_type(app):
    """选中源 + 类型筛选 → 在选中源内按类型过滤。"""
    page = _make_page(app)
    # 全部源为 novel，加一个 video 源验证类型过滤
    page._manager.add(SourceConfig.from_dict(
        {**SRC, "$id": "site_v", "$name": "站点V", "$type": "video"}
    ))
    sources = _captured_sources(page, {"site_a", "site_v"}, selected_type="video")
    assert [s.source_id for s in sources] == ["site_v"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
