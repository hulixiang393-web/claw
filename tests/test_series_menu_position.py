"""系列合并卡「选集」菜单定位回归（2026-10-27）。

系列合并卡（如「共 3 話」「第1-3季」）点击后弹出的成员选集菜单，必须锚在
**被点击的卡片**附近：优先贴在卡片下方，下方空间不足则翻到卡片上方，并夹在
屏幕可用区域内。旧实现用 `menu.exec()` 无参调用，菜单弹在光标处，卡片位于
滚动区时经常跑到窗口别处。

不联网、不创建真实窗口交互，只验证定位计算与调用契约。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import inspect

import pytest

pytest.importorskip("PySide6")


def test_series_menu_positions_below_card(_qapp):
    """卡片在屏幕中部：菜单贴在卡片下方，左边缘对齐卡片。"""
    from PySide6.QtWidgets import QFrame, QMenu, QWidget

    from gui.pages.library_page import LibraryPage

    host = QWidget()
    card = QFrame(host)
    card.setFixedSize(210, 268)
    card.move(40, 30)
    host.resize(900, 700)
    host.show()

    menu = QMenu(host)
    menu.addAction("第一季")
    menu.addAction("第二季")

    pos = LibraryPage._series_menu_pos(menu, card)
    assert pos is not None
    top_left = card.mapToGlobal(card.rect().topLeft())
    assert pos.y() >= top_left.y() + card.height() - 1
    assert pos.x() == top_left.x()
    host.close()


def test_series_menu_flips_above_near_screen_bottom(_qapp):
    """卡片贴近屏幕底部：菜单翻到卡片上方，不越出屏幕。"""
    from PySide6.QtWidgets import QFrame, QMenu, QWidget

    from gui.pages.library_page import LibraryPage

    host = QWidget()
    card = QFrame(host)
    card.setFixedSize(210, 268)
    host.resize(900, 700)
    host.show()

    screen = _qapp.primaryScreen().availableGeometry()
    card.move(40, screen.height() - 200)
    menu = QMenu(host)
    menu.addAction("第一季")

    pos = LibraryPage._series_menu_pos(menu, card)
    assert pos is not None
    top_left = card.mapToGlobal(card.rect().topLeft())
    assert pos.y() < top_left.y()
    assert screen.top() <= pos.y() <= screen.bottom()
    host.close()


def test_series_menu_pos_without_anchor_returns_none(_qapp):
    """无可用锚点（卡片已销毁/未传）时返回 None，调用方回退光标位置。"""
    from PySide6.QtWidgets import QMenu

    from gui.pages.library_page import LibraryPage

    menu = QMenu()
    menu.addAction("x")
    assert LibraryPage._series_menu_pos(menu, None) is None


def test_card_click_passes_card_as_menu_anchor():
    """点击书架卡片必须把卡片本身传给 _on_card_clicked（作为菜单锚点）。"""
    from gui.pages.library_page import LibraryPage

    assert "source_card" in inspect.signature(
        LibraryPage._on_card_clicked
    ).parameters
    assert "anchor" in inspect.signature(
        LibraryPage._pick_series_member
    ).parameters


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
