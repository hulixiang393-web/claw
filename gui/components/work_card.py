"""作品卡片（WorkCard）。

发现列表 / 搜索结果共用的卡片：封面 + 标题 + 来源/作者 + 更新。
点击触发选中（信号）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


class WorkCard(QFrame):
    """单张作品卡片。"""

    clicked = Signal(object)  # 发射 Work 对象

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self.work = work
        self.setObjectName("workCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(140)
        self.setMaximumWidth(160)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # 封面占位
        cover = QLabel("🖼")
        cover.setAlignment(Qt.AlignCenter)
        cover.setFixedHeight(120)
        cover.setStyleSheet("background: palette(midlight); border-radius: 8px; font-size: 32px;")
        layout.addWidget(cover)
        self._cover = cover

        title = QLabel(work.title or "无标题")
        title.setWordWrap(True)
        title.setMaximumHeight(40)
        layout.addWidget(title)

        src = QLabel(work.source_name or "")
        src.setStyleSheet("color: palette(dark); font-size: 10px;")
        layout.addWidget(src)

    def set_cover_image(self, pixmap) -> None:
        """设置真实封面图（缩略）。"""
        from PySide6.QtCore import Qt as _Qt

        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(
                130, 120, _Qt.KeepAspectRatio, _Qt.SmoothTransformation
            )
            self._cover.setPixmap(scaled)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit(self.work)
        super().mouseReleaseEvent(event)
