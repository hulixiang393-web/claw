# -*- coding: utf-8 -*-
"""固定高度卡片在全局主题 QSS 下的布局回归测试。

历史 bug：QSS 全局 `QFrame { padding: 12px }` 会因 QLabel 继承 QFrame
而作用于卡片内所有标签，把标题/作者/来源的 sizeHint 撑大，导致固定高卡片
布局溢出、标题被顶到封面区域（发现/书架/搜索页普遍可见）。

回归点：
- 加载真实主题 QSS（含 QFrame padding）后，WorkCard 标题仍在封面下方；
- ShelfCard（书架卡片）同样不受影响；
- 各信息组合（作者/更新缺失）都不复现。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from PySide6.QtCore import QPoint

from framework.search import SearchResult
from framework.theme_manager import ThemeManager
from gui.components.work_card import WorkCard, CARD_HEIGHT, COVER_HEIGHT
from gui.pages.library_page import _ShelfCard


@pytest.fixture(scope="module")
def themed_app(_qapp):
    """应用真实主题 QSS（触发全局 QFrame padding 规则）。"""
    _qapp.setStyleSheet(ThemeManager().generate_qss("sakura"))
    yield _qapp
    _qapp.setStyleSheet("")


def _gap(card, cover_h, cover_widget):
    """标题顶部到封面的垂直间距（>0 即无重叠）。"""
    p = cover_widget.mapTo(card, QPoint(0, 0))
    sell = getattr(card, "_title", None)
    tp = sell.mapTo(card, QPoint(0, 0))
    return tp.y() - (p.y() + cover_h)


def _make_work(title="标题", author="作者", source="站点", update="更新",
               selectable=True, source_filterable=True):
    w = SearchResult(
        title=title, url="u", source_id="s",
        source_name=source, author=author, update=update,
    )
    w.cover = ""
    return WorkCard(w, selectable=selectable, source_filterable=source_filterable)


def _show(app, card, width=180):
    card.resize(width, card.height())
    card.show()
    app.processEvents()
    card.layout().activate()
    app.processEvents()
    return card


def test_workcard_title_not_overlapping_cover_with_full_info(themed_app):
    """全信息卡片（封面+标题+作者+来源+更新）：标题保持封面下方，不重叠。"""
    card = _show(themed_app, _make_work())
    assert _gap(card, COVER_HEIGHT, card._cover) >= 6


def test_workcard_title_position_discover_default(themed_app):
    """发现页默认配置（无 selectable / 无来源可点）：同样不重叠。"""
    card = _show(themed_app, _make_work(selectable=False, source_filterable=False))
    assert _gap(card, COVER_HEIGHT, card._cover) >= 6


def test_workcard_no_author(themed_app):
    """无作者时布局同样完整。"""
    card = _show(themed_app, _make_work(author=""))
    assert _gap(card, COVER_HEIGHT, card._cover) >= 6


def test_workcard_no_update(themed_app):
    """无更新（来源仍显示，单 meta 行）布局完整。"""
    card = _show(themed_app, _make_work(update=""))
    assert _gap(card, COVER_HEIGHT, card._cover) >= 6


def test_workcard_internal_labels_padding_zero(themed_app):
    """卡片内标签不受全局 QFrame padding 影响：作者行高与字号一致（~11px）。"""
    card = _show(themed_app, _make_work())
    assert card._author_label.height() <= 14


def test_shelfcard_title_not_overlapping_cover(themed_app):
    """书架卡片（_ShelfCard）标题与封面不重叠。"""
    card = _ShelfCard({
        "title": "书名", "author": "作者", "source_name": "站点",
        "kind": "online", "content_type": "video", "url": "u",
        "updated_at": "x", "pos": 0,
    })
    _show(themed_app, card, width=200)
    assert _gap(card, card._cover.height(), card._cover) >= 4