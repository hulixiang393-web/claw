# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QWidget

from gui import app as app_module
from gui.app import MainWindow, TABS


def _stub_page_builders(monkeypatch):
    built = []

    def make_builder(name):
        def build(window):
            built.append(name)
            return QWidget()

        return build

    for name in (
        "home",
        "discover",
        "reader",
        "search",
        "download",
        "library",
        "source",
        "settings",
    ):
        monkeypatch.setattr(MainWindow, f"_build_{name}", make_builder(name))
    return built


def _window(monkeypatch, tmp_path):
    config = tmp_path / "app_config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app_module, "_app_base_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(MainWindow, "_schedule_source_selection", lambda self: None)
    return MainWindow()


def test_startup_keeps_lazy_pages_unbuilt_and_does_not_schedule_diagnostics(
    _qapp, monkeypatch, tmp_path
):
    built = _stub_page_builders(monkeypatch)
    diagnostics = []
    monkeypatch.setattr(
        MainWindow,
        "_schedule_startup_diag",
        lambda self: diagnostics.append("scheduled"),
    )

    window = _window(monkeypatch, tmp_path)

    assert window.tabs.count() == len(TABS)
    assert diagnostics == []
    assert "library" not in built
    assert "discover" not in built
    assert "search" not in built
    assert "source" not in built
    assert "settings" not in built
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
        label for label, _ in TABS
    ]
    _qapp.processEvents()
    window.close()


def test_selecting_library_builds_once_and_reuses_the_same_page(
    _qapp, monkeypatch, tmp_path
):
    built = _stub_page_builders(monkeypatch)
    window = _window(monkeypatch, tmp_path)
    assert "library" not in built

    library_idx = next(i for i, (_, key) in enumerate(TABS) if key == "library")
    window.tabs.setCurrentIndex(library_idx)
    first = window.tabs.widget(library_idx)
    window.tabs.setCurrentIndex(0)
    window.tabs.setCurrentIndex(library_idx)

    assert built.count("library") == 1
    assert window.tabs.widget(library_idx) is first
    _qapp.processEvents()
    window.close()


def test_replacing_library_page_preserves_selected_tab_without_reentrant_builds(
    _qapp, monkeypatch, tmp_path
):
    built = _stub_page_builders(monkeypatch)
    window = _window(monkeypatch, tmp_path)

    library_idx = next(i for i, (_, key) in enumerate(TABS) if key == "library")
    window.tabs.setCurrentIndex(library_idx)

    assert window.tabs.currentIndex() == library_idx
    assert TABS[window.tabs.currentIndex()][1] == "library"
    assert built.count("library") == 1
    assert all(name not in built for name in ("search", "source", "settings"))
    _qapp.processEvents()
    window.close()


def test_replacing_discover_page_preserves_selected_tab_without_reentrant_builds(
    _qapp, monkeypatch, tmp_path
):
    built = _stub_page_builders(monkeypatch)
    window = _window(monkeypatch, tmp_path)

    discover_idx = next(i for i, (_, key) in enumerate(TABS) if key == "discover")
    window.tabs.setCurrentIndex(discover_idx)

    assert window.tabs.currentIndex() == discover_idx
    assert TABS[window.tabs.currentIndex()][1] == "discover"
    assert built.count("discover") == 1
    assert all(name not in built for name in ("search", "source", "settings"))
    _qapp.processEvents()
    window.close()
