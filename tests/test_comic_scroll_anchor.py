# -*- coding: utf-8 -*-
"""漫画阅读器滚动锚定测试（test_comic_scroll_anchor.py）。

背景：自动滚动/手动滚动时，懒加载图片由占位高（600px）变为实际高，当前
可视内容随上方高度变化上下位移 → 视觉「晃动」。修复：`_relayout_gallery`
以视口顶部所在的那张图为锚，重排后补偿滚动值，保持可视内容不动。

离线（offscreen）：mock content 返回 data URI 图片，不触达网络。
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtCore import QEventLoop, QTimer, QBuffer
from PySide6.QtGui import QImage

from framework.content import Detail, Chapter
from gui.pages.reader.comic_view import ComicView, _ComicImageLabel


def _png_uri(w: int, h: int, color: int) -> str:
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(color)
    buf = QBuffer()
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(bytes(buf.data())).decode()


class _MockContent:
    def __init__(self, uris):
        self._uris = uris

    def fetch_comic_pages(self, source, url, on_page=None, cancel_evt=None):
        return list(self._uris)


@pytest.fixture(scope="module")
def app(_qapp):
    return _qapp


def _wait(app, ms=50, times=1):
    for _ in range(times):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
        app.processEvents()


def _make_comic(app):
    uris = [_png_uri(400, 900, 0xFF000000 | (i * 1234)) for i in range(40)]
    detail = Detail(
        source_id="demo", content_type="comic", url="http://x/c/1",
        title="测试漫画", chapters=[Chapter("第1话", "http://x/c/1/1")],
    )
    comic = ComicView(_MockContent(uris))
    comic.resize(700, 800)
    comic.show()
    comic.load(object(), detail, "")
    _wait(app, 50, 40)
    comic._render_incremental(force_full=True)
    _wait(app, 50, 40)
    return comic


def test_visible_anchor_returns_widget_under_viewport(app):
    comic = _make_comic(app)
    vbar = comic.scroll.verticalScrollBar()
    assert vbar.maximum() > 0
    vbar.setValue(vbar.maximum() // 2)
    app.processEvents()
    anchor = comic._visible_anchor(vbar.value())
    assert anchor is not None
    widget, offset = anchor
    assert widget.y() <= vbar.value() < widget.y() + widget.height()
    assert offset == vbar.value() - widget.y()
    comic.deleteLater()
    app.processEvents()


def test_relayout_keeps_visible_content_anchored(app):
    """视口上方图片高度变化后，可视内容（锚点相对位置）保持不动。"""
    comic = _make_comic(app)
    vbar = comic.scroll.verticalScrollBar()
    labels = comic.findChildren(_ComicImageLabel)
    vbar.setValue(vbar.maximum() // 2)
    app.processEvents()
    widget, offset = comic._visible_anchor(vbar.value())

    above = next(
        (l for l in labels if l.y() + l.height() <= vbar.value()), None
    )
    assert above is not None, "前置条件：应有视口上方的图片"
    above.setMinimumHeight(above.height() + 700)  # 上方图片晚到、变高
    comic._relayout_gallery()
    app.processEvents()

    new_offset = vbar.value() - widget.y()
    assert abs(new_offset - offset) <= 2, (
        f"锚点相对位置应保持（可视内容不动），was {offset} now {new_offset}"
    )
    comic.deleteLater()
    app.processEvents()


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
