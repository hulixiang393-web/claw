"""作品卡片（WorkCard）。

发现列表 / 搜索结果共用的卡片：封面图 + 标题 + 来源/作者 + 更新。
封面通过 CoverLoader（全局共享+限流）异步加载，避免并发失控。
点击触发选中（信号）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

from .cover_loader import CoverLoader


class WorkCard(QFrame):
    """单张作品卡片。"""

    clicked = Signal(object)  # 发射 Work 对象

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self.work = work
        self.setObjectName("workCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(150)
        self.setMaximumWidth(170)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # 封面（CoverLoader 全局限流加载）
        cover = QLabel("🖼")
        cover.setAlignment(Qt.AlignCenter)
        cover.setFixedSize(140, 180)
        cover.setStyleSheet("background: palette(midlight); border-radius: 8px; font-size: 36px;")
        layout.addWidget(cover)
        self._cover = cover

        title = QLabel(work.title or "无标题")
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        title.setMinimumHeight(36)
        layout.addWidget(title)
        self._title = title

        src = QLabel(work.source_name or "")
        src.setStyleSheet("color: palette(dark); font-size: 10px;")
        layout.addWidget(src)

        # 异步加载封面（全局限流，不会并发爆炸）
        self._pix = None
        if work.cover:
            self._load_cover(work.cover)

    def _load_cover(self, url: str) -> None:
        """通过 CoverLoader 加载封面（全局最多 4 个并发）。"""
        CoverLoader.instance().load(url, self._on_cover_ready)

    def _on_cover_ready(self, pixmap) -> None:
        """CoverLoader 回调：更新封面。"""
        if pixmap is not None:
            from PySide6.QtCore import Qt as _Qt
            scaled = pixmap.scaled(
                140, 180, _Qt.KeepAspectRatio, _Qt.SmoothTransformation
            )
            self._cover.setPixmap(scaled)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit(self.work)
        super().mouseReleaseEvent(event)
