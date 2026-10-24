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


def test_reentry_keeps_lazy_loaded_cards_and_remaining_plan():
    page = _page()
    records = [
        {**_online(f"book-{i}"), "title": f"书{i}"}
        for i in range(50)
    ]
    page._render(records)
    assert len(page._shelf_cards) == 30
    page._render_batch(15)
    assert len(page._shelf_cards) == 45
    first_card = page._shelf_cards[0]

    page._render(records, reuse=True)

    assert len(page._shelf_cards) == 45
    assert page._shelf_cards[0] is first_card
    assert len(page._cards_by_key) == 45
    assert page._has_more_cards()
    page._render_batch(15)
    assert len(page._shelf_cards) == 50
    assert not page._has_more_cards()


def test_same_view_rebuild_keeps_loaded_count():
    page = _page()
    records = [
        {**_online(f"book-{i}"), "title": f"书{i}"}
        for i in range(50)
    ]
    page._render(records)
    page._render_batch(15)
    assert len(page._shelf_cards) == 45

    page._render(records, reuse=False)

    assert len(page._shelf_cards) == 45
    assert page._has_more_cards()


def test_render_order_change_keeps_loaded_count_without_reloading_first_batch():
    page = _page()
    records = [
        {**_online(f"book-{i}"), "title": f"书{i}"}
        for i in range(40)
    ]
    page._render(records)
    page._render_batch(10)
    assert len(page._shelf_cards) == 40

    page._render(list(reversed(records)), reuse=True)

    assert len(page._shelf_cards) == 40


def test_render_does_not_fetch_detail_chapter_or_body():
    content = _ControlledContent()
    page = _page(content)
    page._render([_online()])
    assert content.detail_calls == []
    assert content.chapter_calls == []
    assert content.body_calls == []


def test_activate_does_not_refresh_progress_synchronously(monkeypatch):
    from gui.pages import library_page as module

    tasks = []
    monkeypatch.setattr(module.QThreadPool, "globalInstance",
                        lambda: SimpleNamespace(start=tasks.append))

    class _Shelf(_FakeShelf):
        def __init__(self):
            self.refresh_calls = 0

        def refresh_metadata(self, snapshot):
            self.refresh_calls += 1
            return snapshot

    shelf = _Shelf()
    page = _page(shelf=shelf)
    assert tasks
    tasks[0].run()
    QApplication.processEvents()

    page.activate()
    assert shelf.refresh_calls == 0


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


def test_folder_filter_reuses_metadata_snapshot_without_content_reads(tmp_path, monkeypatch):
    from framework.library_store import LibraryStore
    from framework.reading_progress import ReadingProgress
    from framework.shelf_service import ShelfService
    from gui.components.cover_loader import CoverLoader

    folder = tmp_path / "downloads" / "本地书"
    folder.mkdir(parents=True)
    (folder / "book.epub").write_bytes(b"unread epub")
    store = LibraryStore(tmp_path / "library.json")
    store.add("src", "u1", "本地书", content_type="novel", folder="甲",
              cover="https://example.test/cover.png")
    store.add("src", "u2", "在线书", content_type="comic", folder="乙")
    content = _ControlledContent()
    reads = []
    progress = ReadingProgress(tmp_path / "progress.json")
    repository = SimpleNamespace(
        get_location=lambda *a: None,
        cached_chapter_count=lambda *a: 0,
        get_book_snapshot=lambda *a: reads.append("detail/chapters/images"),
        cached_chapter_keys=lambda *a: reads.append("chapter directory"),
        get_content=lambda *a: reads.append("body/media"),
    )
    progress._repository = repository
    monkeypatch.setattr(progress, "resume", lambda *a: reads.append("resume"))
    service = ShelfService(folder.parent, library_store=store, reading_progress=progress,
                           data_dir=tmp_path, epub_detector=lambda *a: reads.append("epub"))
    scans = []
    scan = service.scan_local
    monkeypatch.setattr(service, "scan_local", lambda *a, **kw: (scans.append(kw), scan(*a, **kw))[1])
    cached = []
    loader = CoverLoader.instance()
    monkeypatch.setattr(loader, "load", lambda *a, **kw: reads.append("network"))
    monkeypatch.setattr(loader, "load_cached", lambda url, callback, **kw: cached.append(url))
    page = _page(content, shelf=service)
    page._shelf_cache_repository = repository
    backfills = []
    page._cover_backfiller = backfills.append
    _wait_for(lambda: page._metadata_snapshot is not None)
    original_snapshot = page._metadata_snapshot
    page.folder_combo.setCurrentText("甲")
    page.folder_combo.setCurrentText("乙")
    page.folder_combo.setCurrentText("全部")
    assert page._metadata_snapshot is original_snapshot
    page.activate()
    assert len(scans) == 1
    assert [r for r in reads if r != "network"] == []
    assert backfills  # 封面缺失 -> 触发后台补写
    assert cached == []  # 书架不再 cache-only，允许联网回源
    assert content.detail_calls == content.chapter_calls == content.body_calls == []
    assert original_snapshot[0][0].url == ""
    assert original_snapshot[0][0].folder == ""
    assert set(page._cards_by_key) == {"u2"}
    page._open_rec(_online("u2"))
    assert content.started.wait(1)
    content.release.set()
    _wait_for(lambda: not page._detail_inflight)
    assert len(content.detail_calls) == 1


def test_same_filter_old_scan_cannot_overwrite_new_snapshot(monkeypatch):
    from framework.shelf_service import ShelfItem
    from gui.pages import library_page as module

    tasks = []
    monkeypatch.setattr(module.QThreadPool, "globalInstance",
                        lambda: SimpleNamespace(start=tasks.append))
    page = _page()
    first = tasks[0]
    page.activate()
    assert len(tasks) == 1
    page.refresh()
    second = tasks[1]
    assert first.cancel.is_set()
    assert first.filters == second.filters
    assert first.generation != second.generation
    newest = ([], [ShelfItem("new", "favorite", "新", "novel", url="new")])
    oldest = ([], [ShelfItem("old", "favorite", "旧", "novel", url="old")])
    second.signals.done.emit(second.generation, second.filters, newest)
    first.signals.done.emit(first.generation, first.filters, oldest)
    assert set(page._cards_by_key) == {"new"}
    assert page._metadata_snapshot is newest


def test_filter_change_during_scan_uses_current_filter(monkeypatch, tmp_path):
    from framework.library_store import LibraryStore
    from framework.shelf_service import ShelfService
    from gui.pages import library_page as module

    tasks = []
    monkeypatch.setattr(module.QThreadPool, "globalInstance",
                        lambda: SimpleNamespace(start=tasks.append))
    store = LibraryStore(tmp_path / "library.json")
    store.add("src", "u1", "甲书", folder="甲")
    store.add("src", "u2", "乙书", folder="乙")
    service = ShelfService(tmp_path / "downloads", library_store=store, data_dir=tmp_path)
    page = _page(shelf=service)
    page.folder_combo.setCurrentText("乙")
    assert len(tasks) == 1
    tasks[0].run()
    assert set(page._cards_by_key) == {"u2"}
