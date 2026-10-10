# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])


class _FakeShelf:
    def folders(self):
        return []

    def list_items(self, **kwargs):
        return []


class _ShelfItem:
    def __init__(self, rec):
        self._rec = rec

    def to_rec(self):
        return dict(self._rec)


class _RenderingShelf(_FakeShelf):
    def __init__(self, rec):
        self._rec = rec

    def list_items(self, **kwargs):
        return [_ShelfItem(self._rec)]


class _FakeSource:
    source_id = "src"
    content_type = "novel"


class _FakeSourceManager:
    def get(self, source_id):
        return _FakeSource()


class _MissingSourceManager:
    def get(self, source_id):
        return None


class _RaisingSourceManager:
    def get(self, source_id):
        raise RuntimeError("source lookup failed")


_UNSET = object()


class _ControlledContent:
    def __init__(self, detail=_UNSET):
        self.detail = {"title": "loaded"} if detail is _UNSET else detail
        self.detail_calls = []
        self.chapter_calls = []
        self.body_calls = []
        self.precache_calls = []
        self.cover_calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.fail = False

    def fetch_detail(self, source, url):
        self.detail_calls.append((source.source_id, url, source.content_type))
        self.started.set()
        self.release.wait(2)
        if self.fail:
            raise RuntimeError("detail failed")
        return self.detail

    def fetch_chapter(self, *args, **kwargs):
        self.chapter_calls.append((args, kwargs))
        raise AssertionError("chapter API must not be called")

    def fetch_body(self, *args, **kwargs):
        self.body_calls.append((args, kwargs))
        raise AssertionError("body API must not be called")

    def precache_chapters(self, *args, **kwargs):
        self.precache_calls.append((args, kwargs))
        raise AssertionError("precache API must not be called")

    def fetch_cover(self, *args, **kwargs):
        self.cover_calls.append((args, kwargs))
        return "cover-data"


def _page(content=None, source_manager=None, shelf=None):
    from gui.pages.library_page import LibraryPage

    return LibraryPage(
        shelf_service=shelf or _FakeShelf(),
        content=content,
        source_manager=source_manager or _FakeSourceManager(),
    )


def _wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def _online(url="u1"):
    return {
        "title": "在线书",
        "kind": "favorite",
        "source_id": "src",
        "url": url,
        "content_type": "novel",
    }


def test_startup_render_ignores_historical_detail_without_eager_reads():
    content = _ControlledContent()
    historical_detail = SimpleNamespace(
        title="历史详情",
        chapters=[SimpleNamespace(title="第一章", url="chapter-1")],
    )
    rec = {**_online(), "detail": historical_detail}
    page = _page(content, shelf=_RenderingShelf(rec))

    page._render([rec])

    assert content.detail_calls == []
    assert content.chapter_calls == []
    assert content.body_calls == []
    assert content.precache_calls == []
    assert content.cover_calls == []


def test_repeated_scan_result_does_not_duplicate_shelf_cards():
    page = _page()
    records = [{**_online("book-a"), "title": "书甲"},
               {**_online("book-b"), "title": "书乙"}]

    page._render(records)
    page._render(records)

    assert len(page._cards_by_key) == 2
    assert page.body.count() == 2
    grid = page.body.itemAt(1).layout()
    assert grid is not None
    assert grid.count() == 2


def test_render_does_not_fetch_detail_chapter_or_body():
    content = _ControlledContent()
    page = _page(content)
    page._render([_online()])
    assert content.detail_calls == []
    assert content.chapter_calls == []
    assert content.body_calls == []


def test_online_click_loads_detail_before_emitting_open():
    content = _ControlledContent()
    page = _page(content)
    opened = []
    page.open_online_requested.connect(opened.append)

    page._open_rec(_online())
    assert content.started.wait(1)
    assert opened == []
    content.release.set()
    _wait_for(lambda: len(opened) == 1)
    assert opened == [("src", "u1", "novel")]
    assert content.detail_calls == [("src", "u1", "novel")]


def test_local_epub_and_video_use_local_signals_without_online_calls():
    content = _ControlledContent()
    page = _page(content)
    epubs = []
    videos = []
    online = []
    page.open_epub_requested.connect(epubs.append)
    page.play_local_video_requested.connect(videos.append)
    page.open_online_requested.connect(online.append)

    epub = {"kind": "local", "content_type": "novel", "path": "book.epub"}
    video = {
        "kind": "local",
        "content_type": "video",
        "path": "video",
        "episode_paths": ["video/1.mp4"],
    }
    page._open_rec(epub)
    page._open_rec(video)

    assert epubs == ["book.epub"]
    assert videos == [video]
    assert online == []
    assert content.detail_calls == []


def test_stale_detail_result_does_not_open_after_newer_click():
    content = _ControlledContent()
    page = _page(content)
    opened = []
    page.open_online_requested.connect(opened.append)

    page._open_rec(_online("a"))
    assert content.started.wait(1)
    page._open_rec(_online("b"))
    _wait_for(lambda: len(content.detail_calls) == 2)
    assert content.detail_calls == [("src", "a", "novel"), ("src", "b", "novel")]
    content.release.set()
    _wait_for(lambda: len(content.detail_calls) == 2 and not page._detail_inflight)
    assert opened == [("src", "b", "novel")]


def test_duplicate_inflight_click_starts_one_request():
    content = _ControlledContent()
    page = _page(content)
    page._open_rec(_online())
    assert content.started.wait(1)
    page._open_rec(_online())
    assert content.detail_calls == [("src", "u1", "novel")]
    content.release.set()
    _wait_for(lambda: not page._detail_inflight)


def test_missing_source_or_content_does_not_emit_open():
    opened = []
    missing_source_page = _page(_ControlledContent(), _MissingSourceManager())
    missing_source_page.open_online_requested.connect(opened.append)
    missing_source_page._open_rec(_online())

    missing_content_page = _page(None)
    missing_content_page.open_online_requested.connect(opened.append)
    missing_content_page._open_rec(_online())

    assert opened == []
    assert missing_source_page._detail_inflight == {}
    assert missing_content_page._detail_inflight == {}


def test_source_lookup_error_does_not_bubble_or_emit():
    page = _page(_ControlledContent(), _RaisingSourceManager())
    opened = []
    page.open_online_requested.connect(opened.append)

    page._open_rec(_online())

    assert opened == []
    assert page._detail_inflight == {}


def test_none_detail_does_not_emit_and_can_retry():
    content = _ControlledContent(detail=None)
    page = _page(content)
    opened = []
    page.open_online_requested.connect(opened.append)

    page._open_rec(_online())
    assert content.started.wait(1)
    content.release.set()
    _wait_for(lambda: not page._detail_inflight)
    assert opened == []

    content.started.clear()
    content.release.clear()
    content.detail = {"title": "retry"}
    page._open_rec(_online())
    assert content.started.wait(1)
    content.release.set()
    _wait_for(lambda: len(opened) == 1)
    assert opened == [("src", "u1", "novel")]
    assert len(content.detail_calls) == 2


def test_failed_detail_clears_state_and_retry_starts_again():
    content = _ControlledContent()
    page = _page(content)
    opened = []
    page.open_online_requested.connect(opened.append)

    content.fail = True
    page._open_rec(_online())
    assert content.started.wait(1)
    content.release.set()
    _wait_for(lambda: not page._detail_inflight)
    assert opened == []

    content.started.clear()
    content.release.clear()
    content.fail = False
    page._open_rec(_online())
    assert content.started.wait(1)
    content.release.set()
    _wait_for(lambda: len(opened) == 1)
    assert len(content.detail_calls) == 2
