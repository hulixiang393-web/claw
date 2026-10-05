# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.app import MainWindow


def _record_theme(monkeypatch):
    applied = []
    original = MainWindow._apply_theme_qss

    def record_theme(window, theme):
        applied.append(theme)
        original(window, theme)

    monkeypatch.setattr(MainWindow, "_apply_theme_qss", record_theme)
    return applied


def test_initial_theme_is_applied_once_when_event_loop_is_idle(_qapp, monkeypatch):
    applied = _record_theme(monkeypatch)
    window = MainWindow()

    assert applied == []

    _qapp.processEvents()
    assert applied == [window.theme_manager.current_key()]

    _qapp.processEvents()
    assert applied == [window.theme_manager.current_key()]
    window.close()


def test_explicit_theme_switch_is_immediate_and_cancels_pending_initial_apply(
    _qapp, monkeypatch
):
    applied = _record_theme(monkeypatch)
    window = MainWindow()

    window.theme_manager.switch_to("midnight")
    assert applied == ["midnight"]

    _qapp.processEvents()
    assert applied == ["midnight"]
    window.close()
