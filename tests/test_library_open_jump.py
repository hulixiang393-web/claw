# -*- coding: utf-8 -*-
"""书架卡片点击跳转回归测试（test_library_open_jump.py）。

历史 bug：card.clicked 信号携带 rec 参数抢占了 lambda 闭包默认参数，
`_on_card_clicked` 收到的是裸 rec 而非 {"rec","members"} 条目 → KeyError
静默，点击卡片没反应（跳不到阅读界面）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QGridLayout

_app = QApplication.instance() or QApplication([])


class _FakeShelf:
    def folders(self):
        return []


def _page():
    from gui.pages.library_page import LibraryPage

    return LibraryPage(shelf_service=_FakeShelf())


def _fav(title, url, sid="avgood", ctype="video"):
    return {"title": title, "source_id": sid, "url": url,
            "kind": "favorite", "content_type": ctype}


def _cards(page):
    out = []
    for i in range(page.body.count()):
        it = page.body.itemAt(i)
        if it.layout() and isinstance(it.layout(), QGridLayout):
            g = it.layout()
            out.extend(g.itemAt(j).widget() for j in range(g.count())
                       if g.itemAt(j).widget())
    return out


def test_single_card_click_emits_open_online(monkeypatch):
    """单本收藏卡：点击 → open_online_requested 带完整 payload。"""
    from unittest.mock import patch

    from gui.components.cover_loader import CoverLoader

    page = _page()
    with patch.object(CoverLoader.instance(), "load"):
        page._render([_fav("剑来", "u1")])
    cards = _cards(page)
    assert len(cards) == 1
    got = []
    page.open_online_requested.connect(got.append)
    cards[0].clicked.emit(cards[0].rec)  # 同 mousePressEvent 的发信号路径
    assert got == [("avgood", "u1", "video")]


def test_merged_card_click_picks_member(monkeypatch):
    """系列合并卡：点击 → 弹成员选集（不 KeyError）。"""
    from gui.pages import library_page as lp_mod

    page = _page()
    called = []
    monkeypatch.setattr(lp_mod.LibraryPage, "_pick_series_member",
                        lambda self, leader, members: called.append(members))
    item = page._to_shelf_item([
        _fav("背徳の少女-第1話", "u1"),
        _fav("背徳の少女-第2話", "u2"),
    ])
    from gui.pages.library_page import _ShelfCard

    card = _ShelfCard(item["rec"])
    card.clicked.connect(lambda _rec, it=item: page._on_card_clicked(it))
    card.clicked.emit(card.rec)
    assert called and called[0] is item["members"]


def test_local_card_click_emits_local(monkeypatch):
    """本地书卡：点击 → 走本地打开链路（play_local_video / load_local...）。"""
    from unittest.mock import patch

    from gui.components.cover_loader import CoverLoader

    page = _page()
    rec = {"title": "本地视频", "kind": "local", "content_type": "video",
          "path": "C:/x", "episode_paths": ["C:/x/1.mp4"]}
    with patch.object(CoverLoader.instance(), "load"):
        page._render([rec])
    cards = _cards(page)
    assert len(cards) == 1
    got = []
    page.play_local_video_requested.connect(got.append)
    cards[0].clicked.emit(cards[0].rec)
    assert got == [rec]