"""三态吉祥物（MascotWidget）。

Q 版小猫角色，由 QPainter 精细绘制，三态差异明显：
- 待机 idle  ：闭眼微笑 + 安静坐姿（呼吸缩放动画）
- 运行 running：睁眼 + 猫爪忙碌转圈（爪子围绕身体转）
- 报错 error ：哭脸 + 泪滴落下

对应 ui-home.md「吉祥物三态联动」+ theme.md 三态吉祥物。
由 EventBus 事件驱动状态。插画成品资源第二阶段替换（换资源不改逻辑）。
"""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, QTimer, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF, QBrush
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

# 三态主色：待机=樱花粉，运行=薄荷绿，报错=珊瑚红
_STATE_COLORS = {
    STATE_IDLE: "#FF8FAB",
    STATE_RUNNING: "#7AE0C9",
    STATE_ERROR: "#FF6B6B",
}


class MascotWidget(QWidget):
    """Q 版猫三态吉祥物。"""

    def __init__(self, parent=None, size: int = 48):
        super().__init__(parent)
        self._state = STATE_IDLE
        self.setFixedSize(size, size)
        # 动画时钟：驱动呼吸/转圈/泪滴
        self._tick = 0
        self._timer = QTimer(self)
        self._timer.setInterval(60)  # ~16fps，省 CPU
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    # ------------------------------------------------------------------ #
    def set_state(self, state: str) -> None:
        if state not in _STATE_COLORS:
            state = STATE_IDLE
        if state != self._state:
            self._state = state
            self._tick = 0  # 切换状态重置动画相位
            self.update()

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

    def _on_tick(self) -> None:
        """动画帧：只有 running 态需要持续转圈，其余态低频呼吸。"""
        self._tick += 1
        if self._state == STATE_RUNNING:
            self.update()  # 持续刷新（转圈）
        elif self._tick % 20 == 0:
            self.update()  # idle/error 低频呼吸

    # ------------------------------------------------------------------ #
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        color = QColor(_STATE_COLORS[self._state])

        # 呼吸缩放：身体大小随 tick 轻微脉动
        breath = 1.0 + 0.03 * math.sin(self._tick * 0.15)
        body_r = min(w, h) * 0.30 * breath

        # 猫耳（两个三角形，画在身体后）
        ear_color = color.lighter(150)
        ear_pen = QPen(color.darker(120), 2)
        painter.setPen(ear_pen)
        painter.setBrush(QBrush(ear_color))
        ear_dx = body_r * 0.55
        ear_dy = body_r * 0.30
        for side in (-1, 1):
            ear = QPolygonF([
                QPointF(cx + side * ear_dx - body_r * 0.35, cy - body_r * 0.45),
                QPointF(cx + side * ear_dx + body_r * 0.35, cy - body_r * 0.45),
                QPointF(cx + side * ear_dx * 0.9, cy - body_r * 1.35),
            ])
            painter.drawPolygon(ear)
        # 耳内粉色
        inner_color = color.lighter(180)
        painter.setBrush(QBrush(inner_color))
        painter.setPen(Qt.NoPen)
        for side in (-1, 1):
            inner = QPolygonF([
                QPointF(cx + side * ear_dx - body_r * 0.20, cy - body_r * 0.55),
                QPointF(cx + side * ear_dx + body_r * 0.20, cy - body_r * 0.55),
                QPointF(cx + side * ear_dx * 0.9, cy - body_r * 1.15),
            ])
            painter.drawPolygon(inner)

        # 身体（圆脸）
        painter.setPen(QPen(color.darker(110), 2))
        painter.setBrush(QBrush(color.lighter(160)))
        painter.drawEllipse(
            int(cx - body_r), int(cy - body_r),
            int(body_r * 2), int(body_r * 2),
        )

        # 腮红（两侧粉晕）
        blush_color = QColor("#FFB6C1")
        painter.setBrush(QBrush(blush_color))
        painter.setPen(Qt.NoPen)
        for side in (-1, 1):
            painter.drawEllipse(
                int(cx + side * body_r * 0.85 - body_r * 0.14),
                int(cy + body_r * 0.25 - body_r * 0.10),
                int(body_r * 0.28), int(body_r * 0.20),
            )

        eye_color = QColor("#3D2A3B")
        eye_pen = QPen(eye_color, 2)
        painter.setPen(eye_pen)
        painter.setBrush(QBrush(eye_color))

        # 眼睛（按状态）
        eye_dx = body_r * 0.38
        eye_y = cy - body_r * 0.10
        if self._state == STATE_IDLE:
            # 闭眼微笑：弯弧
            painter.setBrush(Qt.NoBrush)
            for side in (-1, 1):
                painter.drawArc(
                    int(cx + side * eye_dx - body_r * 0.22),
                    int(eye_y - body_r * 0.10),
                    int(body_r * 0.44), int(body_r * 0.28),
                    20 * 16, 140 * 16,
                )
        elif self._state == STATE_ERROR:
            # 哭脸：皱眉 + 泪滴
            painter.setBrush(QBrush(eye_color))
            for side in (-1, 1):
                painter.drawEllipse(
                    int(cx + side * eye_dx - body_r * 0.10),
                    int(eye_y - body_r * 0.12),
                    int(body_r * 0.20), int(body_r * 0.20),
                )
            # 泪滴（往下落）
            tear_color = QColor("#7EC8FF")
            painter.setBrush(QBrush(tear_color))
            painter.setPen(Qt.NoPen)
            drop_y = eye_y + body_r * 0.30 + (self._tick % 30) * body_r * 0.02
            painter.drawEllipse(
                int(cx + eye_dx - body_r * 0.06),
                int(drop_y),
                int(body_r * 0.12), int(body_r * 0.16),
            )
        else:  # RUNNING：睁眼 + 忙碌
            painter.setBrush(QBrush(eye_color))
            for side in (-1, 1):
                painter.drawEllipse(
                    int(cx + side * eye_dx - body_r * 0.12),
                    int(eye_y - body_r * 0.12),
                    int(body_r * 0.24), int(body_r * 0.24),
                )
            # 瞳孔高光
            painter.setBrush(QBrush(QColor("#FFFFFF")))
            for side in (-1, 1):
                painter.drawEllipse(
                    int(cx + side * eye_dx - body_r * 0.02),
                    int(eye_y - body_r * 0.20),
                    int(body_r * 0.06), int(body_r * 0.06),
                )

        # 嘴（按状态）
        mx, my = cx, cy + body_r * 0.30
        mouth_r = body_r * 0.22
        painter.setPen(QPen(eye_color, 2))
        if self._state == STATE_IDLE:
            # 微笑上弯
            painter.setBrush(Qt.NoBrush)
            painter.drawArc(
                int(mx - mouth_r), int(my - mouth_r * 0.6),
                int(mouth_r * 2), int(mouth_r * 1.2),
                20 * 16, 140 * 16,
            )
        elif self._state == STATE_ERROR:
            # 哭嘴下弯
            painter.setBrush(Qt.NoBrush)
            painter.drawArc(
                int(mx - mouth_r), int(my),
                int(mouth_r * 2), int(mouth_r * 1.2),
                200 * 16, 140 * 16,
            )
        else:
            # 张开的 O 嘴（忙碌）
            painter.setBrush(QBrush(color))
            painter.drawEllipse(
                int(mx - mouth_r * 0.5), int(my - mouth_r * 0.3),
                int(mouth_r), int(mouth_r * 0.9),
            )

        # 胡须（两侧）
        whisker_pen = QPen(QColor("#8A6F6F"), 1)
        painter.setPen(whisker_pen)
        for side in (-1, 1):
            for dy in (-0.10, 0.05, 0.20):
                y = cy + body_r * (0.10 + dy)
                x0 = cx + side * body_r * 0.80
                x1 = cx + side * body_r * 1.25
                painter.drawLine(
                    int(x0), int(y), int(x1), int(y + body_r * 0.08 * side),
                )

        # 运行态：猫爪转圈（在身体外围绕转）
        if self._state == STATE_RUNNING:
            paw_color = color.lighter(130)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(paw_color))
            angle = self._tick * 0.2  # 每帧转一点
            for k in range(3):
                a = angle + k * math.pi * 2 / 3
                px = cx + math.cos(a) * body_r * 1.35
                py = cy + math.sin(a) * body_r * 1.10
                painter.drawEllipse(
                    int(px - body_r * 0.14), int(py - body_r * 0.14),
                    int(body_r * 0.28), int(body_r * 0.28),
                )
        painter.end()
