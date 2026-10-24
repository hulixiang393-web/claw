# -*- coding: utf-8 -*-
"""书架卡片不得创建瞬时顶层窗口（「pythonw 弹窗」）回归。

无 Qt 父对象的 QWidget 在 Qt 里就是顶层窗口；批量建卡时每个卡片/子标签
都会短暂成为系统顶层窗口，表现为书架首次加载时的「大量弹窗」，随后被
布局 reparent 掉（自动清除）。修复后所有子控件在构造时就带父对象。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QLabel, QWidget

from gui.components import cover_loader as cl
from gui.pages import library_page as lp_mod

_app = QApplication.instance() or QApplication([])


class _FakeLoader:
    def load(self, *args, **kwargs):
        return None

    def busy(self):
        return False


def _rec(**overrides):
    rec = {
        "kind": "favorite",
        "url": "http://site/book/1",
        "source_id": "src",
        "content_type": "novel",
        "title": "测试书名",
        "author": "作者",
        "cover": "http://img/1.jpg",
        "series_badge": "共 3 話",
        "series_tooltip": "第1话\n第2话",
    }
    rec.update(overrides)
    return rec


def test_shelf_card_is_child_not_window(monkeypatch):
    monkeypatch.setattr(
        cl.CoverLoader, "instance", staticmethod(lambda: _FakeLoader())
    )
    host = QWidget()

    card = lp_mod._ShelfCard(_rec(), host)

    # 卡片必须直接挂到宿主下，且不是独立顶层窗口
    assert card.parent() is host
    assert card.isWindow() is False
    assert card in host.findChildren(QWidget)


def test_shelf_card_children_all_have_parents(monkeypatch):
    monkeypatch.setattr(
        cl.CoverLoader, "instance", staticmethod(lambda: _FakeLoader())
    )
    host = QWidget()
    card = lp_mod._ShelfCard(_rec(), host)

    orphans = [w for w in card.findChildren(QWidget) if w.parent() is None]
    assert orphans == []


def test_empty_shelf_label_is_parented(monkeypatch):
    monkeypatch.setattr(
        cl.CoverLoader, "instance", staticmethod(lambda: _FakeLoader())
    )

    class _EmptyShelf:
        def folders(self):
            return []

        def list_items(self, **_kwargs):
            return []

    page = lp_mod.LibraryPage(shelf_service=_EmptyShelf())
    page._add_empty()

    labels = [
        w for w in page.container.findChildren(QLabel)
        if "书架还空着" in (w.text() or "")
    ]
    assert labels, "空书架提示未创建"
    assert labels[0].parent() is page.container
    assert labels[0].isWindow() is False


def test_clear_all_never_detaches_visible_cards(monkeypatch):
    """重建/换收藏夹时清空卡片：必须先隐藏再解除父子关系，不能把可见卡片
    直接 setParent(None)（否则会瞬间变成顶层 "pythonw" 窗口）。"""
    monkeypatch.setattr(
        cl.CoverLoader, "instance", staticmethod(lambda: _FakeLoader())
    )

    class _Shelf:
        def folders(self):
            return []

        def list_items(self, **_kwargs):
            return []

    page = lp_mod.LibraryPage(shelf_service=_Shelf())
    records = [_rec(url=f"http://site/book/{i}", title=f"书{i}") for i in range(5)]
    page._render(records)
    assert len(page._shelf_cards) == 5

    visible_detached: list[str] = []
    original = QWidget.setParent

    def _spy(widget, parent):
        if parent is None and widget.isVisible():
            visible_detached.append(type(widget).__name__)
        return original(widget, parent)

    monkeypatch.setattr(QWidget, "setParent", _spy)
    page._clear_all()

    assert visible_detached == [], visible_detached
    # 卡片已从容器脱离（不会继续被 findChildren 命中）
    assert page.container.findChildren(lp_mod._ShelfCard) == []
    assert page._shelf_cards == []