# -*- coding: utf-8 -*-
"""源管理页搜索/过滤功能测试（test_source_page_search.py）。

覆盖：
- 初始无过滤时全部源行可见；
- 按 名称 / source_id / 英文类型 / 中文类型 子串匹配，且大小写不敏感；
- 清空搜索框后全部恢复；
- 过滤只改可见性，不改启用 / 权重 / 健康 / manager 数据，也不重建行；
- 刷新（_rebuild_rows）后当前过滤词仍然生效。

注意：未 show 的父窗口下 isVisible() 恒为 False，故统一用 isHidden() 断言
（setVisible(False) 会置 hidden=True；_rebuild_rows 末尾对空过滤调用
setVisible(True) 会清除 hidden 标记，因此 isHidden() 可靠反映过滤结果）。
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
from framework.source_manager import SourceManager
from gui.pages.source_page import SourcePage


@pytest.fixture(scope="module")
def app(_qapp):
    return _qapp


def _cfg(sid, name, ctype):
    return SourceConfig.from_dict(
        {"$schema_version": 2, "$id": sid, "$type": ctype, "$name": name}
    )


def _make_page(tmp_path):
    mgr = SourceManager()
    mgr.add(_cfg("alpha_site", "阿尔法小说", "novel"))
    mgr.add(_cfg("comic_hub", "漫画之家", "comic"))
    mgr.add(_cfg("video_zone", "影视地带", "video"))
    # checker 传 None：搜索/过滤不触发诊断，无需真实 checker
    return SourcePage(mgr, None, tmp_path)


def _rows_by_id(page):
    return {row.source().source_id: row for row in page._rows}


def _hidden_map(page):
    return {sid: row.isHidden() for sid, row in _rows_by_id(page).items()}


# ------------------------------------------------------------------------- #
def test_initial_all_visible(app, tmp_path):
    page = _make_page(tmp_path)
    assert _hidden_map(page) == {
        "alpha_site": False,
        "comic_hub": False,
        "video_zone": False,
    }


def test_filter_by_name_substring(app, tmp_path):
    page = _make_page(tmp_path)
    page._search_input.setText("阿尔法")
    assert _hidden_map(page) == {
        "alpha_site": False,
        "comic_hub": True,
        "video_zone": True,
    }


def test_filter_by_source_id_case_insensitive(app, tmp_path):
    page = _make_page(tmp_path)
    page._search_input.setText("COMIC_HUB")
    assert _hidden_map(page) == {
        "alpha_site": True,
        "comic_hub": False,
        "video_zone": True,
    }


def test_filter_by_english_type(app, tmp_path):
    page = _make_page(tmp_path)
    page._search_input.setText("video")
    assert _hidden_map(page) == {
        "alpha_site": True,
        "comic_hub": True,
        "video_zone": False,
    }


def test_filter_by_chinese_type(app, tmp_path):
    page = _make_page(tmp_path)
    page._search_input.setText("漫画")
    assert _hidden_map(page) == {
        "alpha_site": True,
        "comic_hub": False,
        "video_zone": True,
    }


def test_clear_restores_all(app, tmp_path):
    page = _make_page(tmp_path)
    page._search_input.setText("阿尔法")
    page._search_input.clear()
    assert all(not h for h in _hidden_map(page).values())


def test_filter_does_not_mutate_state_or_rebuild(app, tmp_path):
    page = _make_page(tmp_path)
    mgr = page._manager
    rows_before = [id(r) for r in page._rows]
    snapshot = {
        s.source_id: (s.enabled, s.weight, mgr.get_health(s.source_id).state)
        for s in mgr.all()
    }

    page._search_input.setText("影视")

    assert [id(r) for r in page._rows] == rows_before  # 未重建行
    assert {s.source_id: (s.enabled, s.weight, mgr.get_health(s.source_id).state)
            for s in mgr.all()} == snapshot


def test_filter_survives_refresh(app, tmp_path):
    page = _make_page(tmp_path)
    page._search_input.setText("影视")
    page.refresh()  # 重建行后应沿用当前过滤词
    assert _hidden_map(page) == {
        "alpha_site": True,
        "comic_hub": True,
        "video_zone": False,
    }


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
