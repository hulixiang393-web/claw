# -*- coding: utf-8 -*-
"""书架收藏系列并卡 GUI 测试（test_library_series_gui.py）。

规则（用户确认 2026-09-12）：
- 收藏按「同 source_id + 同主书名（去尾部 第N话/集/卷/季）」合并成一张卡；
- 合并卡展示首部封面 + 角标（「共 2 話」/「第1-3季」），悬停 tooltip 列成员；
- 点击合并卡 → 弹成员选集；选成员 → 原打开链路（open_online_requested）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QGridLayout, QLabel, QMenu

_app = QApplication.instance() or QApplication([])


class _FakeShelf:
    """书架页用到的 ShelfService 子集（render 路径用）。"""

    def folders(self):
        return []


def _page():
    from gui.pages.library_page import LibraryPage

    return LibraryPage(shelf_service=_FakeShelf())


def _fav(title, url, sid="avgood", ctype="video"):
    return {"title": title, "source_id": sid, "url": url,
            "kind": "favorite", "content_type": ctype}


# ------------------------------------------------------------------------- #
def test_to_shelf_item_single_stays_plain():
    page = _page()
    rec = _fav("剑来", "u1")
    item = page._to_shelf_item([rec])
    assert "members" not in item
    assert item["rec"] is rec


def test_to_shelf_item_merges_series():
    page = _page()
    grp = [
        _fav("背徳の少女-第1話", "u1"),
        _fav("背徳の少女-第2話", "u2"),
    ]
    item = page._to_shelf_item(grp)
    assert item["rec"]["title"] == "背徳の少女"          # 主书名
    assert item["rec"]["series_badge"] == "共 2 話"     # 角标
    assert len(item["members"]) == 2                    # 合集成员
    assert len(item["rec"]["series_tooltip"].splitlines()) == 2


def test_render_merges_series_to_one_card():
    page = _page()
    page._render([
        _fav("背徳の少女-第1話", "u1"),
        _fav("背徳の少女-第2話", "u2"),
        {"title": "本地书", "kind": "local",
         "content_type": "novel", "path": "x"},
    ])
    headers, grids = [], []
    for i in range(page.body.count()):
        it = page.body.itemAt(i)
        if it.widget() and isinstance(it.widget(), QLabel):
            headers.append(it.widget().text())
        if it.layout() and isinstance(it.layout(), QGridLayout):
            grids.append(it.layout())

    assert "本地" in headers and "收藏" in headers
    cards = [g.itemAt(j).widget() for g in grids for j in range(g.count())
             if g.itemAt(j).widget()]
    assert len(cards) == 2  # 1 本地 + 1 合并收藏
    merged = cards[1]
    assert merged.rec.get("series_badge") == "共 2 話"
    assert merged.toolTip().startswith("该系列成员")


def test_on_card_clicked_dispatches_to_members(monkeypatch):
    """点击合并卡 → 进入成员选集（不直接打开）。"""
    from gui.pages import library_page as lp_mod

    page = _page()
    grp = [
        _fav("背徳の少女-第1話", "u1"),
        _fav("背徳の少女-第2話", "u2"),
    ]
    item = page._to_shelf_item(grp)
    called = []
    monkeypatch.setattr(lp_mod.LibraryPage, "_pick_series_member",
                        lambda self, leader, members: called.append(members))
    page._on_card_clicked(item)
    assert called and called[0] is item["members"]


def test_open_rec_opens_online_series_member():
    """成员选完后 → 走原打开链路（网络书 open_online_requested）。"""
    page = _page()
    got = []
    page.open_online_requested.connect(got.append)
    page._open_rec(_fav("背徳の少女-第2話", "u2"))
    assert got == [("avgood", "u2", "video")]


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))