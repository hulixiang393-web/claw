# -*- coding: utf-8 -*-
"""漫画阅读器自动滚动平滑性测试（test_comic_scroll_anchor.py）。

背景：自动滚动需要「记住当前位置 → 按设定速度递增」，不得因图片懒加载重排
而修正滚动值（会与速度推进叠加 → 跳过某一页/直接跳到另一页）。

覆盖：
- `_auto_scroll_tick` 按速度单调递增（不跳、不回退）。
- 自动滚动中 `_relayout_gallery` 不改动滚动值。
- 到最大值自动停止。

离线（offscreen）：不加载图片、不触达网络（直接设置滚动范围，避免异步图片
解码的 Qt 生命周期竞态）。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtCore import QEventLoop, QTimer

from framework.content import Chapter
from gui.pages.reader.comic_view import ComicView


class _MockContent:
    def fetch_comic_pages(self, source, url, on_page=None, cancel_evt=None):
        return []


@pytest.fixture(scope="module")
def app(_qapp):
    return _qapp


@pytest.fixture(scope="module")
def comic(app):
    c = ComicView(_MockContent())
    c.resize(700, 800)
    c.show()
    c._chapters = [Chapter("第1话", "http://x/c/1")]
    c._current_idx = 0
    c._images = []
    # 撑起 gallery 高度，让滚动范围稳定（避免空内容时 adjustSize 把范围收为 0）
    c.gallery.setMinimumHeight(20000)
    c.gallery.adjustSize()
    app.processEvents()
    yield c
    c._stop_auto_scroll()
    c.hide()


def _reset(comic, value=0):
    comic._stop_auto_scroll()
    comic.gallery.setMinimumHeight(20000)
    comic.gallery.adjustSize()
    comic.scroll.verticalScrollBar().setValue(value)


def test_tick_increments_monotonically_by_speed(app, comic):
    _reset(comic, 0)
    comic.auto_scroll_speed_slider.setValue(3)
    comic._toggle_auto_scroll()
    assert comic._auto_scrolling
    prev = comic.scroll.verticalScrollBar().value()
    comic._auto_last_tick = time.monotonic() - 0.016  # 模拟 1 帧 elapsed=16ms
    dt = 0.016
    for _ in range(20):
        comic._auto_scroll_tick()
        # 模拟下一帧：把时间基准往前拨固定 16ms，保持恒速推进
        comic._auto_last_tick = time.monotonic() - dt
        v = comic.scroll.verticalScrollBar().value()
        assert v >= prev, "自动滚动值不得回退"
        prev = v
    from gui.pages.reader.comic_view import AUTO_BASE_PX_PER_SEC, AUTO_RATIO

    speed = AUTO_BASE_PX_PER_SEC * (AUTO_RATIO ** (3 - 1))
    assert comic.scroll.verticalScrollBar().value() == int(20 * dt * speed), (
        f"应按 速度(px/s)*dt 线性递增，got {comic.scroll.verticalScrollBar().value()}"
    )
    comic._stop_auto_scroll()


def test_relayout_does_not_change_value_while_auto_scrolling(app, comic):
    _reset(comic, 2000)
    comic._toggle_auto_scroll()
    before = comic.scroll.verticalScrollBar().value()
    comic._relayout_gallery()  # 模拟图片懒加载重排
    app.processEvents()
    assert comic.scroll.verticalScrollBar().value() == before, "自动滚动中重排不得改动滚动值"
    comic._stop_auto_scroll()


def test_tick_stops_at_bottom(app, comic):
    _reset(comic, 0)
    vbar = comic.scroll.verticalScrollBar()
    vbar.setValue(vbar.maximum() - 3)
    comic._toggle_auto_scroll()
    comic._auto_last_tick = time.monotonic() - 0.1  # 模拟经过 100ms，足够越过 3px
    comic._auto_scroll_tick()
    assert not comic._auto_scrolling, "到底应自动停止"
    assert vbar.value() == vbar.maximum()


def test_location_snapshot_tracks_visible_image_and_internal_fraction(app, comic):
    first = type("Image", (), {"url": "https://img/old.jpg", "_image_key": "old", "y": 0, "height": 1000})()
    second = type("Image", (), {"url": "https://img/keep.jpg", "_image_key": "keep", "y": 1000, "height": 800})()
    comic._images = [first, second]
    comic._rendered_count = 2
    comic._image_labels = [first, second]
    comic.scroll.verticalScrollBar().setRange(0, 2000)
    comic.scroll.verticalScrollBar().setValue(1200)
    snapshot = comic._build_location_snapshot()
    assert snapshot["image_url"] == "https://img/keep.jpg"
    assert snapshot["image_key"] == "keep"
    assert snapshot["image_index"] == 1
    assert 0.0 < snapshot["image_fraction"] < 1.0
    assert snapshot["scroll_ratio"] == 0.6


def test_resolve_image_anchor_prefers_url_then_key_then_index(app, comic):
    labels = [
        type("Image", (), {"url": "https://img/a.jpg", "_image_key": "a", "y": 0, "height": 500})(),
        type("Image", (), {"url": "https://img/b.jpg", "_image_key": "b", "y": 500, "height": 500})(),
    ]
    comic._image_labels = labels
    assert comic._resolve_image_anchor({"image_url": "https://img/b.jpg", "image_index": 0}) is labels[1]
    assert comic._resolve_image_anchor({"image_key": "b", "image_index": 0}) is labels[1]
    assert comic._resolve_image_anchor({"image_index": 1}) is labels[1]


def test_image_key_ignores_signature_but_preserves_identity_query():
    signed_a = "https://img.example/page/2.jpg?width=800&sig=old&expires=1"
    signed_b = "https://img.example/page/2.jpg?expires=999&sig=new&width=800"
    other = "https://img.example/page/3.jpg?width=800&sig=new"
    assert ComicView._image_key_for_url(signed_a) == ComicView._image_key_for_url(signed_b)
    assert ComicView._image_key_for_url(signed_a) != ComicView._image_key_for_url(other)


def test_resolve_image_anchor_survives_signature_change_and_inserted_page(app, comic):
    target_url = "https://img.example/page/2.jpg?sig=old&expires=1"
    labels = [
        type("Image", (), {"url": "https://img.example/page/1.jpg?sig=x", "_image_key": ComicView._image_key_for_url("https://img.example/page/1.jpg?sig=x"), "y": 0, "height": 500})(),
        type("Image", (), {"url": "https://img.example/page/0.jpg?sig=y", "_image_key": ComicView._image_key_for_url("https://img.example/page/0.jpg?sig=y"), "y": 500, "height": 500})(),
        type("Image", (), {"url": "https://img.example/page/2.jpg?sig=new&expires=9", "_image_key": ComicView._image_key_for_url(target_url), "y": 1000, "height": 500})(),
    ]
    comic._image_labels = labels
    assert comic._resolve_image_anchor({"image_key": ComicView._image_key_for_url(target_url), "image_url": target_url, "image_index": 1}) is labels[2]


def test_restore_image_anchor_retries_after_lazy_layout(app, comic):
    comic._detail = object()
    comic._scroll_epoch += 1
    comic._image_labels = []
    comic.scroll.verticalScrollBar().setRange(0, 2000)
    comic._restore_location_with_retry({"chapter_url": "http://x/c/1", "image_key": "target", "scroll_ratio": 0.1}, tries=2)
    label = type("Image", (), {"url": "new", "_image_key": "target", "y": 700, "height": 500})()
    comic._image_labels = [label]
    loop = QEventLoop()
    QTimer.singleShot(250, loop.quit)
    loop.exec()
    assert comic.scroll.verticalScrollBar().value() == 700


def test_restore_image_anchor_ignores_stale_generation_and_epoch(app, comic):
    comic._detail = object()
    comic._image_labels = []
    comic.scroll.verticalScrollBar().setRange(0, 1000)
    comic.scroll.verticalScrollBar().setValue(0)
    comic._gen = 4
    comic._scroll_epoch = 7
    comic._restore_location_with_retry({"scroll_ratio": 0.8}, tries=1, generation=3, epoch=7)
    comic._restore_location_with_retry({"scroll_ratio": 0.8}, tries=1, generation=4, epoch=6)
    assert comic.scroll.verticalScrollBar().value() == 0


def test_restore_image_anchor_falls_back_to_ratio_when_image_unavailable(app, comic):
    comic._image_labels = []
    comic.scroll.verticalScrollBar().setRange(0, 1000)
    comic._restore_location_with_retry(
        {"chapter_url": "http://x/c/1", "image_url": "missing", "image_index": 9, "scroll_ratio": 0.4},
        tries=1,
    )
    assert comic.scroll.verticalScrollBar().value() == 400


def test_restore_image_anchor_requires_current_chapter_identity(app, comic):
    assert comic._location_matches_chapter({"chapter_url": "chapter-1"}, "chapter-1")
    assert not comic._location_matches_chapter({"chapter_url": "chapter-1"}, "chapter-2")
    assert not comic._location_matches_chapter({}, "chapter-1")


def test_comic_user_scroll_invalidates_pending_restore_retry(app):
    view = ComicView(object())
    view._detail = object()
    view._chapters = [Chapter("第1话", "http://x/c/1")]
    view._current_idx = 0
    view.scroll.verticalScrollBar().setRange(0, 2000)
    label = type("Image", (), {"url": "new", "_image_key": "target", "y": 700, "height": 500})()
    view._image_labels = []
    location = {"chapter_url": "http://x/c/1", "image_key": "target", "image_fraction": 0.0}
    view._restore_location_with_retry(location, pos=0.1, tries=2)
    view.scroll.verticalScrollBar().setValue(1600)
    epoch = view._scroll_epoch
    view._on_scrollbar_user_interaction()
    view._scroll_epoch = epoch
    view._image_labels = [label]
    loop = QEventLoop()
    QTimer.singleShot(150, loop.quit)
    loop.exec()
    assert view.scroll.verticalScrollBar().value() == 1600
    view.deleteLater()


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
