# -*- coding: utf-8 -*-
"""作品卡片文字占位封面测试（test_work_card_placeholder.py）。

背景：部分源站无封面（列表纯文本）或封面图床失效（如 quanben 的
www.quanben.com 已成封锁页）。卡片应生成「标题首字 + 散列主题色」的文字
占位封面，而不是长期空白或加载失败的占位图标。

离线（offscreen）：不触达网络。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from gui.components.work_card import WorkCard, _make_text_cover, COVER_HEIGHT


class _Work:
    def __init__(self, title="完美世界", cover=""):
        self.title = title
        self.url = "http://x.example/w/1"
        self.source_id = "quanben"
        self.cover = cover
        self.author = ""
        self.source_name = "全本小说网"
        self.update = ""


@pytest.fixture(scope="module")
def app(_qapp):
    return _qapp


def test_make_text_cover_non_null_and_distinct(app):
    pm1 = _make_text_cover("完美世界", 200, COVER_HEIGHT)
    pm2 = _make_text_cover("斗破苍穹", 200, COVER_HEIGHT)
    assert not pm1.isNull() and not pm2.isNull()
    assert pm1.toImage() != pm2.toImage()  # 不同标题 → 不同配色


def test_no_cover_card_shows_text_placeholder(app):
    card = WorkCard(_Work(cover=""))
    card.resize(200, COVER_HEIGHT)
    card.show()
    app.processEvents()
    assert card._placeholder_active is True
    assert not card._cover.pixmap().isNull()
    card.deleteLater()
    app.processEvents()


def test_cover_load_failure_falls_back_to_placeholder(app):
    card = WorkCard(_Work(cover="http://x.example/c.jpg"), defer_cover=True)
    card.resize(200, COVER_HEIGHT)
    card.show()
    app.processEvents()
    card.set_cover_pixmap(None)  # 模拟加载失败回调
    assert card._placeholder_active is True
    assert not card._cover.pixmap().isNull()
    card.deleteLater()
    app.processEvents()


def test_real_cover_clears_placeholder_flag(app):
    from PySide6.QtGui import QPixmap

    card = WorkCard(_Work(cover=""), )
    card.resize(200, COVER_HEIGHT)
    app.processEvents()
    assert card._placeholder_active is True
    pm = QPixmap(10, 10)
    pm.fill()
    card.set_cover_pixmap(pm)
    assert card._placeholder_active is False
    card.deleteLater()
    app.processEvents()


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
