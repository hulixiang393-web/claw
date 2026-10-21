# -*- coding: utf-8 -*-
"""书架批量选择与本地删除回归测试（test_library_batch_delete.py）。"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QGridLayout, QMessageBox

from gui.pages import library_page as lp_mod

_app = QApplication.instance() or QApplication([])


class _FakeShelf:
    def folders(self):
        return []

    def list_items(self, **kwargs):
        return []


def _page():
    from gui.pages.library_page import LibraryPage

    return LibraryPage(shelf_service=_FakeShelf())


def _fav(url="u1", title="书"):
    return {"title": title, "source_id": "src", "url": url,
            "kind": "favorite", "content_type": "novel"}


def _cards(page):
    out = []
    for i in range(page.body.count()):
        it = page.body.itemAt(i)
        if it.layout() and isinstance(it.layout(), QGridLayout):
            grid = it.layout()
            out.extend(
                grid.itemAt(j).widget()
                for j in range(grid.count())
                if grid.itemAt(j).widget()
            )
    return out


def test_batch_box_hidden_by_default_and_toggles_with_mode():
    page = _page()
    page._render([_fav()])
    cards = _cards(page)
    assert len(cards) == 1
    box = cards[0].batch_box
    assert box.isHidden()

    page.batch_btn.setChecked(True)
    assert page._batch_mode is True
    assert not box.isHidden()

    box.setChecked(True)
    assert page._batch_selected

    page.batch_btn.setChecked(False)
    assert page._batch_mode is False
    assert box.isHidden()
    assert box.isChecked() is False
    assert page._batch_selected == {}


def test_batch_mode_survives_rebuild_and_keeps_selection():
    page = _page()
    rec = _fav()
    page._render([rec])
    page.batch_btn.setChecked(True)
    _cards(page)[0].batch_box.setChecked(True)

    page._render([dict(rec)])
    cards = _cards(page)
    assert len(cards) == 1
    assert not cards[0].batch_box.isHidden()
    assert cards[0].batch_box.isChecked()
    assert page._batch_selected


def test_close_local_path_only_matches_open_path_and_is_idempotent(tmp_path):
    from gui.pages.reader.epub_view import EpubView
    from gui.pages.reader_page import ReaderPage

    view = EpubView()
    view._path = str(tmp_path / "a.epub")
    reader = SimpleNamespace(epub_view=view)

    assert ReaderPage.close_local_path(reader, str(tmp_path / "b.epub")) is False
    assert view._path
    view._path = str(tmp_path / "dir" / "a.epub")
    assert ReaderPage.close_local_path(reader, str(tmp_path / "dir")) is True
    assert view._path == ""
    view.close_book()
    view.close_book()


def test_close_handler_releases_epub_then_delete_succeeds(tmp_path, monkeypatch):
    from gui.pages.reader.epub_view import EpubView
    from gui.pages.reader_page import ReaderPage

    epub = tmp_path / "book.epub"
    epub.write_bytes(b"PK\x03\x04fake")
    handle = open(epub, "rb")

    class _Epub:
        def __init__(self, fh):
            self._fh = fh
            self.closed = False

        def close(self):
            if not self.closed:
                self.closed = True
                self._fh.close()

    view = EpubView()
    view._epub = _Epub(handle)
    view._path = str(epub)
    view._chapters = [object()]
    view._open_task = object()
    reader = SimpleNamespace(epub_view=view)

    page = _page()
    page.set_local_close_handler(lambda p: ReaderPage.close_local_path(reader, p))
    refreshed = []
    monkeypatch.setattr(page, "refresh", lambda: refreshed.append(True))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)

    page._delete_local({"title": "书", "kind": "local", "path": str(epub)})

    assert getattr(view, "_epub", "missing") is None
    assert view._path == ""
    assert view._chapters == []
    assert getattr(view, "_open_task", "missing") is None
    assert handle.closed
    assert not epub.exists()
    assert refreshed == [True]


def test_delete_failure_warns_once(tmp_path, monkeypatch):
    folder = tmp_path / "bookdir"
    folder.mkdir()
    (folder / "a.epub").write_bytes(b"x")

    page = _page()
    monkeypatch.setattr(page, "refresh", lambda: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    monkeypatch.setattr(lp_mod, "_DELETE_RETRY_DELAY", 0)

    def _locked(*args, **kwargs):
        raise PermissionError("in use")

    monkeypatch.setattr(shutil, "rmtree", _locked)

    page._delete_local({"title": "书", "kind": "local", "path": str(folder)})

    assert len(warnings) == 1
    assert "文件被占用" in warnings[0][2]
    assert folder.exists()


def test_favorite_remove_ignores_cache_clear_failure(tmp_path):
    from framework.shelf_service import ShelfService

    class _Store:
        def remove(self, url):
            return True

    class _Repo:
        def clear_content_cache(self, url):
            raise RuntimeError("cache failure")

    service = ShelfService(
        output_dir=tmp_path / "dl", library_store=_Store(), repository=_Repo()
    )
    assert service.favorite_remove("http://x/1") is True


def test_remove_favorite_warns_once_and_still_activates(monkeypatch):
    page = _page()
    monkeypatch.setattr(page, "_ensure_folder_unlocked", lambda *a, **k: True)

    def _fail(url):
        raise RuntimeError("boom")

    monkeypatch.setattr(page._shelf, "favorite_remove", _fail, raising=False)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    refreshed = []
    monkeypatch.setattr(page, "refresh", lambda: refreshed.append(True))

    page._remove_favorite({"url": "u1", "folder": ""})

    assert len(warnings) == 1
    assert refreshed == [True]


def test_batch_delete_counts_failures_and_warns_once(monkeypatch):
    page = _page()
    page._store = object()
    page._batch_selected = {"u1": {"url": "u1"}, "u2": {"url": "u2"}}
    removed = []

    def _remove(url):
        if url == "u2":
            raise RuntimeError("boom")
        removed.append(url)

    monkeypatch.setattr(page._shelf, "favorite_remove", _remove, raising=False)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    refreshed = []
    monkeypatch.setattr(page, "refresh", lambda: refreshed.append(True))

    page._batch_delete()

    assert removed == ["u1"]
    assert len(warnings) == 1
    assert "1" in warnings[0][2]
    assert refreshed == [True]
    assert page._batch_selected == {}
