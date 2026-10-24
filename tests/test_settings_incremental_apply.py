# -*- coding: utf-8 -*-
"""设置页「独立加载」回归：应用时只写回发生变化的设置项。

对应需求：每个设置项带一个布尔修改标记，只加载/应用真正修改过的项，
未修改项完全跳过，避免整份配置重写与全局刷新。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QMessageBox

from framework.settings_manager import SettingsManager
from framework.theme_manager import ThemeManager
from gui.pages.settings_page import SettingsPage

_app = QApplication.instance() or QApplication([])


def _make_page(tmp_path) -> tuple[SettingsPage, SettingsManager]:
    sm = SettingsManager(tmp_path / "app_config.json")
    page = SettingsPage(sm, ThemeManager(sm))
    return page, sm


def test_apply_only_writes_changed_setting(tmp_path, monkeypatch):
    page, sm = _make_page(tmp_path)
    writes: list[tuple[str, str, object]] = []
    original_set = SettingsManager.set

    def _record(self, section, key, value):
        writes.append((section, key, value))
        return original_set(self, section, key, value)

    monkeypatch.setattr(SettingsManager, "set", _record)

    page._net_timeout.setValue(page._net_timeout.value() + 1)
    applied: list[frozenset] = []
    page.settings_applied.connect(applied.append)

    page._on_apply()

    written = [f"{s}.{k}" for s, k, _ in writes]
    assert written, "没有任何设置被写回"
    assert written == ["network.default_timeout"], written
    assert applied and applied[-1] == frozenset({"network.default_timeout"})


def test_apply_without_changes_skips_all_writes(tmp_path, monkeypatch):
    page, _sm = _make_page(tmp_path)
    writes: list[str] = []
    original_set = SettingsManager.set

    def _record(self, section, key, value):
        writes.append(f"{section}.{key}")
        return original_set(self, section, key, value)

    monkeypatch.setattr(SettingsManager, "set", _record)
    applied: list[frozenset] = []
    page.settings_applied.connect(applied.append)

    page._on_apply()

    assert writes == []
    assert applied == [frozenset()]


def test_dirty_flags_exist_for_every_setting(tmp_path):
    page, _sm = _make_page(tmp_path)
    assert set(page._dirty_flags) == set(page._baseline) == set(page._SETTING_KEYS)
    assert all(isinstance(v, bool) and v is False for v in page._dirty_flags.values())

    page._dl_concurrent.setValue(page._dl_concurrent.value() + 1)
    page._on_apply()
    assert page._dirty_flags["download.max_concurrent_downloads"] is True
    assert page._dirty_flags["network.default_timeout"] is False


def test_apply_updates_baseline_so_second_apply_is_noop(tmp_path, monkeypatch):
    page, _sm = _make_page(tmp_path)
    page._ui_font.setValue(1.2)
    page._on_apply()

    writes: list[str] = []
    original_set = SettingsManager.set

    def _record(self, section, key, value):
        writes.append(f"{section}.{key}")
        return original_set(self, section, key, value)

    monkeypatch.setattr(SettingsManager, "set", _record)
    page._on_apply()
    assert writes == []


def test_reset_emits_full_set_and_resets_flags(tmp_path):
    page, sm = _make_page(tmp_path)
    page._dl_concurrent.setValue(2)
    page._on_apply()

    page.settings_applied.connect(lambda changed: None)
    original = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    try:
        page._on_reset()
    finally:
        QMessageBox.question = original

    assert sm.get("download", "max_concurrent_downloads") == 6
    assert all(v is False for v in page._dirty_flags.values())
