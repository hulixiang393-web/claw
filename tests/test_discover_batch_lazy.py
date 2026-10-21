# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtWidgets import QApplication, QInputDialog

from gui.components.work_card import WorkCard
from gui.pages.discover_page import DiscoverPage


class _FakeManager:
    def discoverable_sources(self):
        return []

    def get_health(self, sid):
        return SimpleNamespace(state="ok", last_error="")

    def get(self, sid):
        return None


class _Work:
    def __init__(self, i):
        self.title = f"作品{i}"
        self.url = f"http://x.example/w/{i}"
        self.source_id = "demo"
        self.cover = ""
        self.author = ""
        self.source_name = "演示站"
        self.update = ""


@pytest.fixture(scope="module")
def app(_qapp):
    return _qapp


def _make_page(provider=None):
    page = DiscoverPage(
        _FakeManager(), None, None, None, None, None,
        shelf_folders_provider=provider,
    )
    page.resize(900, 700)
    page.show()
    return page


def _dispose(page) -> None:
    page.hide()
    page.setParent(None)
    page.deleteLater()
    a = QApplication.instance()
    if a is not None:
        a.processEvents()


def _cards(page):
    return page.list_container.findChildren(WorkCard)


def test_append_works_renders_only_first_batch(app):
    page = _make_page()
    page._append_works([_Work(i) for i in range(60)])
    expected = page._batch_size(page._last_columns)
    assert len(page._works) == 60
    assert page._work_count == expected
    assert page._work_count < len(page._works)
    _dispose(page)


def test_scroll_renders_next_batch_then_requests_next_page(app):
    page = _make_page()
    page._append_works([_Work(i) for i in range(60)])
    first = page._work_count
    calls = []
    page._load_next_page = lambda *a, **k: calls.append((a, k))
    vbar = page.scroll.verticalScrollBar()
    vbar.blockSignals(True)
    vbar.setRange(0, 1000)
    vbar.setValue(1000)
    vbar.blockSignals(False)
    page._on_scroll(1000)
    assert page._work_count > first
    assert page._work_count == min(first + page._batch_size(page._last_columns), 60)
    assert len(calls) == 1
    _dispose(page)


def test_reflow_rebuilds_only_rendered_prefix(app):
    page = _make_page()
    page._append_works([_Work(i) for i in range(60)])
    rendered = page._work_count
    page._last_columns = 1
    page._reflow()
    assert page._work_count == rendered
    assert len(page._works) == 60
    _dispose(page)


def test_cards_are_selectable_and_checked_state_restores(app):
    page = _make_page()
    page._append_works([_Work(i) for i in range(3)])
    card = _cards(page)[0]
    assert card._checkbox is not None
    url = card.work.url
    card.set_checked(True)
    assert url in page._selected
    page._on_card_checked(card.work, True)
    assert len(page._selected) == 1
    card.set_checked(False)
    assert url not in page._selected
    _dispose(page)


def test_select_all_covers_loaded_works_and_clear(app):
    page = _make_page()
    page._append_works([_Work(i) for i in range(60)])
    assert page._work_count < 60
    page._on_select_all(True)
    assert len(page._selected) == 60
    assert page.select_all_check.isChecked()
    assert page.batch_bar.isVisible()
    assert "60" in page.batch_count.text()
    assert all(card.is_checked() for card in _cards(page))
    page._clear_selection()
    assert page._selected == {}
    assert not page.batch_bar.isVisible()
    assert not page.select_all_check.isChecked()
    assert not any(card.is_checked() for card in _cards(page))
    _dispose(page)


def test_select_all_ignores_unloaded_works(app):
    page = _make_page()
    page._append_works([_Work(i) for i in range(10)])
    page._on_select_all(True)
    assert len(page._selected) == 10
    _dispose(page)


def test_batch_add_shelf_signal_and_folder_provider(app, monkeypatch):
    page = _make_page(provider=lambda: ["收藏夹A", "全部"])
    page._append_works([_Work(i) for i in range(3)])
    page._on_select_all(True)
    seen = []
    monkeypatch.setattr(
        QInputDialog,
        "getItem",
        lambda parent, title, label, items, current, editable: (
            seen.append(list(items)) or ("收藏夹A", True)
        ),
    )
    got = []
    page.add_to_shelf_requested.connect(
        lambda items, folder: got.append((items, folder))
    )
    page._on_batch_add_shelf()
    assert len(got) == 1
    items, folder = got[0]
    assert len(items) == 3
    assert folder == "收藏夹A"
    assert seen == [["未分类", "收藏夹A"]]
    assert page._selected == {}
    _dispose(page)


def test_batch_add_shelf_cancel_keeps_selection(app, monkeypatch):
    page = _make_page()
    page._append_works([_Work(i) for i in range(3)])
    page._on_select_all(True)
    monkeypatch.setattr(
        QInputDialog,
        "getItem",
        lambda *a, **k: ("未分类", False),
    )
    got = []
    page.add_to_shelf_requested.connect(lambda *a: got.append(a))
    page._on_batch_add_shelf()
    assert got == []
    assert len(page._selected) == 3
    _dispose(page)


def test_batch_download_signal_and_clear(app):
    page = _make_page()
    page._append_works([_Work(i) for i in range(3)])
    page._on_select_all(True)
    got = []
    page.batch_download_requested.connect(lambda items: got.append(items))
    page._on_batch_download()
    assert len(got) == 1
    assert len(got[0]) == 3
    assert page._selected == {}
    assert not page.batch_bar.isVisible()
    _dispose(page)


def test_source_switch_clears_selection(app):
    page = _make_page()
    page._append_works([_Work(i) for i in range(3)])
    page._on_select_all(True)
    page._try_restore_source_snapshot = lambda: False
    page._load_categories = lambda: None
    source = SimpleNamespace(
        source_id="s2",
        source_name="源2",
        base_url="http://x.example",
        get_discovery_config=lambda: {"works_list_url": "/list"},
    )
    page.source_combo.blockSignals(True)
    page.source_combo.addItem("源2", source)
    page.source_combo.setCurrentIndex(page.source_combo.count() - 1)
    page.source_combo.blockSignals(False)
    page._on_source_changed(page.source_combo.currentIndex())
    assert page._selected == {}
    assert not page.batch_bar.isVisible()
    _dispose(page)


def test_category_reset_clears_selection(app):
    page = _make_page()
    page._append_works([_Work(i) for i in range(3)])
    page._on_select_all(True)
    page._reset_works()
    assert page._selected == {}
    _dispose(page)
