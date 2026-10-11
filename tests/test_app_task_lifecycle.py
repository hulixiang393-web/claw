# -*- coding: utf-8 -*-
"""App 后台任务生命周期 / 退出有界等待 / 共享基类（A4/A5/F2）的针对性测试。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui import app as app_module
from gui.app import MainWindow, _AppRunnable


def _make_window(monkeypatch, tmp_path):
    monkeypatch.setattr("gui.app._app_base_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(MainWindow, "_schedule_source_selection", lambda self: None)

    class _Task:
        def __init__(self, *args):
            pass

        def start(self):
            pass

    monkeypatch.setattr("gui.app._StartupTask", _Task)
    return MainWindow()


# --------------------------------------------------------------------------- #
# A4：封面回填任务完成后注销引用，集合不随触发次数无限增长
# --------------------------------------------------------------------------- #
def test_cover_backfill_task_references_do_not_grow(_qapp, monkeypatch, tmp_path):
    window = _make_window(monkeypatch, tmp_path)
    source = SimpleNamespace(base_url="", get_detail_config=lambda: {})
    window.source_manager = SimpleNamespace(get=lambda sid: source)
    window.content = SimpleNamespace(
        fetch_cover=lambda s, u: "https://img.example/c.jpg",
        _is_placeholder_cover=lambda cover, sel: False,
    )

    started = []

    class _InlinePool:
        def start(self, task):
            started.append(task)
            task.run()

    class _InlinePoolType:
        @staticmethod
        def globalInstance():
            return _InlinePool()

    monkeypatch.setattr(app_module, "QThreadPool", _InlinePoolType)

    rec = [{"source_id": "s", "url": "u1", "cover": ""}]
    for _ in range(5):
        window._backfill_favorite_covers(rec)

    assert len(started) == 5
    assert window._cover_backfill_tasks == []  # 完成即注销，无界增长被消除
    window.close()


def test_cover_backfill_finished_signal_removes_only_target(
    _qapp, monkeypatch, tmp_path
):
    window = _make_window(monkeypatch, tmp_path)
    window._cover_backfill_tasks = ["a", "b", "c"]
    window._on_cover_backfill_finished("b")
    assert window._cover_backfill_tasks == ["a", "c"]
    window._on_cover_backfill_finished("missing")  # 幂等，不抛
    assert window._cover_backfill_tasks == ["a", "c"]
    window.close()


# --------------------------------------------------------------------------- #
# A5：退出有界等待线程池与 VLC 预热线程，绝不无限阻塞
# --------------------------------------------------------------------------- #
def test_close_waits_bounded_for_pool_and_vlc(_qapp, monkeypatch, tmp_path):
    window = _make_window(monkeypatch, tmp_path)
    recorded = {}

    class _Pool:
        def waitForDone(self, ms):
            recorded["ms"] = ms
            return False  # 模拟仍未完成 → 走容忍超时分支

    class _PoolType:
        @staticmethod
        def globalInstance():
            return _Pool()

    monkeypatch.setattr(app_module, "QThreadPool", _PoolType)

    class _Thread:
        def __init__(self):
            self.joins = []

        def is_alive(self):
            return len(self.joins) == 0

        def join(self, timeout):
            self.joins.append(timeout)

    thread = _Thread()
    window._vlc_warmup_thread = thread

    start = time.perf_counter()
    window.close()
    elapsed = time.perf_counter() - start

    assert recorded["ms"] == app_module._POOL_WAIT_MS  # 有界（正）超时
    assert thread.joins == [app_module._VLC_WARMUP_JOIN_S]
    assert elapsed < 1.0  # 未因等待被无限阻塞


def test_close_clears_task_references(_qapp, monkeypatch, tmp_path):
    window = _make_window(monkeypatch, tmp_path)
    window._cover_backfill_tasks = ["x"]
    window._bg_compose_tasks = ["y"]
    window._dl_tasks = ["z"]
    window.close()
    assert window._cover_backfill_tasks == []
    assert window._bg_compose_tasks == []
    assert window._dl_tasks == []


# --------------------------------------------------------------------------- #
# F2：共享基类收敛 QRunnable 样板，emit_safe 对已销毁接收者静默
# --------------------------------------------------------------------------- #
def test_app_runnable_emit_safe_isolates_deleted_receiver(_qapp):
    assert issubclass(_AppRunnable, app_module.QRunnable)

    class _RaisingSignal:
        def emit(self, *args):
            raise RuntimeError("receiver deleted")

    _AppRunnable.emit_safe(_RaisingSignal(), 1)  # 不应抛出

    from PySide6.QtCore import QObject, Signal

    class _Sig(QObject):
        value = Signal(int)

    got = []
    obj = _Sig()
    obj.value.connect(got.append)
    _AppRunnable.emit_safe(obj.value, 7)
    assert got == [7]
