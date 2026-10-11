# -*- coding: utf-8 -*-
"""分阶段启动修复的针对性测试（占位服务 / 首次导航 / 事件线程 / 原子发布 / 退出编排）。"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QWidget

from framework.events import Event, EventBus

from gui import app as app_module
from gui.app import (
    MainWindow,
    TABS,
    _BootstrapSourceManager,
    _EventBusQtBridge,
    _GuiEventBus,
)


class _EmptySourceManager:
    def all(self):
        return []

    def set_event_bus(self, bus):
        self.event_bus = bus

    def set_adult_visible(self, visible):
        self.adult = visible


class _StubRepository:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1

    def prune_video_history(self):
        pass


class _StubHttp:
    def close(self):
        pass


class _FakeReader:
    def __init__(self):
        self.online = []
        self.epubs = []
        self.flushed = 0

    def open(self, *args):
        self.online.append(args)

    def open_epub(self, path):
        self.epubs.append(path)

    def flush_progress(self):
        self.flushed += 1


def _fake_services(repository=None, source_manager=None):
    return SimpleNamespace(
        source_manager=source_manager or _EmptySourceManager(),
        cookie_manager=None,
        shelf_cache=None,
        search_cache=None,
        shelf_cache_repository=repository or _StubRepository(),
        http=_StubHttp(),
        parser=None,
        checker=None,
        discovery=None,
        decrypter=None,
        content=None,
        bulk_fetch=SimpleNamespace(),
        provider_memory=None,
        search=None,
        download_queue=SimpleNamespace(),
    )


def _make_window(monkeypatch, tmp_path):
    monkeypatch.setattr("gui.app._app_base_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(MainWindow, "_schedule_source_selection", lambda self: None)
    return MainWindow()


# --------------------------------------------------------------------------- #
# 占位 SourceManager：就绪前只读空结果，不抛异常
# --------------------------------------------------------------------------- #
def test_bootstrap_source_manager_is_read_only_and_defensive():
    sm = _BootstrapSourceManager()
    assert sm.all() == []
    assert sm.list_broken() == []
    assert sm.discoverable_sources() == []
    assert sm.enabled_sources() == []
    assert sm.get("x") is None
    assert sm.get_health("x") is not None
    # 写操作空实现，不应抛异常
    sm.set_event_bus(object())
    sm.set_adult_visible(True)
    sm.apply_enabled_selection(["a"])


# --------------------------------------------------------------------------- #
# 项 2：READY 前导航排队（记录通道 + 原始参数快照）
# --------------------------------------------------------------------------- #
def test_pre_ready_navigation_queues_each_entry_point(_qapp, monkeypatch, tmp_path):
    window = _make_window(monkeypatch, tmp_path)
    window._runtime_state = "LOADING"
    window.reader = None

    detail = SimpleNamespace(
        source_id="s1", url="http://a/1", content_type="novel",
        chapters=[SimpleNamespace(url="http://a/1/1")],
    )
    window._open_reader(detail)
    assert window._pending_navigation[0] == "reader"

    window._open_from_search("s2", "http://b/2", "comic")
    assert window._pending_navigation[0] == "reader"

    window._open_epub(str(tmp_path / "book.epub"))
    assert window._pending_navigation[0] == "reader"

    window._open_online_from_shelf(("s3", "http://c/3", ""))
    assert window._pending_navigation[0] == "reader"

    window._open_download_dialog(detail)
    assert window._pending_navigation[0] == "download"
    window.close()


def test_pending_reader_navigation_uses_original_param_snapshot(
    _qapp, monkeypatch, tmp_path
):
    window = _make_window(monkeypatch, tmp_path)
    window._runtime_state = "LOADING"
    window.reader = None

    detail = SimpleNamespace(
        source_id="s1", url="http://a/1", content_type="novel",
        chapters=[SimpleNamespace(url="http://a/1/1")],
    )
    window._open_reader(detail)
    detail.url = "MUTATED"  # 原始参数应已被快照，不受后续修改影响

    fake = _FakeReader()
    window.reader = fake
    window._replace_pending_navigation()

    assert fake.online == [("s1", "http://a/1", "novel", "http://a/1/1")]
    assert window._pending_navigation is None

    # epub 路径在打开时 resolve()，与书架扫描生成的续读 key 一致
    epub = str(tmp_path / "book.epub")
    window._open_epub(epub)
    assert fake.epubs == [str(Path(epub).resolve())]
    window.close()


# --------------------------------------------------------------------------- #
# 项 1：READY 后对已构建页面 rebind（非重建）
# --------------------------------------------------------------------------- #
class _FakeHome(QWidget):
    def set_runtime_services(self, source_manager, bulk_fetch):
        self.bound = (source_manager, bulk_fetch)

    def set_runtime_status(self, text):
        self.status = text


class _FakePage(QWidget):
    def __init__(self):
        super().__init__()
        self.calls = []

    def rebind_runtime(self, *args):
        self.calls.append(args)


def _stub_pages(monkeypatch):
    def build_home(self):
        page = _FakeHome()
        self.home_page = page
        return page

    def make(name):
        def build(self):
            page = _FakePage()
            setattr(self, f"{name}_page", page)
            return page

        return build

    monkeypatch.setattr(MainWindow, "_build_home", build_home)
    for name in ("discover", "search", "source", "library"):
        monkeypatch.setattr(MainWindow, f"_build_{name}", make(name))


def test_ready_rebinds_already_built_pages(_qapp, monkeypatch, tmp_path):
    _stub_pages(monkeypatch)
    window = _make_window(monkeypatch, tmp_path)

    for key in ("discover", "search", "source", "library"):
        window.tabs.setCurrentIndex(window._tab_index[key])

    window._runtime_state = "LOADING"
    window._runtime_generation = 1
    services = _fake_services()
    window._on_runtime_ready(services, 1)

    assert window._runtime_state == "READY"
    assert window.discover_page.calls == [
        (services.source_manager, services.discovery, services.content, services.bulk_fetch)
    ]
    assert window.search_page.calls == [
        (services.source_manager, services.search, services.content)
    ]
    assert window.source_page.calls == [
        (services.source_manager, services.checker, services.cookie_manager)
    ]
    assert window.library_page.calls == [
        (services.source_manager, services.content, services.shelf_cache_repository)
    ]
    assert window.source_manager is services.source_manager
    assert window.home_page.bound == (services.source_manager, services.bulk_fetch)
    window.close()


# --------------------------------------------------------------------------- #
# 项 4：发布失败回滚为 FAILED，回收私有连接，不残留半套服务
# --------------------------------------------------------------------------- #
def test_ready_publish_failure_rolls_back_and_fails(_qapp, monkeypatch, tmp_path):
    window = _make_window(monkeypatch, tmp_path)
    window._runtime_state = "LOADING"
    window._runtime_generation = 1
    repo = _StubRepository()
    services = _fake_services(repository=repo)
    bootstrap = window.source_manager

    def boom(self, svc):
        raise RuntimeError("rebind failed")

    monkeypatch.setattr(MainWindow, "_rebind_pages", boom)
    window._on_runtime_ready(services, 1)

    assert window._runtime_state == "FAILED"
    assert repo.closed == 1
    assert window.source_manager is bootstrap  # 未发布半套服务
    assert not window.home_page.runtime_status.isHidden()
    window.close()


def test_publish_failure_restores_every_published_reference(
    _qapp, monkeypatch, tmp_path
):
    """发布失败必须整体回滚：所有已 setattr 的服务引用恢复为回滚前的旧值。"""
    window = _make_window(monkeypatch, tmp_path)
    window._runtime_state = "LOADING"
    window._runtime_generation = 1
    repo = _StubRepository()
    services = _fake_services(repository=repo)
    fields = [
        "source_manager", "cookie_manager", "shelf_cache", "search_cache",
        "shelf_cache_repository", "http", "parser", "checker", "discovery",
        "decrypter", "content", "bulk_fetch", "ikanpp_provider_memory",
        "search", "download_queue",
    ]
    before = {name: getattr(window, name) for name in fields}

    def boom(self, svc):
        raise RuntimeError("rebind failed")

    monkeypatch.setattr(MainWindow, "_rebind_pages", boom)
    window._on_runtime_ready(services, 1)

    assert window._runtime_state == "FAILED"
    for name in fields:
        assert getattr(window, name) is before[name], name
    assert repo.closed == 1
    window.close()


def test_failed_state_can_be_retried(_qapp, monkeypatch, tmp_path):
    """回滚后 FAILED 保持既有重试语义：再次初始化进入 LOADING 并启动任务。"""
    window = _make_window(monkeypatch, tmp_path)
    window._runtime_state = "FAILED"
    started = []

    class _Task:
        def __init__(self, *args):
            pass

        def start(self):
            started.append(True)

    monkeypatch.setattr("gui.app._StartupTask", _Task)
    window._start_runtime_initialization()

    assert window._runtime_state == "LOADING"
    assert started == [True]
    window.close()


def test_late_ready_after_close_is_dropped(_qapp, monkeypatch, tmp_path):
    window = _make_window(monkeypatch, tmp_path)
    repo = _StubRepository()
    services = _fake_services(repository=repo)

    window.close()
    assert window._runtime_state == "CLOSING"

    window._on_runtime_ready(services, window._runtime_generation)
    assert window._runtime_state == "CLOSING"  # 迟到 ready 不覆盖状态
    assert repo.closed == 1  # 迟到服务被回收
    assert getattr(window, "shelf_cache_repository", None) is None


# --------------------------------------------------------------------------- #
# 项 3：事件总线经 Qt 桥接回主线程，且异常隔离
# --------------------------------------------------------------------------- #
def test_gui_event_bus_delivers_on_gui_thread_and_isolates_errors(_qapp):
    bus = EventBus()
    bridge = _EventBusQtBridge()
    gui_bus = _GuiEventBus(bus, bridge)

    gui_thread = threading.get_ident()
    seen = []
    boom = []

    gui_bus.subscribe(lambda e: seen.append(threading.get_ident()))

    def bad(event):
        boom.append(1)
        raise RuntimeError("boom")

    gui_bus.subscribe(bad)

    worker = threading.Thread(target=lambda: bus.emit(Event("X")))
    worker.start()
    worker.join()

    for _ in range(200):
        _qapp.processEvents()
        if seen and boom:
            break

    assert seen and all(tid == gui_thread for tid in seen)
    assert boom == [1]  # 异常订阅者不影响其它订阅者

    gui_bus.close()
    bridge.close()
    bus.emit(Event("Y"))
    _qapp.processEvents()
    assert len(seen) == 1  # close 后不再投递


# --------------------------------------------------------------------------- #
# 项 6：退出编排 —— 关闭已发布连接并落盘阅读进度
# --------------------------------------------------------------------------- #
def test_close_flushes_reader_and_closes_repository(_qapp, monkeypatch, tmp_path):
    window = _make_window(monkeypatch, tmp_path)
    closed = []
    window.shelf_cache_repository = SimpleNamespace(close=lambda: closed.append("repo"))
    window.reader = _FakeReader()

    window.close()

    assert closed == ["repo"]
    assert window.reader.flushed == 1
    assert window._runtime_state == "CLOSING"
    assert window.shelf_cache_repository is None


# --------------------------------------------------------------------------- #
# 项 5：启动不做全源自检（保留用户显式入口）
# --------------------------------------------------------------------------- #
def test_startup_has_no_automatic_source_diagnostic(_qapp, monkeypatch, tmp_path):
    assert not hasattr(MainWindow, "_schedule_startup_diag")
    assert not hasattr(MainWindow, "_on_startup_diag_done")

    (tmp_path / "data").mkdir()
    health = tmp_path / "data" / "health.json"
    health.write_text('{"sentinel": 1}', encoding="utf-8")

    started = []

    class _Task:
        def __init__(self, *args):
            pass

        def start(self):
            started.append(True)

    monkeypatch.setattr("gui.app._app_base_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(MainWindow, "_schedule_source_selection", lambda self: None)
    monkeypatch.setattr("gui.app._StartupTask", _Task)

    window = MainWindow()
    window.show()
    _qapp.processEvents()

    assert health.read_text(encoding="utf-8") == '{"sentinel": 1}'
    window.close()
