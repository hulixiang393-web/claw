"""三态吉祥物（MascotWidget）。

待机 / 运行 / 报错 三态，由 EventBus 事件驱动。
对应 ui-home.md「吉祥物三态联动」。
插画成品资源第二阶段替换；本阶段用 QPainter 绘制占位表情，换资源不改逻辑。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from framework.events import (
    EVENT_SEARCH_STARTED,
    EVENT_SEARCH_COMPLETED,
    EVENT_DOWNLOAD_STARTED,
    EVENT_DOWNLOAD_COMPLETED,
    EVENT_DOWNLOAD_FAILED,
    EVENT_SOURCE_ERROR,
    EVENT_STRUCTURE_CHANGED,
)

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_ERROR = "error"

# 三种表情的占位配色
_STATE_COLORS = {
    STATE_IDLE: "#FF8FAB",      # 樱花粉
    STATE_RUNNING: "#7AE0C9",   # 薄荷绿
    STATE_ERROR: "#FF6B6B",     # 珊瑚红
}


class MascotWidget(QWidget):
    """三态吉祥物。占位绘制圆形 + 眼睛/嘴，表示三态。"""

    def __init__(self, parent=None, size: int = 40):
        super().__init__(parent)
        self._state = STATE_IDLE
        self.setFixedSize(size, size)

    # ------------------------------------------------------------------ #
    def set_state(self, state: str) -> None:
        if state not in _STATE_COLORS:
            state = STATE_IDLE
        if state != self._state:
            self._state = state
            self.update()  # 重绘

    def current_state(self) -> str:
        return self._state

    def on_event(self, event) -> None:
        """由 EventBus 事件驱动三态。"""
        t = event.type
        if t in (EVENT_SEARCH_STARTED, EVENT_DOWNLOAD_STARTED):
            self.set_state(STATE_RUNNING)
        elif t in (
            EVENT_SEARCH_COMPLETED,
            EVENT_DOWNLOAD_COMPLETED,
            EVENT_DOWNLOAD_FAILED,
        ):
            self.set_state(STATE_IDLE)
        elif t in (EVENT_SOURCE_ERROR, EVENT_STRUCTURE_CHANGED):
            self.set_state(STATE_ERROR)

    # ------------------------------------------------------------------ #
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height())
        color = QColor(_STATE_COLORS[self._state])

        # 身体（圆形）
        painter.setPen(QPen(color, 2))
        painter.setBrush(color.lighter(165))
        body = side * 0.62
        painter.drawEllipse(
            int((self.width() - body) / 2),
            int((self.height() - body) / 2),
            int(body),
            int(body),
        )

        eye_color = QColor("#3D2A3B")
        painter.setPen(QPen(eye_color, 2))
        # 眼睛
        eye_dx = side * 0.13
        eye_y = self.height() * 0.40
        for ex in (self.width() / 2 - eye_dx, self.width() / 2 + eye_dx):
            painter.drawPoint(int(ex), int(eye_y))

        # 嘴（按状态）
        mx, my = self.width() / 2, self.height() * 0.55
        mouth_r = side * 0.09
        if self._state == STATE_RUNNING:
            # 张开的"O"嘴（忙碌）
            painter.setBrush(color)
            painter.drawEllipse(int(mx - mouth_r), int(my - mouth_r), int(mouth_r * 2), int(mouth_r * 2))
        elif self._state == STATE_ERROR:
            # 下弯嘴角（叹气）
            painter.drawArc(
                int(mx - mouth_r), int(my), int(mouth_r * 2), int(mouth_r * 1.4), 200 * 16, 140 * 16
            )
        else:
            # 平嘴（待机）
            painter.drawLine(int(mx - mouth_r), int(my), int(mx + mouth_r), int(my))
        painter.end()
