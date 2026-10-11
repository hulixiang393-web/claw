# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.app import MainWindow, _SettingsSnapshot


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


def test_home_bootstrap_history_is_ready_without_runtime_startup(
    _qapp, monkeypatch, tmp_path
):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "search_history.json").write_text(
        json.dumps([{"keyword": "claw", "timestamp": time.time()}]), encoding="utf-8"
    )
    (tmp_path / "data" / "reading_progress.json").write_text(
        json.dumps({
            "book": {
                "book_url": "book",
                "content_type": "video",
                "chapter_url": "chapter",
                "chapter_title": "第1集",
                "title": "作品",
                "history_timestamp": time.time(),
                "updated_at": "2026-10-10T00:00:00",
                "location": {"seconds": 12, "duration": 60},
            }
        }), encoding="utf-8"
    )
    monkeypatch.setattr("gui.app._app_base_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(MainWindow, "_schedule_source_selection", lambda self: None)
    started = []

    class _Task:
        def __init__(self, *args):
            pass

        def start(self):
            started.append(True)

    monkeypatch.setattr("gui.app._StartupTask", _Task)
    start = time.perf_counter()
    window = MainWindow()
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0
    assert window.home_page.playback_list.count() == 1
    assert any(label.text() == "  claw" for label in window.home_page.findChildren(type(window.home_page.runtime_status)))
    assert window.reading_progress is not None
    window.show()
    _qapp.processEvents()
    assert started == [True]
    window.close()


def test_runtime_failure_preserves_bootstrap_history_and_close_drops_late_result(
    _qapp, monkeypatch, tmp_path
):
    monkeypatch.setattr("gui.app._app_base_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(MainWindow, "_schedule_source_selection", lambda self: None)
    window = MainWindow()
    window._runtime_state = "LOADING"
    window._runtime_generation = 4
    window._on_runtime_failed("failure", 4)

    assert window._runtime_state == "FAILED"
    assert window.home_page.playback_list.count() == 0
    assert not window.home_page.runtime_status.isHidden()
    window.close()
    window._on_runtime_failed("late", 4)
    assert window._runtime_state == "CLOSING"


def test_settings_snapshot_supports_section_and_key_access():
    snapshot = _SettingsSnapshot({"network": {"default_timeout": 7}})
    assert snapshot.get("network") == {"default_timeout": 7}
    assert snapshot.get("network", "default_timeout", 10) == 7
    assert snapshot.get("network", "missing", 10) == 10


def _policy_manager(runtime_settings):
    from framework.config import SourceConfig
    from framework.source_manager import SourceManager

    manager = SourceManager(runtime_settings=runtime_settings)
    manager.add(SourceConfig.from_dict({
        "$schema_version": 2, "$id": "sa", "$name": "源A", "$type": "novel",
        "transports": {"base_url": "https://example.test"},
        "endpoints": {"search": {"base_url": "/search", "method": "GET"}},
    }))
    return manager


def _policy_steps(manager):
    steps = [manager.get("sa").enabled]
    manager.update_health("sa", "broken", "boom1")
    steps.append(manager.get("sa").enabled)
    manager.update_health("sa", "broken", "boom2")
    steps.append(manager.get("sa").enabled)
    manager.update_health("sa", "ok")
    steps.append(manager.get("sa").enabled)
    return steps


def test_snapshot_policy_contract_matches_real_settings_manager(tmp_path):
    from framework.settings_manager import SettingsManager

    settings = SettingsManager(tmp_path / "settings.json")
    settings.set("sources_runtime", "auto_disable_after_failures", 2)
    via_snapshot = _policy_steps(_policy_manager(_SettingsSnapshot(settings.as_dict())))
    via_manager = _policy_steps(_policy_manager(settings))
    assert via_snapshot == via_manager == [True, True, False, True]
    assert _SettingsSnapshot(settings.as_dict()).get("sources_runtime") == settings.get_section("sources_runtime")


def test_mainwindow_bootstrap_construction_is_read_only(_qapp, monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    now = time.time()
    search = data / "search_history.json"
    search.write_text(json.dumps([{"keyword": "old", "timestamp": now - 604801}]), encoding="utf-8")
    progress = data / "reading_progress.json"
    progress.write_text(json.dumps({
        "book": {
            "book_url": "book", "content_type": "video", "chapter_url": "chapter",
            "chapter_title": "第1集", "title": "作品", "history_timestamp": now,
            "updated_at": "2026-10-10T00:00:00", "location": {"seconds": 12, "duration": 60},
        }
    }), encoding="utf-8")
    search_before, progress_before = search.read_bytes(), progress.read_bytes()
    monkeypatch.setattr("gui.app._app_base_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(MainWindow, "_schedule_source_selection", lambda self: None)

    class _Task:
        def __init__(self, *args):
            pass

        def start(self):
            pass

    monkeypatch.setattr("gui.app._StartupTask", _Task)
    window = MainWindow()

    # 首帧构造只读：历史/进度 JSON 字节不变、不落临时文件、不创建 data 子目录
    assert search.read_bytes() == search_before
    assert progress.read_bytes() == progress_before
    assert list(tmp_path.rglob("*.tmp")) == []
    assert not (tmp_path / "data" / "cache").exists()
    assert not (tmp_path / "data" / "logs").exists()
    # 首页仍即时显示内存中的最近播放
    assert window.home_page.playback_list.count() == 1
    window.close()


def test_deferred_history_cleanup_rewrites_only_from_background_thread(tmp_path):
    import threading
    from types import SimpleNamespace

    from gui.app import _run_deferred_history_cleanup
    from framework.reading_progress import ReadingProgress
    from framework.search_history import SearchHistory

    search = SearchHistory(tmp_path / "search.json", defer_cleanup=True)
    search.push("word")
    progress = ReadingProgress(tmp_path / "progress.json", defer_cleanup=True)
    progress.save("demo", "book", "video", "ep", "chapter", title="title",
                  location={"seconds": 42, "duration": 100})
    thread_seen = []
    original = search.run_deferred_cleanup

    def record_thread():
        thread_seen.append(threading.current_thread().name)
        original()

    search.run_deferred_cleanup = record_thread
    window = SimpleNamespace(search_history=search, reading_progress=progress)
    thread = threading.Thread(
        target=_run_deferred_history_cleanup, args=(window,), name="runtime-startup"
    )
    thread.start()
    thread.join()

    assert thread_seen == ["runtime-startup"]
    assert [item["keyword"] for item in json.loads(search.path.read_text())] == ["word"]
    assert not progress._defer_cleanup
    assert progress.resume("book")["location"]["seconds"] == 42


def test_runtime_navigation_is_queued_until_ready(_qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("gui.app._app_base_dir", lambda: Path(tmp_path))
    window = MainWindow()
    window._runtime_state = "LOADING"
    window._open_reader(type("Detail", (), {
        "source_id": "s", "url": "u", "content_type": "novel", "chapters": []
    })())
    assert window._pending_navigation[0] == "reader"
    assert window.tabs.currentIndex() == window._tab_index["reader"]
    window.close()
