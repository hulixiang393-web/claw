"""作品卡片（WorkCard）。

发现列表 / 搜索结果共用的卡片：封面图 + 标题 + 来源/作者 + 更新。
封面从 work.cover URL 异步加载（QNetworkAccessManager），加载中显示占位。
点击触发选中（信号）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


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

        # 封面（占位，随后异步加载真实图）
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

        # 异步加载封面
        self._manager = None
        if work.cover:
            self._load_cover(work.cover)

    # ------------------------------------------------------------------ #
    def _load_cover(self, url: str) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

        manager = QNetworkAccessManager(self)
        manager.finished.connect(
            lambda reply: self._on_cover_reply(reply, manager)
        )
        request = QNetworkRequest(QUrl(url))
        request.setHeader(QNetworkRequest.UserAgentHeader, "Mozilla/5.0 SpiderDemo")
        manager.get(request)
        self._manager = manager

    def _on_cover_reply(self, reply, manager) -> None:
        from PySide6.QtCore import Qt as _Qt

        try:
            data = reply.readAll()
            pixmap = QPixmap()
            if pixmap.loadFromData(data) and not pixmap.isNull():
                scaled = pixmap.scaled(
                    140, 180, _Qt.KeepAspectRatio, _Qt.SmoothTransformation
                )
                self._cover.setPixmap(scaled)
        except Exception:
            pass
        reply.deleteLater()
        manager.deleteLater()
        self._manager = None

    def set_cover_image(self, pixmap) -> None:
        """设置真实封面图（缩略）。"""
        from PySide6.QtCore import Qt as _Qt

        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(
                140, 180, _Qt.KeepAspectRatio, _Qt.SmoothTransformation
            )
            self._cover.setPixmap(scaled)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit(self.work)
        super().mouseReleaseEvent(event)
