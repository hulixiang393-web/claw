"""下载管理页（DownloadPage）。

对应 ui-download.md 全部 10 个功能点：
1. 任务队列列表（书名/进度/状态/速度）
2. 续传（skip_existing，下载器侧已实现）
3. 并发可调（顶栏 [-][N][+]）
4. 单任务操作（暂停/继续/取消/重试）
5. 进度条 + 章节数 + 时间估算
6. 队列操作（全暂停/全继续/全取消）
7. 已完成区（打开文件夹/删除）
8. 失败区（原因 + 重试，自动重试 3 次后标红）
9. 排序/筛选（状态 + 内容类型）
10. 下载完成通知（系统托盘，可开关）

事件从下载 worker 子线程经 EventBus 广播，这里订阅后用 QTimer.singleShot
回主线程刷新 UI（线程安全）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)
from framework.events import (
    EVENT_DOWNLOAD_COMPLETED,
    EVENT_DOWNLOAD_FAILED,
    EVENT_DOWNLOAD_PROGRESS,
    EVENT_DOWNLOAD_STARTED,
)
from framework.download_queue import TaskStatus

from gui.pages.base_page import BasePage

STATUS_OPTIONS = [("全部状态", None), ("下载中", "active"), ("已完成", "done"), ("失败", "failed")]
TYPE_OPTIONS = [("全部类型", None), ("小说", "novel"), ("漫画", "comic"), ("视频", "video")]


class _TaskCard(QFrame):
    """单个下载任务的卡片。"""

    def __init__(self, task, page: "DownloadPage", parent=None):
        super().__init__(parent)
        self.task = task
        self._page = page
        self._build_ui()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        t = self.task

        # 行1：书名 + 状态 + 类型标签
        row1 = QHBoxLayout()
        title = QLabel(t.title or "无标题")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        title.setWordWrap(True)
        row1.addWidget(title, stretch=1)

        type_lbl = QLabel(t.content_type)
        type_lbl.setStyleSheet(
            "font-size: 10px; padding: 2px 6px; border-radius: 4px;"
            "background: palette(midlight);"
        )
        row1.addWidget(type_lbl)
        layout.addLayout(row1)

        # 行2：进度文本 + 速度 + 时间估算
        row2 = QHBoxLayout()
        self.progress_text = QLabel(self._make_progress_text())
        self.progress_text.setStyleSheet("font-size: 12px; color: palette(dark);")
        row2.addWidget(self.progress_text, stretch=1)
        self.speed_label = QLabel("")
        self.speed_label.setStyleSheet("font-size: 11px; color: palette(mid);")
        row2.addWidget(self.speed_label)
        self.time_label = QLabel("")
        self.time_label.setStyleSheet("font-size: 11px; color: palette(mid);")
        row2.addWidget(self.time_label)
        layout.addLayout(row2)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, max(1, t.total))
        self.progress_bar.setValue(t.done)
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)

        # 行2.5：并发下载中的章节标题（如有多章并行）
        self.active_label = QLabel("")
        self.active_label.setStyleSheet("font-size: 11px; color: palette(accent);")
        self.active_label.setWordWrap(True)
        self.active_label.setVisible(False)
        layout.addWidget(self.active_label)

        # 行3：失败信息（如有）
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("font-size: 11px; color: #D32F2F;")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(bool(t.failed))
        if t.failed:
            self.error_label.setText(f"失败: {', '.join(t.failed[:3])}")
        layout.addWidget(self.error_label)

        # 行4：操作按钮（按状态显示不同按钮）
        self.btn_row = QHBoxLayout()
        self.btn_row.setSpacing(6)
        self._build_buttons()
        layout.addLayout(self.btn_row)

        # 失败3次以上标红边框
        self._apply_status_style()

    def _make_progress_text(self) -> str:
        t = self.task
        status_text = {
            TaskStatus.WAITING: "等待中",
            TaskStatus.DOWNLOADING: "下载中",
            TaskStatus.PAUSED: "已暂停",
            TaskStatus.CANCELED: "已取消",
            TaskStatus.DONE: "已完成",
            TaskStatus.FAILED: "失败",
        }.get(t.status, t.status)
        # 并发下载：下载中显示并发路数（如 3 路）
        if t.status == TaskStatus.DOWNLOADING:
            parallel = getattr(t, "parallel", 1) or 1
            suffix = f"（{parallel}路并发）" if parallel > 1 else ""
            # 视频下载中：合并 ffmpeg 期间显示已合并字节（否则全程 0/1 无变化）
            if t.content_type == "video" and t.merge_progress > 0:
                mb = t.merge_progress / 1048576.0
                return f"{t.done}/{t.total} 章节  正在合并 {mb:.1f} MB{suffix}"
            return f"{t.done}/{t.total} 章节  {status_text}{suffix}"
        return f"{t.done}/{t.total} 章节  {status_text}"

    def _build_buttons(self) -> None:
        """根据任务状态动态生成操作按钮。"""
        t = self.task
        q = self._page._queue
        tid = t.task_id

        def _btn(text, slot, warn=False):
            b = QPushButton(text)
            b.setFixedHeight(26)
            if warn:
                b.setStyleSheet(
                    "background-color: palette(mid); font-size: 11px; padding: 0 10px;"
                )
            else:
                b.setStyleSheet("font-size: 11px; padding: 0 10px;")
            b.clicked.connect(slot)
            return b

        if t.status in (TaskStatus.WAITING, TaskStatus.DOWNLOADING):
            self.btn_row.addWidget(_btn("暂停", lambda: q.pause_task(tid)))
            self.btn_row.addWidget(_btn("取消", lambda: q.cancel_task(tid), warn=True))
        elif t.status == TaskStatus.PAUSED:
            self.btn_row.addWidget(_btn("继续", lambda: q.resume_task(tid)))
            self.btn_row.addWidget(_btn("取消", lambda: q.cancel_task(tid), warn=True))
        elif t.status == TaskStatus.DONE:
            # 小说/漫画：产出 epub → 可打开阅读
            if t.content_type in ("novel", "comic"):
                self.btn_row.addWidget(
                    _btn("打开阅读", lambda: self._page._open_epub_for_task(tid))
                )
            self.btn_row.addWidget(_btn("打开文件夹", lambda: self._open_folder()))
            self.btn_row.addWidget(
                _btn("删除本地文件", lambda: self._page._delete_local_files(tid), warn=True)
            )
            self.btn_row.addWidget(
                _btn("从列表移除", lambda: q.remove_done(tid), warn=True)
            )
        elif t.status == TaskStatus.FAILED:
            self.btn_row.addWidget(_btn("重试", lambda: q.retry_task(tid)))
            self.btn_row.addWidget(
                _btn("忽略", lambda: q.remove_done(tid), warn=True)
            )
        elif t.status == TaskStatus.CANCELED:
            cancel_lbl = QLabel("已取消")
            cancel_lbl.setStyleSheet("font-size: 11px; color: palette(mid);")
            self.btn_row.addWidget(cancel_lbl)
            self.btn_row.addWidget(
                _btn("从列表移除", lambda: q.remove_done(tid), warn=True)
            )
        self.btn_row.addStretch()

    def _open_folder(self) -> None:
        """打开文件所在文件夹。"""
        out = Path(self.task.out_dir)
        if not out.exists():
            out.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(out))
        elif os.name == "posix":
            subprocess.Popen(["xdg-open", str(out)])

    def _apply_status_style(self) -> None:
        """失败任务（重试 3 次仍失败）标红色边框。"""
        t = self.task
        if t.status == TaskStatus.FAILED and len(t.failed) >= 3:
            self.setStyleSheet("border: 2px solid #D32F2F; border-radius: 8px;")

    # ------------------------------------------------------------------ #
    def refresh(self, task) -> None:
        """轻量刷新：更新进度条/文本/按钮（不重建）。

        状态变化时（暂停→继续→完成）重建按钮行，保证按钮跟随状态。
        """
        prev_status = getattr(self, "_last_status", None)
        self.task = task
        t = task
        self.progress_bar.setRange(0, max(1, t.total))
        self.progress_bar.setValue(t.done)
        self.progress_text.setText(self._make_progress_text())
        if t.speed_mbs > 0:
            self.speed_label.setText(f"{t.speed_mbs:.1f} MB/s")
        else:
            self.speed_label.setText("")
        if t.elapsed > 0:
            self.time_label.setText(f"已用{int(t.elapsed)}秒 剩余{int(t.remaining_s)}秒")
        else:
            self.time_label.setText("")
        # 并发下载中的章节（章节级进度展示）
        active = list(getattr(t, "active_titles", []) or [])
        if active and t.status == TaskStatus.DOWNLOADING:
            shown = active[:4]
            more = f" 等{len(active)}章" if len(active) > 4 else ""
            self.active_label.setText("正在下载：" + " / ".join(shown) + more)
            self.active_label.setVisible(True)
        else:
            self.active_label.setVisible(False)
        self._apply_status_style()
        # 状态变化 → 重建按钮行（暂停/继续/取消/重试随状态切换）
        if prev_status is not None and prev_status != t.status:
            self._rebuild_buttons()
        self._last_status = t.status

    def _rebuild_buttons(self) -> None:
        """清空并重建按钮行（按最新状态）。"""
        while self.btn_row.count():
            item = self.btn_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._build_buttons()


# ====================================================================== #
class DownloadPage(BasePage):
    """下载管理页：任务队列列表 + 并发控制 + 全局操作 + 筛选。"""

    open_epub_requested = Signal(object)  # 请求打开某任务的 epub（发 epub 路径）
    # 内部信号：事件 → 主线程处理（EventBus 回调在后台线程，QTimer.singleShot
    # 从后台线程调度不可靠，改用 Qt 信号自动 Queued 回主线程）
    _evt_to_ui = Signal(object)

    def __init__(
        self,
        queue,
        event_bus,
        settings,
        parent=None,
    ):
        super().__init__(parent)
        self._q = queue
        self._settings = settings
        self._bus = event_bus
        self._queue = queue
        self._tray = None
        # 下载完成通知开关（ui-download.md #10，设置页 download.show_notification）
        try:
            self._notify_enabled = bool(
                settings.get("download", "show_notification", True)
            )
        except Exception:  # noqa: BLE001
            self._notify_enabled = True

        self._evt_to_ui.connect(self._handle_event)

        self._build_ui()
        self._bus.subscribe(self._on_event)
        self._init_tray()

        # 进度高频刷新：限流，0.5 秒最多刷新一次
        self._progress_timer = QTimer(self)
        self._progress_timer.setSingleShot(True)
        self._progress_timer.setInterval(500)
        self._progress_timer.timeout.connect(self._light_refresh)

        self._full_rebuild()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 工具栏
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(12, 8, 12, 8)
        toolbar.setSpacing(8)

        # 并发数
        conc_lbl = QLabel("并发:")
        conc_lbl.setStyleSheet("font-size: 12px; color: palette(dark);")
        toolbar.addWidget(conc_lbl)

        self.conc_label = QLabel(str(self._queue.concurrent))
        self.conc_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        self.conc_label.setAlignment(Qt.AlignCenter)
        self.conc_label.setFixedWidth(30)
        toolbar.addWidget(self.conc_label)

        def _spin(delta):
            n = self._queue.concurrent + delta
            self._queue.set_concurrency(n)
            self._settings.set("download", "max_concurrent_downloads", n)
            self._settings.save()
            self.conc_label.setText(str(self._queue.concurrent))

        minus_btn = QPushButton("-")
        minus_btn.setFixedSize(28, 26)
        minus_btn.clicked.connect(lambda: _spin(-1))
        toolbar.addWidget(minus_btn)
        plus_btn = QPushButton("+")
        plus_btn.setFixedSize(28, 26)
        plus_btn.clicked.connect(lambda: _spin(1))
        toolbar.addWidget(plus_btn)

        # 分隔
        sep = QLabel("│")
        sep.setStyleSheet("color: palette(mid);")
        toolbar.addWidget(sep)

        # 全暂停/全继续/全取消
        def _btn(text, slot):
            b = QPushButton(text)
            b.setFixedHeight(26)
            b.clicked.connect(slot)
            return b

        toolbar.addWidget(_btn("全暂停", self._queue.pause_all))
        toolbar.addWidget(_btn("全继续", self._queue.resume_all))
        toolbar.addWidget(_btn("全取消", self._queue.cancel_all))
        toolbar.addStretch()

        # 筛选
        self.status_combo = QComboBox()
        for label, _ in STATUS_OPTIONS:
            self.status_combo.addItem(label)
        self.status_combo.currentIndexChanged.connect(lambda _: self._full_rebuild())
        self.status_combo.setFixedWidth(100)
        toolbar.addWidget(self.status_combo)

        self.type_combo = QComboBox()
        for label, _ in TYPE_OPTIONS:
            self.type_combo.addItem(label)
        self.type_combo.currentIndexChanged.connect(lambda _: self._full_rebuild())
        self.type_combo.setFixedWidth(80)
        toolbar.addWidget(self.type_combo)

        layout.addLayout(toolbar)

        # 内容堆栈（空态 / 列表）
        self.stack = QStackedWidget()

        # 空态
        empty = QWidget()
        el = QVBoxLayout(empty)
        el.addStretch(1)
        e_icon = QLabel("📭")
        e_icon.setAlignment(Qt.AlignCenter)
        e_icon.setStyleSheet("font-size: 40px;")
        el.addWidget(e_icon, alignment=Qt.AlignCenter)
        e_text = QLabel("还没有下载任务哦，去发现里逛逛吧")
        e_text.setAlignment(Qt.AlignCenter)
        e_text.setStyleSheet("font-size: 14px; color: palette(mid);")
        el.addWidget(e_text, alignment=Qt.AlignCenter)
        el.addStretch(1)
        self.stack.addWidget(empty)

        # 任务列表（滚动）
        self.list_scroll = QScrollArea()
        self.list_scroll.setWidgetResizable(True)
        self.list_scroll.setFrameShape(QFrame.NoFrame)
        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch()
        self.list_scroll.setWidget(self.list_container)
        self.stack.addWidget(self.list_scroll)

        layout.addWidget(self.stack, stretch=1)

    # ------------------------------------------------------------------ #
    # 筛选
    # ------------------------------------------------------------------ #
    def _active_filter(self) -> str | None:
        return STATUS_OPTIONS[self.status_combo.currentIndex()][1]

    def _type_filter(self) -> str | None:
        return TYPE_OPTIONS[self.type_combo.currentIndex()][1]

    def _filter_tasks(self, tasks):
        sf = self._active_filter()
        tf = self._type_filter()
        result = []
        for t in tasks:
            if tf and t.content_type != tf:
                continue
            if sf == "active" and t.status not in (
                TaskStatus.WAITING, TaskStatus.DOWNLOADING, TaskStatus.PAUSED
            ):
                continue
            if sf == "done" and t.status != TaskStatus.DONE:
                continue
            if sf == "failed" and t.status != TaskStatus.FAILED:
                continue
            result.append(t)
        return result

    # ------------------------------------------------------------------ #
    # 刷新
    # ------------------------------------------------------------------ #
    def _full_rebuild(self) -> None:
        """完整重建列表分区布局（状态/类型变化时调用）。"""
        tasks = self._filter_tasks(self._queue.tasks())
        has_any = len(tasks) > 0

        # 切换空态/列表
        self.stack.setCurrentIndex(0 if not has_any else 1)

        # 清空（含底部弹性）
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not has_any:
            self.list_layout.addStretch()
            return

        # 按状态分组：active → done → failed
        active = [
            t for t in tasks if t.status in (
                TaskStatus.WAITING, TaskStatus.DOWNLOADING, TaskStatus.PAUSED
            )
        ]
        done = [t for t in tasks if t.status == TaskStatus.DONE]
        failed = [t for t in tasks if t.status == TaskStatus.FAILED]
        canceled = [t for t in tasks if t.status == TaskStatus.CANCELED]

        if active:
            self._add_section_header(f"下载中 ({len(active)})")
            for t in active:
                self.list_layout.addWidget(_TaskCard(t, self))

        if done:
            self._add_section_header(f"已完成 ({len(done)})")
            for t in done:
                self.list_layout.addWidget(_TaskCard(t, self))

        if failed:
            self._add_section_header(f"失败 ({len(failed)})")
            for t in failed:
                self.list_layout.addWidget(_TaskCard(t, self))

        if canceled:
            self._add_section_header(f"已取消 ({len(canceled)})")
            for t in canceled:
                self.list_layout.addWidget(_TaskCard(t, self))

        self.list_layout.addStretch()

    def _add_section_header(self, text: str) -> None:
        h = QLabel(text)
        h.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: palette(mid);"
            "padding: 8px 0 2px 12px;"
        )
        self.list_layout.addWidget(h)

    def _light_refresh(self) -> None:
        """轻量刷新：只更新现有卡片进度值，不重建布局。"""
        self._needs_progress_refresh = False
        for i in range(self.list_layout.count()):
            item = self.list_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), _TaskCard):
                card = item.widget()
                task = card.task
                # 从队列取最新状态
                latest = self._queue.get(task.task_id)
                if latest:
                    card.refresh(latest)

    # ------------------------------------------------------------------ #
    # EventBus 事件处理（子线程 emit → QTimer 回主线程）
    # ------------------------------------------------------------------ #
    def on_event(self, event) -> None:
        """EventBus 事件回调（subscribe 注册此方法）。

        EventBus.emit 同步调用本方法 —— 可能在后台 worker 线程执行。
        QTimer.singleShot 从非主线程调度不可靠（事件丢失，UI 不刷新），
        改用 Qt 信号 self._evt_to_ui（auto 连接 → Queued 回主线程）触发
        _handle_event，保证任何下载事件都可靠地驱动 UI 更新。
        """
        t = event.type
        if t not in (
            EVENT_DOWNLOAD_STARTED,
            EVENT_DOWNLOAD_PROGRESS,
            EVENT_DOWNLOAD_COMPLETED,
            EVENT_DOWNLOAD_FAILED,
        ):
            return
        self._evt_to_ui.emit(event)

    def _on_event(self, event) -> None:
        """订阅包装（subscribe 需要显式回调约定），实际走 on_event。"""
        self.on_event(event)

    def _handle_event(self, event) -> None:
        t = event.type
        if t == EVENT_DOWNLOAD_STARTED:
            self._full_rebuild()
        elif t == EVENT_DOWNLOAD_PROGRESS:
            # 高频限流：标记需要刷新，0.5 秒内合并多次进度
            self._needs_progress_refresh = True
            if not self._progress_timer.isActive():
                self._progress_timer.start()
                self._light_refresh()
        elif t in (EVENT_DOWNLOAD_COMPLETED, EVENT_DOWNLOAD_FAILED):
            self._full_rebuild()
            # 通知：托盘 + GUI 内弹窗双通道
            payload = event.payload or {}
            title = payload.get("title", "")
            if t == EVENT_DOWNLOAD_COMPLETED:
                msg = f"《{title}》下载完成"
            else:
                msg = f"《{title}》下载失败: {payload.get('error', '未知错误')}"
            # 通道1：系统托盘通知
            if self._tray is not None:
                try:
                    icon = QSystemTrayIcon.Information if t == EVENT_DOWNLOAD_COMPLETED else QSystemTrayIcon.Warning
                    self._tray.showMessage("下载通知", msg, icon, 4000)
                except Exception:
                    pass
            # 通道2：GUI 内非模态 toast（始终显示，不依赖托盘）
            self._show_toast(msg)

    def _show_toast(self, msg: str) -> None:
        """弹出一个非模态 3 秒自动消失的 GUI 内通知 toast。"""
        toast = QLabel(msg, self)
        toast.setStyleSheet(
            "background: rgba(50,50,50,220); color: white;"
            "font-size: 13px; padding: 10px 18px; border-radius: 8px;"
        )
        toast.setAlignment(Qt.AlignCenter)
        toast.setWindowFlags(Qt.ToolTip)
        # 位置：顶部居中
        toast.adjustSize()
        parent_geo = self.geometry()
        toast.move(
            parent_geo.center().x() - toast.width() // 2,
            parent_geo.top() + 20,
        )
        toast.show()
        QTimer.singleShot(3000, toast.deleteLater)

    def _init_tray(self) -> None:
        """初始化系统托盘图标（用于下载完成通知）。开关关闭则跳过。"""
        if not self._notify_enabled:
            self._tray = None
            return
        try:
            from PySide6.QtGui import QColor, QIcon, QPixmap

            self._tray = QSystemTrayIcon(self)
            self._tray.setToolTip("多源爬虫框架")
            pix = QPixmap(16, 16)
            pix.fill(QColor("#EC4899"))
            self._tray.setIcon(QIcon(pix))
            self._tray.show()
        except Exception:
            self._tray = None  # 托盘不可用时降级为仅 GUI 内通知

    # ------------------------------------------------------------------ #
    def on_theme_changed(self, theme: str) -> None:
        """主题切换时刷新卡片样式。"""
        self._full_rebuild()

    # ------------------------------------------------------------------ #
    # 任务本地文件操作（ui-download.md #7 已完成区：删除本地文件）
    # ------------------------------------------------------------------ #
    def _delete_local_files(self, task_id: str) -> None:
        """删除下载任务已落盘的本地文件（书目录），弹确认框。"""
        from PySide6.QtWidgets import QMessageBox

        task = self._queue.get(task_id)
        if task is None:
            return
        out_dir = Path(task.out_dir)
        if not out_dir.exists():
            # 无文件，直接从列表移除
            self._queue.remove_done(task_id)
            self._full_rebuild()
            return
        resp = QMessageBox.question(
            self,
            "确认删除",
            f"确定删除《{task.title}》的本地文件吗？\n{out_dir}\n\n"
            "该操作不可恢复。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return
        try:
            import shutil

            shutil.rmtree(out_dir, ignore_errors=True)
        except OSError:
            pass
        self._queue.remove_done(task_id)
        self._full_rebuild()

    def _open_epub_for_task(self, task_id: str) -> None:
        """「打开阅读」：找任务产出的 .epub 文件，发信号给 App 打开 epub 阅读器。"""
        task = self._queue.get(task_id)
        if task is None:
            return
        out_dir = Path(task.out_dir)
        if not out_dir.is_dir():
            return
        epub_files = list(out_dir.glob("*.epub"))
        if not epub_files:
            return
        self.open_epub_requested.emit(str(epub_files[0]))
