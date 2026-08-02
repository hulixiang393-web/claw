"""迷你任务进度（MiniProgress）。

订阅 EventBus，显示当前后台任务数量与进度。
对应 ui-home.md「迷你任务进度」。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

from framework.events import (
    EVENT_SEARCH_STARTED,
    EVENT_SEARCH_COMPLETED,
    EVENT_DOWNLOAD_STARTED,
    EVENT_DOWNLOAD_COMPLETED,
    EVENT_DOWNLOAD_PROGRESS,
)


class _TaskRow(QWidget):
    """单条任务进度。"""

    def __init__(self, task_id: str, label: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._id = task_id

        lbl = QLabel(label)
        lbl.setFixedWidth(180)
        layout.addWidget(lbl)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFixedHeight(8)
        layout.addWidget(self.progress, stretch=1)

        self.pct = QLabel("0%")
        self.pct.setFixedWidth(40)
        layout.addWidget(self.pct)

    def update_progress(self, done: int, total: int) -> None:
        if total > 0:
            pct_val = int(done / total * 100)
            self.progress.setValue(pct_val)
            self.pct.setText(f"{pct_val}%")

    def finish(self) -> None:
        self.progress.setValue(100)
        self.pct.setText("100%")


class MiniProgress(QWidget):
    """迷你任务进度条：多个任务并行显示。"""

    def __init__(self, event_bus, parent=None):
        super().__init__(parent)
        self._bus = event_bus
        self._tasks: dict[str, _TaskRow] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._layout = layout

        header = QLabel("后台任务")
        header.setObjectName("statsLabel")
        self._header = header
        layout.addWidget(header)
        self._empty_label = QLabel("  无进行中任务")
        self._empty_label.setObjectName("statsLabel")
        layout.addWidget(self._empty_label)

        bus = event_bus
        if bus is not None:
            bus.subscribe(self._on_event)

    def _on_event(self, event) -> None:
        t = event.type
        sid = event.source_id or ""

        if t == EVENT_SEARCH_STARTED:
            self._add_task(sid, f"搜索中 [{sid}]")
            self._empty_label.setVisible(False)
        elif t == EVENT_SEARCH_COMPLETED:
            self._remove_task(sid)
        elif t == EVENT_DOWNLOAD_STARTED:
            self._add_task(sid, f"下载中 [{sid}]")
            self._empty_label.setVisible(False)
        elif t == EVENT_DOWNLOAD_PROGRESS:
            p = event.payload or {}
            row = self._tasks.get(sid)
            if row:
                row.update_progress(p.get("done", 0), p.get("total", 1))
        elif t == EVENT_DOWNLOAD_COMPLETED:
            self._remove_task(sid)

        if not self._tasks:
            self._empty_label.setVisible(True)

    def _add_task(self, task_id: str, label: str) -> None:
        if task_id in self._tasks:
            return
        row = _TaskRow(task_id, label)
        self._tasks[task_id] = row
        self._layout.addWidget(row)

    def _remove_task(self, task_id: str) -> None:
        row = self._tasks.pop(task_id, None)
        if row:
            row.finish()
            self._layout.removeWidget(row)
            row.deleteLater()

    def clear_all(self) -> None:
        for tid in list(self._tasks):
            self._remove_task(tid)
        self._empty_label.setVisible(True)
