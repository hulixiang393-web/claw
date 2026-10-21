# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])


class _Obj:
    def __init__(self, rec):
        self._rec = rec

    def to_rec(self):
        return dict(self._rec)


class _Shelf:
    def __init__(self):
        self.filter_rec = None

    def folders(self):
        return []

    def list_items(self, **kwargs):
        return []

    def filter_snapshot(self, snapshot, *filters):
        return [_Obj(self.filter_rec)]


def _page(monkeypatch):
    from gui.pages import library_page as module

    started = []
    monkeypatch.setattr(module.QThreadPool, "globalInstance",
                        lambda: SimpleNamespace(start=started.append))
    from gui.pages.library_page import LibraryPage

    return LibraryPage(shelf_service=_Shelf()), started


def _old():
    return {
        "kind": "favorite",
        "url": "u1",
        "source_id": "src",
        "content_type": "novel",
        "title": "old title",
        "reading_status": "reading",
    }


def test_render_reuse_updates_card_in_place(monkeypatch):
    page, _started = _page(monkeypatch)
    page._render([_old()])
    card = page._shelf_cards[0]
    body_count = page.body.count()
    clear_calls = []
    monkeypatch.setattr(page, "_clear_all", lambda: clear_calls.append(1))

    new = {**_old(), "title": "new title", "resume_title": "chapter 3",
           "reading_status": "completed"}
    page._render([new], reuse=True)

    assert clear_calls == []
    assert page._shelf_cards[0] is card
    assert card.rec["title"] == "new title"
    assert "chapter 3" in card._meta_label.text()
    assert card._reading_status_label.text() == "已看完"
    assert page.body.count() == body_count


def test_activate_skips_rebuild_when_cards_exist(monkeypatch):
    page, _started = _page(monkeypatch)
    page._render([_old()])
    page._metadata_snapshot = ([], [])
    rebuilds = []
    queued = []
    monkeypatch.setattr(page, "_rebuild", lambda: rebuilds.append(1))
    monkeypatch.setattr(page, "_queue_metadata_refresh", lambda: queued.append(1))

    page.activate()

    assert rebuilds == []
    assert queued == [1]


def test_metadata_ready_reuses_cards(monkeypatch):
    page, _started = _page(monkeypatch)
    page._render([_old()])
    card = page._shelf_cards[0]
    old_snapshot = ([], [])
    enriched = ([], [])
    page._metadata_snapshot = old_snapshot
    page._metadata_task = object()
    page._scan_generation = 7
    page._shelf.filter_rec = {**_old(), "title": "enriched title",
                              "resume_title": "chapter 9"}
    rebuilds = []
    monkeypatch.setattr(page, "_rebuild", lambda: rebuilds.append(1))

    page._on_metadata_ready(7, old_snapshot, enriched)

    assert rebuilds == []
    assert page._metadata_snapshot is enriched
    assert page._shelf_cards[0] is card
    assert card.rec["title"] == "enriched title"
    assert "chapter 9" in card._meta_label.text()
