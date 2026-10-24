# -*- coding: utf-8 -*-
"""书架封面加载回归：不能被 cache-only 卡住，补写后要就地刷新卡片。"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from gui.components import cover_loader as cl
from gui.pages import library_page as lp_mod

_app = QApplication.instance() or QApplication([])


class _FakeLoader:
    def __init__(self):
        self.calls = []

    def load(self, url, callback, *args, **kwargs):
        self.calls.append((url, callback, args, kwargs))

    def load_cached(self, *args, **kwargs):
        raise AssertionError("书架封面必须允许网络回源，不能只用 cache-only")

    def busy(self):
        return False


def _install_loader(monkeypatch):
    fake = _FakeLoader()
    monkeypatch.setattr(cl.CoverLoader, "instance", staticmethod(lambda: fake))
    return fake


def _rec(**overrides):
    rec = {
        "kind": "favorite",
        "url": "http://site/book/1",
        "source_id": "src",
        "content_type": "novel",
        "cover": "http://img/1.jpg",
    }
    rec.update(overrides)
    return rec


def test_shelf_card_fetches_uncached_cover_and_persists(monkeypatch):
    fake = _install_loader(monkeypatch)
    card = lp_mod._ShelfCard(_rec())

    assert len(fake.calls) == 1
    url, callback, _args, kwargs = fake.calls[0]
    assert url == "http://img/1.jpg"
    assert callback == card._on_cover_ready
    assert kwargs["persist"] is True
    assert kwargs["source_id"] == "src"


def test_update_cover_refreshes_card_in_place(monkeypatch):
    fake = _install_loader(monkeypatch)
    card = lp_mod._ShelfCard(_rec(cover=""))
    holder = SimpleNamespace(
        _cards_by_key={card.rec["url"]: card}, _render_plan=[]
    )

    lp_mod.LibraryPage.update_cover(holder, card.rec["url"], "http://img/2.jpg")

    assert card.rec["cover"] == "http://img/2.jpg"
    assert fake.calls[-1][0] == "http://img/2.jpg"
    assert fake.calls[-1][3]["persist"] is True

def test_prompt_unlock_does_not_ask_again_in_same_session(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from framework.library_store import LibraryStore

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("secret", locked=True, pw="hash", salt="00" * 16)
    holder = SimpleNamespace(_store=store, _unlocked={"secret"})

    def _fail(*_args, **_kwargs):
        raise AssertionError("已解锁的收藏夹不应再次弹窗")

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(_fail))
    assert lp_mod.LibraryPage._prompt_unlock(holder, "secret") is True

def test_more_than_30_favorites_render_lazily(monkeypatch):
    _install_loader(monkeypatch)

    class _Shelf:
        def folders(self):
            return []

        def list_items(self, **_kwargs):
            return []

    page = lp_mod.LibraryPage(shelf_service=_Shelf())
    records = [
        {"kind": "favorite", "url": f"http://site/book/{i}", "source_id": "src",
         "content_type": "novel", "title": f"书{i}", "cover": ""}
        for i in range(31)
    ]

    page._render(records)
    assert len(page._shelf_cards) == 30
    assert page._has_more_cards()

    page._render_batch()
    assert len(page._shelf_cards) == 31
    assert not page._has_more_cards()
