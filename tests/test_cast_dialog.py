# -*- coding: utf-8 -*-
"""投屏设备选择对话框测试（test_cast_dialog.py）。

Task 4（DLNA 投屏）：验证 CastDeviceDialog 的纯逻辑与交互行为——
- 设备标签格式化（friendly name + IP）与 IP 提取；
- 后台线程 SSDP 扫描经 Qt signal 回传 list[DlnaRenderer] 并回填列表（去重）；
- 选中 / 确定（信号 + 访问器）/ 双击 / 取消；
- 刷新重新扫描、空列表提示、扫描错误提示。

SSDP 全程 patch framework.dlna.discover_renderers，不发真实组播。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtWidgets import QDialog

from framework.dlna import DlnaRenderer
from gui.components import cast_dialog as cd


def _renderer(
    udn: str = "uuid:tv-1",
    name: str = "客厅电视",
    ip: str = "192.168.1.100",
    location: str | None = None,
) -> DlnaRenderer:
    location = location or f"http://{ip}:49152/description.xml"
    return DlnaRenderer(
        udn=udn,
        friendly_name=name,
        location=location,
        av_transport_control_url=f"{location}/ctl/AVTransport",
        rendering_control_url=f"{location}/ctl/RenderingControl",
        service_type="urn:schemas-upnp-org:service:AVTransport:1",
    )


def _drain(app, cond, timeout: float = 5.0) -> bool:
    """轮询处理 Qt 事件直到 cond() 为真或超时（扫描信号为队列投递）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        app.processEvents()
        time.sleep(0.01)
    return bool(cond())


def _wait_thread_idle(app, worker, timeout: float = 5.0) -> None:
    """等待扫描线程真正退出（确保 patch 作用域内完成协议调用）。"""
    assert worker is not None
    _drain(app, lambda: not worker.isRunning(), timeout)


def _detached_running() -> list:
    return [w for w in cd._DETACHED_WORKERS if w.isRunning()]


@pytest.fixture
def app(_qapp):
    return _qapp


# ---------------------------------------------------------------------- #
# 纯逻辑（无 Qt 交互）
# ---------------------------------------------------------------------- #
def test_format_device_label_includes_name_and_ip():
    """列表项文本 = friendly name + IP。"""
    assert cd.format_device_label(_renderer()) == "客厅电视 (192.168.1.100)"


def test_format_device_label_without_ip_keeps_name():
    """location 无法解析出主机时只显示名称，不带空括号。"""
    r = _renderer(location="not-a-url")
    assert cd.format_device_label(r) == "客厅电视"


def test_device_ip_variants():
    assert cd.device_ip("http://192.168.1.50:8008/desc.xml") == "192.168.1.50"
    assert cd.device_ip("http://192.168.1.50/desc.xml") == "192.168.1.50"
    assert cd.device_ip("") == ""
    assert cd.device_ip("not-a-url") == ""


# ---------------------------------------------------------------------- #
# 对话框基础结构
# ---------------------------------------------------------------------- #
def test_dialog_basics(app):
    """标题、当前集、按钮、空列表提示、错误标签初始状态。"""
    dialog = cd.CastDeviceDialog("第 12 集 · 告白")

    assert dialog.windowTitle() == "选择投屏设备"
    assert "第 12 集 · 告白" in dialog.episode_label.text()
    assert dialog.refresh_btn.text() == "刷新"
    assert dialog.cancel_btn.text() == "取消"
    assert not dialog.select_btn.isEnabled()  # 未选中前不可确定
    # 空列表提示：初始可见，含局域网 / 防火墙关键词
    assert not dialog.empty_hint.isHidden()
    assert "同一局域网" in cd.EMPTY_HINT_TEXT
    assert "Windows 防火墙" in cd.EMPTY_HINT_TEXT
    assert dialog.error_label.isHidden()


def test_selected_device_raises_without_selection(app):
    """未选中设备时访问 selected_device() 抛 RuntimeError。"""
    dialog = cd.CastDeviceDialog("第 1 集")
    with pytest.raises(RuntimeError):
        dialog.selected_device()


# ---------------------------------------------------------------------- #
# 后台扫描 → signal 回传 → 列表回填
# ---------------------------------------------------------------------- #
def test_worker_emits_renderers_via_signal(app):
    """SsdpScanWorker 在后台线程调用 discover_renderers，signal 回传列表。"""
    worker = cd.SsdpScanWorker(timeout=0.1)
    payload = []
    worker.scanFinished.connect(payload.append)

    with patch.object(
        cd.dlna,
        "discover_renderers",
        side_effect=lambda timeout=2.0: [_renderer(), _renderer(udn="uuid:tv-2")],
    ) as fake:
        worker.start()
        _wait_thread_idle(app, worker)
        app.processEvents()  # 队列信号投递

    assert fake.call_count == 1
    fake.assert_called_once_with(0.1)
    assert isinstance(payload[0], list)
    assert payload[0] == [
        _renderer(),
        _renderer(udn="uuid:tv-2"),
    ]


def test_scan_populates_list_dedups_and_runs_offthread(app):
    """扫描回填列表（按 UDN 去重）、更新状态、隐藏空提示、线程不阻塞 UI。"""
    main_ident = threading.get_ident()
    seen_idents = []

    def fake(timeout=2.0):
        seen_idents.append(threading.get_ident())
        return [
            _renderer(udn="uuid:tv-1", name="客厅电视"),
            _renderer(udn="uuid:tv-1", name="客厅电视（重复）"),
            _renderer(udn="uuid:tv-2", name="卧室电视", ip="192.168.1.101"),
        ]

    dialog = cd.CastDeviceDialog("第 3 集")
    with patch.object(cd.dlna, "discover_renderers", side_effect=fake):
        dialog._start_scan()
        assert dialog.refresh_btn.isEnabled() is False  # 扫描中禁用刷新
        assert _drain(app, lambda: dialog.device_list.count() == 2)
        _wait_thread_idle(app, dialog._worker)

    # 协议调用发生在后台线程
    assert seen_idents and seen_idents[0] != main_ident
    # 去重 + friendly name + IP
    assert dialog.device_list.item(0).text() == "客厅电视 (192.168.1.100)"
    assert dialog.device_list.item(1).text() == "卧室电视 (192.168.1.101)"
    # 状态 / 提示 / 刷新按钮
    assert "2" in dialog.status_label.text()
    assert dialog.empty_hint.isHidden()
    assert dialog.refresh_btn.isEnabled()
    assert dialog.error_label.isHidden()


def test_scan_empty_result_shows_hint(app):
    """扫描无设备：状态提示未找到、空列表提示显示、刷新可重试。"""
    dialog = cd.CastDeviceDialog("第 4 集")
    with patch.object(cd.dlna, "discover_renderers", return_value=[]):
        dialog._start_scan()
        assert _drain(app, lambda: "未找到设备" in dialog.status_label.text())
        _wait_thread_idle(app, dialog._worker)

    assert not dialog.empty_hint.isHidden()
    assert dialog.refresh_btn.isEnabled()


def test_scan_error_shows_message_and_enables_refresh(app):
    """扫描抛错：错误标签可见、状态提示出错、刷新可重试。"""
    dialog = cd.CastDeviceDialog("第 5 集")
    with patch.object(
        cd.dlna, "discover_renderers", side_effect=OSError("network down")
    ):
        dialog._start_scan()
        assert _drain(app, lambda: "扫描出错" in dialog.status_label.text())
        _wait_thread_idle(app, dialog._worker)

    assert not dialog.error_label.isHidden()
    assert "network down" in dialog.error_label.text()
    assert dialog.refresh_btn.isEnabled()


# ---------------------------------------------------------------------- #
# 刷新 / 选择 / 取消
# ---------------------------------------------------------------------- #
def test_refresh_clears_list_and_rescans(app):
    """刷新清空列表与去重缓存并重新发起 SSDP 扫描。"""
    dialog = cd.CastDeviceDialog("第 6 集")
    with patch.object(
        cd.dlna, "discover_renderers", side_effect=[[], [_renderer()]]
    ) as fake:
        dialog._start_scan()
        assert _drain(app, lambda: "未找到设备" in dialog.status_label.text())
        _wait_thread_idle(app, dialog._worker)
        assert dialog.device_list.count() == 0

        dialog.refresh_btn.click()
        assert _drain(app, lambda: dialog.device_list.count() == 1)
        _wait_thread_idle(app, dialog._worker)

    assert fake.call_count == 2
    assert dialog.device_list.item(0).text() == "客厅电视 (192.168.1.100)"
    assert "1" in dialog.status_label.text()


def test_select_confirm_returns_renderer_and_emits_signal(app):
    """选中后确定：QDialog.Accepted、selected_device() 访问器、deviceSelected 信号。"""
    dialog = cd.CastDeviceDialog("第 7 集")
    emitted = []
    dialog.deviceSelected.connect(emitted.append)

    with patch.object(cd.dlna, "discover_renderers", return_value=[_renderer()]):
        dialog._start_scan()
        assert _drain(app, lambda: dialog.device_list.count() == 1)
        _wait_thread_idle(app, dialog._worker)

    dialog.device_list.setCurrentRow(0)
    assert dialog.select_btn.isEnabled()
    assert dialog.selected_device().udn == "uuid:tv-1"

    dialog.select_btn.click()
    assert dialog.result() == QDialog.Accepted
    assert dialog.selected_device().udn == "uuid:tv-1"
    assert emitted and emitted[0].udn == "uuid:tv-1"


def test_double_click_confirms_selection(app):
    """双击设备直接确认（跳过确定按钮）。"""
    dialog = cd.CastDeviceDialog("第 8 集")
    with patch.object(cd.dlna, "discover_renderers", return_value=[_renderer()]):
        dialog._start_scan()
        assert _drain(app, lambda: dialog.device_list.count() == 1)
        _wait_thread_idle(app, dialog._worker)

    dialog.device_list.itemDoubleClicked.emit(dialog.device_list.item(0))
    assert dialog.result() == QDialog.Accepted
    assert dialog.selected_device().udn == "uuid:tv-1"


def test_cancel_rejects_and_detaches_running_worker(app):
    """取消关闭对话框（Rejected）；扫描中取消则 worker 脱离生命周期后台收尾。"""
    dialog = cd.CastDeviceDialog("第 9 集")
    with patch.object(
        cd.dlna,
        "discover_renderers",
        side_effect=lambda timeout=2.0: time.sleep(0.2) or [_renderer()],
    ):
        dialog._start_scan()
        worker = dialog._worker
        assert worker is not None and worker.isRunning()

        dialog.cancel_btn.click()
        assert dialog.result() == QDialog.Rejected
        assert dialog._worker is None  # 已从对话框脱离
        assert worker in cd._DETACHED_WORKERS
        # patch 作用域内等待线程结束，避免真实 SSDP 泄漏到后台
        _drain(app, lambda: not worker.isRunning())
        app.processEvents()

    assert not _detached_running()
    assert worker not in cd._DETACHED_WORKERS  # 结束后自动回收


def test_show_starts_scan_automatically(app):
    """show() 触发 showEvent 自动扫描；关闭后 worker 清理。"""
    dialog = cd.CastDeviceDialog("第 10 集")
    with patch.object(cd.dlna, "discover_renderers", return_value=[_renderer()]):
        dialog.show()
        assert _drain(app, lambda: dialog.device_list.count() == 1)
        _wait_thread_idle(app, dialog._worker)
        dialog.close()
        app.processEvents()

    assert dialog._worker is None
    assert not _detached_running()
