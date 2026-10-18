# -*- coding: utf-8 -*-
"""漫画阅读视图：换话滚动归零 + 后续话预加载回归测试。

覆盖两个用户反馈：
1. 换话（上一话/下一话/目录跳转）后滚动条必须归零到顶部，且不被异步重排 /
   按比例恢复再次拉到底部。根因：_apply_zoom 捕获**上一话**的滚动比例，
   _finish_episode_load 归零后 160ms，_restore_scroll_ratio 又把新话拉回同一
   比例（读到底部时 ratio≈1 → 新话落到底部）。
2. 当前话加载完成后即预渲染后续 PREFETCH_COUNT（≥3）话，走既有串行队列。

离线（offscreen）：_ComicImageLabel 替换为不触网的占位标签；内容用假
Content（fetch_comic_pages 即时返回），不触达真实网络。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtCore import QEventLoop, QTimer

from framework.content import Chapter, Detail
from gui.pages.reader import comic_view as cv


def _detail(n: int = 6) -> Detail:
    return Detail(
        source_id="demo", content_type="comic", url="http://c.example/m/1",
        title="漫画",
        chapters=[
            Chapter(f"第{i+1}话", f"http://c.example/m/1/{i}.html") for i in range(n)
        ],
    )


class _StubLabel(cv._ComicImageLabel):
    """占位图片标签：不触网，保留 min-height 撑起 gallery 高度。"""

    def load(self):
        pass


class _FakeContent:
    """假 Content：即时返回一页图片，避免真实网络。"""

    def __init__(self):
        self.calls = []

    def fetch_comic_pages(self, source, url, on_page=None, cancel_evt=None):
        self.calls.append(url)
        return [f"http://img.example/{abs(hash(url)) % 1000}/0.jpg"]


@pytest.fixture(scope="module")
def app(_qapp):
    return _qapp


def _make_view(content=None, cached=True):
    view = cv.ComicView(content if content is not None else _FakeContent())
    view._source = SimpleNamespace(source_id="demo")
    view._mode = "gallery"
    d = _detail()
    view._detail = d
    view._chapters = d.chapters
    if cached:
        for ch in d.chapters:
            ch._cached_images = [f"http://img.example/{i}/x.jpg" for i in range(6)]
    view.resize(600, 800)
    view.show()
    return view, d


def _wait(ms: int = 350) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _dispose(view) -> None:
    """销毁视图并摘掉应用级事件过滤器（防悬空过滤器污染后续测试）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        try:
            app.removeEventFilter(view)
        except Exception:  # noqa: BLE001
            pass
    view.hide()
    view.setParent(None)
    view.deleteLater()
    if app is not None:
        app.processEvents()


def _pump_until(pred, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        loop = QEventLoop()
        QTimer.singleShot(40, loop.quit)
        loop.exec()
    return pred()


# ------------------------------------------------------------------------- #
# 1. 换话滚动归零到顶部
# ------------------------------------------------------------------------- #
def test_scroll_returns_to_top_on_prev_chapter(app, monkeypatch):
    """读到底部后点「上一话」，滚动条必须归零到顶部（修复前停在底部）。"""
    monkeypatch.setattr(cv, "_ComicImageLabel", _StubLabel)
    view, _d = _make_view()
    view._load_episode(2)
    app.processEvents()
    vb = view.scroll.verticalScrollBar()
    vb.setValue(vb.maximum())  # 读到底部
    app.processEvents()
    assert vb.value() > 0, "前置条件：应已在底部"

    view._load_episode(1)  # 上一话
    app.processEvents()
    _wait()  # 越过 _apply_zoom 的 160ms 比例恢复
    app.processEvents()
    assert vb.value() == 0, f"上一话应归零到顶部，got {vb.value()}/{vb.maximum()}"
    _dispose(view)


def test_scroll_returns_to_top_on_next_chapter(app, monkeypatch):
    """读到底部后点「下一话」，滚动条同样必须归零到顶部。"""
    monkeypatch.setattr(cv, "_ComicImageLabel", _StubLabel)
    view, _d = _make_view()
    view._load_episode(0)
    app.processEvents()
    vb = view.scroll.verticalScrollBar()
    vb.setValue(vb.maximum())
    app.processEvents()
    assert vb.value() > 0, "前置条件：应已在底部"

    view._load_episode(1)  # 下一话
    app.processEvents()
    _wait()
    app.processEvents()
    assert vb.value() == 0, f"下一话应归零到顶部，got {vb.value()}/{vb.maximum()}"
    _dispose(view)


def test_scroll_returns_to_top_on_toc_jump(app, monkeypatch):
    """目录跳转（_load_episode）同样归零到顶部。"""
    monkeypatch.setattr(cv, "_ComicImageLabel", _StubLabel)
    view, _d = _make_view()
    view._load_episode(0)
    app.processEvents()
    vb = view.scroll.verticalScrollBar()
    vb.setValue(vb.maximum())
    app.processEvents()

    view._load_episode(3)
    app.processEvents()
    _wait()
    app.processEvents()
    assert vb.value() == 0, f"目录跳转应归零到顶部，got {vb.value()}/{vb.maximum()}"
    _dispose(view)


def test_restore_position_still_works_on_initial_load(app, monkeypatch):
    """续读恢复不被破坏：首次 load 的 _pending_position 仍被恢复（非强制归零）。"""
    monkeypatch.setattr(cv, "_ComicImageLabel", _StubLabel)
    view, _d = _make_view()
    calls = []
    monkeypatch.setattr(view, "_restore_position_with_retry", lambda pos: calls.append(pos))
    view._pending_position = 0.5
    view._current_idx = 0
    view._finish_episode_load(view._chapters[0])
    assert calls == [0.5], "首次 load 的续读位置应被恢复"
    assert view._pending_position is None
    _dispose(view)


# ------------------------------------------------------------------------- #
# 2. 后续话预加载
# ------------------------------------------------------------------------- #
def test_prefetch_count_at_least_three():
    """预加载后续话数应 ≥ 3。"""
    assert cv.PREFETCH_COUNT >= 3, f"PREFETCH_COUNT={cv.PREFETCH_COUNT}"


def test_finish_load_prefetches_next_chapters(app, monkeypatch):
    """当前话加载完成后，后续 PREFETCH_COUNT 话进入预取缓存（假 content 即时返回）。"""
    monkeypatch.setattr(cv, "_ComicImageLabel", _StubLabel)
    content = _FakeContent()
    view, d = _make_view(content, cached=False)  # 不预缓存后续话 → 走串行预取队列

    view._load_episode(0)
    app.processEvents()

    def _all_prefetched():
        return all(d.chapters[j].url in view._prefetched for j in range(1, cv.PREFETCH_COUNT + 1))

    ok = _pump_until(_all_prefetched, timeout=5.0)
    assert ok, f"后续 {cv.PREFETCH_COUNT} 话应被预取，prefetched={list(view._prefetched)}"
    for j in range(1, cv.PREFETCH_COUNT + 1):
        entry = view._prefetched[d.chapters[j].url]
        assert entry["images"], f"第{j+1}话预取结果不应为空"
    _dispose(view)


def test_prefetch_future_queues_serially(app, monkeypatch):
    """_prefetch_future 入队后同一时间只启动 1 个预渲染任务（串行）。"""
    view, d = _make_view(cached=False)
    started = []
    monkeypatch.setattr(view, "_start_next_prefetch", lambda: started.append(1))
    view._prefetch_future(0, cv.PREFETCH_COUNT)
    # 队首为最近的后续话，顺序 idx+1, idx+2, idx+3
    assert view._prefetch_queue[: cv.PREFETCH_COUNT] == [
        d.chapters[j].url for j in range(1, cv.PREFETCH_COUNT + 1)
    ]
    assert started == [1], "空闲时应只启动一个预渲染任务"
    _dispose(view)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
