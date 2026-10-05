"""投屏设备选择对话框（cast_dialog.py）。

DLNA 投屏第一步：SSDP 扫描局域网 MediaRenderer，列表展示
friendly name + IP，用户选中后经 selected_device() 访问器或
deviceSelected 信号把 DlnaRenderer 交回调用方。

- 扫描在后台 QThread 执行，UI 不阻塞；结果经 Qt signal 队列投递回主线程；
- 「搜索设备」与「刷新」清空/更新扫描结果并启动 SSDP 搜索；
- 设备列表为空时提示检查同一局域网 / Windows 防火墙；
- 关闭对话框时若扫描仍在进行，worker 脱离生命周期后台收尾，
  不阻塞 UI、不悬挂线程。

历史设备（构造参数 history，来自 framework.cast_history.CastHistory）：
- 分组置顶、首条标「上次使用」并在打开时默认选中，直接点确定即可投屏；
- 后台 SSDP 扫描照常进行但不阻塞选择，扫描结果与历史按 UDN 合并去重
  （历史保名称、新扫描更新 IP / 控制 URL）；
- 「清除历史」经 clearHistoryRequested 信号通知调用方删存储，本组件
  不直接碰文件。

样式跟随 palette()，不写死色值，兼容主题切换。
"""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlparse

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from framework import dlna
from framework.cast_history import renderer_from_entry
from framework.dlna import DlnaRenderer

# 空列表提示（spec 6.2：同一局域网 / 防火墙）
EMPTY_HINT_TEXT = (
    "请确认电脑和电视处于同一局域网，并允许应用通过 Windows 防火墙。\n"
    "如网络配置无误，请检查电视已开机且 DLNA 功能已启用，"
    "然后点击搜索设备重试。"
)

# 已脱离对话框、仍在后台收尾的扫描线程（防止被 GC 后 QThread 悬挂）
_DETACHED_WORKERS: set["SsdpScanWorker"] = set()


def device_ip(location: str) -> str:
    """从设备 description URL 提取 IP / 主机名（失败返回空串）。"""
    try:
        return urlparse(location).hostname or ""
    except (ValueError, TypeError):
        return ""


def format_device_label(renderer: DlnaRenderer) -> str:
    """列表项文本：friendly name + IP（解析不出 IP 时只显示名称）。"""
    ip = device_ip(renderer.location)
    return f"{renderer.friendly_name} ({ip})" if ip else renderer.friendly_name


class SsdpScanWorker(QThread):
    """后台 SSDP 扫描任务：完成经 scanFinished 回传 list[DlnaRenderer]。"""

    scanFinished = Signal(object)  # list[DlnaRenderer]
    scanError = Signal(str)  # 错误信息

    def __init__(self, timeout: float = 2.0, parent=None):
        super().__init__(parent)
        self.timeout = timeout

    def run(self) -> None:
        """线程入口：调用协议层 discover_renderers，异常统一转 scanError。"""
        try:
            renderers = dlna.discover_renderers(self.timeout)
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一降级为错误提示
            self.scanError.emit(str(exc) or exc.__class__.__name__)
            return
        self.scanFinished.emit(list(renderers))


class CastDeviceDialog(QDialog):
    """选择投屏设备：扫描 → 列表选择 → 确认返回 DlnaRenderer。"""

    deviceSelected = Signal(object)  # DlnaRenderer，确认选中时发出
    clearHistoryRequested = Signal()  # 用户点「清除历史」（调用方负责删存储）

    SSDP_TIMEOUT = 2.0  # SSDP 扫描超时（秒）

    def __init__(self, current_episode_title: str, parent=None,
                 history: list[dict] | None = None):
        super().__init__(parent)
        self.setWindowTitle("选择投屏设备")
        self.setMinimumWidth(440)

        self._devices: dict[str, DlnaRenderer] = {}  # UDN → 设备（去重）
        self._selected: DlnaRenderer | None = None
        self._worker: SsdpScanWorker | None = None
        # 历史设备（最近在前）：置顶展示 + 首条「上次使用」默认选中
        self._history_renderers: list[DlnaRenderer] = []
        self._history_udns: set[str] = set()
        self._last_used_udn = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        header = QLabel("选择投屏设备")
        header.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(header)

        # 当前集标题（上下文感知）
        self.episode_label = QLabel(f"当前集：{current_episode_title}")
        self.episode_label.setWordWrap(True)
        self.episode_label.setStyleSheet(
            "color: palette(text2); font-size: 12px;"
        )
        layout.addWidget(self.episode_label)

        # 状态行：扫描状态 + 进度条 + 刷新
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: palette(mid); font-size: 12px;")
        status_row.addWidget(self.status_label, stretch=1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(90)
        self.progress.setVisible(False)
        status_row.addWidget(self.progress)
        self.search_btn = QPushButton("搜索设备")
        self.search_btn.clicked.connect(self._on_search_clicked)
        status_row.addWidget(self.search_btn)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        status_row.addWidget(self.refresh_btn)
        self.clear_history_btn = QPushButton("清除历史")
        self.clear_history_btn.clicked.connect(self._on_clear_history_clicked)
        self.clear_history_btn.setVisible(False)  # 无历史时隐藏
        status_row.addWidget(self.clear_history_btn)
        layout.addLayout(status_row)

        # 设备列表（friendly name + IP）
        self.device_list = QListWidget()
        self.device_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.device_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.device_list, stretch=1)

        # 空列表提示
        self.empty_hint = QLabel(EMPTY_HINT_TEXT)
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setStyleSheet("color: palette(mid); font-size: 12px;")
        layout.addWidget(self.empty_hint)

        # 扫描错误提示
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #F44336; font-size: 12px;")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        self.select_btn = buttons.button(QDialogButtonBox.Ok)
        self.select_btn.setText("确定")
        self.select_btn.setEnabled(False)
        self.cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        self.cancel_btn.setText("取消")
        self.select_btn.clicked.connect(self._on_accept)
        self.cancel_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

        # 历史设备置顶 + 上次使用默认选中（放最后：依赖上面全部控件就位）
        self._load_history(history)

    # ------------------------------------------------------------------ #
    # 历史设备
    # ------------------------------------------------------------------ #
    def _load_history(self, history: list[dict] | None) -> None:
        """历史条目 → 列表置顶分组，首条标「上次使用」并默认选中。

        打开对话框即可直接点确定投上次设备；SSDP 扫描由用户主动触发，
        不阻塞选择。非法条目（非 dict / 字段不全）静默跳过。
        """
        for entry in history or []:
            renderer = renderer_from_entry(entry)
            if renderer is None or renderer.udn in self._history_udns:
                continue
            self._history_udns.add(renderer.udn)
            self._history_renderers.append(renderer)
        if not self._history_renderers:
            return
        self._last_used_udn = self._history_renderers[0].udn
        for renderer in self._history_renderers:
            self._append_item(renderer)
        self.empty_hint.setVisible(False)
        self.clear_history_btn.setVisible(True)
        # 打开即选中上次使用：select_btn 随 selectionChanged 同步启用
        self.device_list.setCurrentRow(0)

    def _item_label(self, renderer: DlnaRenderer) -> str:
        """列表项文本：名称 + IP；上次使用的设备追加「上次使用」标记。"""
        label = format_device_label(renderer)
        return f"{label} · 上次使用" if renderer.udn == self._last_used_udn else label

    def _append_item(self, renderer: DlnaRenderer) -> QListWidgetItem:
        """把设备加入列表与去重缓存，返回列表项。"""
        self._devices[renderer.udn] = renderer
        item = QListWidgetItem(self._item_label(renderer))
        item.setData(Qt.UserRole, renderer.udn)
        self.device_list.addItem(item)
        return item

    def _merge_scanned(self, existing: DlnaRenderer,
                       scanned: DlnaRenderer) -> None:
        """扫描到列表已有设备：历史保名称、新扫描更新 IP / 控制 URL。

        非历史设备的重复应答维持原去重语义（保留首次结果）。
        """
        if existing.udn not in self._history_udns:
            return
        merged = replace(scanned, friendly_name=existing.friendly_name)
        self._devices[merged.udn] = merged
        if self._selected is not None and self._selected.udn == merged.udn:
            self._selected = merged  # 已选中的历史项改用新扫描的连接地址
        for row in range(self.device_list.count()):
            item = self.device_list.item(row)
            if item.data(Qt.UserRole) == merged.udn:
                item.setText(self._item_label(merged))
                break

    def _on_clear_history_clicked(self) -> None:
        """「清除历史」：通知调用方清存储，本地历史分组随之移除。

        对话框不直接碰 JSON 文件（存储归调用方 CastHistory 管）；扫描到的
        非历史设备保留，用户仍可正常选设备投屏。
        """
        self.clearHistoryRequested.emit()
        history_udns = set(self._history_udns)
        selected_udn = self._selected.udn if self._selected is not None else None
        self._history_udns.clear()
        self._history_renderers.clear()
        self._last_used_udn = ""
        self.clear_history_btn.setVisible(False)
        for row in range(self.device_list.count() - 1, -1, -1):
            if self.device_list.item(row).data(Qt.UserRole) in history_udns:
                self.device_list.takeItem(row)
        for udn in history_udns:
            self._devices.pop(udn, None)
        if selected_udn in history_udns:
            # 选中项刚被移除：明确清掉选中（Qt 会把 current 挪到下一项，
            # 不显式复位就会让「确定」静默投给另一台电视）
            self.device_list.setCurrentRow(-1)
            self._selected = None
            self.select_btn.setEnabled(False)
        if self._worker is None or not self._worker.isRunning():
            self._finish_scan_ui()  # 状态行设备数按移除后的实际数量刷新
        else:
            # 扫描仍在进行：_finish_scan_ui 被跳过，空提示要按移除后的实际
            # 数量即时回显（状态行此刻是「正在扫描…」，无陈旧计数可修）。
            self.empty_hint.setVisible(self.device_list.count() == 0)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def showEvent(self, event) -> None:
        """显示对话框，不自动开始 SSDP 扫描。"""
        super().showEvent(event)

    def done(self, result: int) -> None:
        """确认 / 取消 / 关闭均先回收扫描线程。"""
        self._shutdown_worker()
        super().done(result)

    # ------------------------------------------------------------------ #
    # 扫描
    # ------------------------------------------------------------------ #
    def _start_scan(self) -> None:
        """后台线程发起 SSDP 扫描（扫描进行中不重复启动）。"""
        if self._worker is not None and self._worker.isRunning():
            return
        self._shutdown_worker()

        self.error_label.setVisible(False)
        self.search_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.progress.setRange(0, 0)  # 忙碌指示
        self.progress.setVisible(True)
        self.status_label.setText("正在扫描…")
        self.empty_hint.setVisible(False)

        worker = SsdpScanWorker(timeout=self.SSDP_TIMEOUT)
        worker.scanFinished.connect(self._on_scan_finished)
        worker.scanError.connect(self._on_scan_error)
        self._worker = worker
        worker.start()

    def _shutdown_worker(self) -> None:
        """断开信号并移交 / 回收扫描线程，绝不阻塞主线程。"""
        worker, self._worker = self._worker, None
        if worker is None:
            return
        for signal, slot in (
            (worker.scanFinished, self._on_scan_finished),
            (worker.scanError, self._on_scan_error),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        if worker.isRunning():
            # SSDP 阻塞在 socket，不等待：脱离对话框后台收尾
            _DETACHED_WORKERS.add(worker)
            worker.finished.connect(
                lambda w=worker: _DETACHED_WORKERS.discard(w)
            )

    def _on_scan_finished(self, renderers) -> None:
        """扫描完成（主线程槽）：与历史合并去重回填列表并更新状态。"""
        for renderer in renderers:
            existing = self._devices.get(renderer.udn)
            if existing is not None:
                self._merge_scanned(existing, renderer)
                continue
            self._append_item(renderer)
        self._finish_scan_ui()

    def _on_scan_error(self, message: str) -> None:
        """扫描失败（主线程槽）：显示错误并允许重试。"""
        self.error_label.setText(f"扫描失败：{message}")
        self.error_label.setVisible(True)
        self.status_label.setText("扫描出错，请重试")
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.search_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.empty_hint.setVisible(self.device_list.count() == 0)

    def _finish_scan_ui(self) -> None:
        count = len(self._devices)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.progress.setVisible(False)
        if count:
            self.status_label.setText(f"扫描完成，找到 {count} 台设备")
        else:
            self.status_label.setText("未找到设备（请检查网络）")
        self.search_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.empty_hint.setVisible(count == 0)

    def _on_search_clicked(self) -> None:
        """点击「搜索设备」：主动发起 SSDP 扫描。"""
        self._start_scan()

    def _on_refresh_clicked(self) -> None:
        """刷新：清空扫描结果与去重缓存后重新扫描（历史分组原样置顶保留）。"""
        self._devices.clear()
        self._selected = None
        self.device_list.clear()
        self.select_btn.setEnabled(False)
        for renderer in self._history_renderers:
            self._append_item(renderer)
        if self._history_renderers:
            # 与打开对话框时一致：恢复「上次使用」默认选中，刷新后不必
            # 再多点一次（选中经 itemSelectionChanged 同步启用确定按钮）
            self.device_list.setCurrentRow(0)
        self._start_scan()

    # ------------------------------------------------------------------ #
    # 选择与确认
    # ------------------------------------------------------------------ #
    def _on_selection_changed(self) -> None:
        self._selected = self._renderer_for_item(self.device_list.currentItem())
        self.select_btn.setEnabled(self._selected is not None)

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        renderer = self._renderer_for_item(item)
        if renderer is None:
            return
        self._selected = renderer
        self.deviceSelected.emit(renderer)
        self.accept()

    def _on_accept(self) -> None:
        if self._selected is None:
            return
        self.deviceSelected.emit(self._selected)
        self.accept()

    def _renderer_for_item(
        self, item: QListWidgetItem | None
    ) -> DlnaRenderer | None:
        if item is None:
            return None
        udn = item.data(Qt.UserRole)
        return self._devices.get(udn) if isinstance(udn, str) else None

    def selected_device(self) -> DlnaRenderer:
        """返回用户选中的设备；未选中时抛 RuntimeError。"""
        if self._selected is None:
            raise RuntimeError("未选中投屏设备")
        return self._selected
