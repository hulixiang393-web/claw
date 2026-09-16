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


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
