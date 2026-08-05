"""详情抽屉（DetailDrawer）。

点作品后右侧展示：封面/标题/作者/状态/简介 + 章节数 + 操作按钮。
对应 ui-discover.md「详情预览抽屉」。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from framework.content import Detail


class DetailDrawer(QFrame):
    """详情抽屉。"""

    read_requested = Signal(object)      # Detail
    open_url_requested = Signal(str)     # url
    download_requested = Signal(object)  # Detail
    favorite_requested = Signal(object)  # Detail（收藏/取消收藏）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("detailDrawer")
        self.setFixedWidth(320)
        # 隐藏时仍保留布局占位，防止抽屉显示/隐藏时网格列数跳变/闪屏
        from PySide6.QtWidgets import QSizePolicy

        sp = QSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        sp.setRetainSizeWhenHidden(True)
        self.setSizePolicy(sp)
        self.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # 顶栏：标题占位 + 关闭按钮
        header = QHBoxLayout()
        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setToolTip("关闭详情")
        self.close_btn.setStyleSheet(
            "font-size: 14px; border: none; border-radius: 14px;"
            "background: palette(midlight); color: palette(text);"
        )
        self.close_btn.clicked.connect(self.hide_detail)
        header.addStretch(1)
        header.addWidget(self.close_btn)
        layout.addLayout(header)

        # 封面占位
        self.cover = QLabel("🖼")
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setFixedHeight(180)
        self.cover.setStyleSheet("background: palette(midlight); border-radius: 8px; font-size: 40px;")
        layout.addWidget(self.cover)

        self.title = QLabel("")
        self.title.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.title.setWordWrap(True)
        layout.addWidget(self.title)

        self.author = QLabel("")
        self.author.setStyleSheet("color: palette(dark); font-size: 12px;")
        layout.addWidget(self.author)

        self.status = QLabel("")
        self.status.setStyleSheet("color: palette(mid); font-size: 11px;")
        layout.addWidget(self.status)

        self.chapters_label = QLabel("")
        self.chapters_label.setStyleSheet("color: palette(dark); font-size: 11px;")
        layout.addWidget(self.chapters_label)

        # 简介（滚动）
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color: palette(text); font-size: 12px;")
        self.summary.setMaximumHeight(120)
        self.summary.setAlignment(Qt.AlignTop)
        layout.addWidget(self.summary)

        layout.addStretch(1)

        # 按钮
        btn_row = QHBoxLayout()
        self.read_btn = QPushButton("开始阅读")
        self.read_btn.clicked.connect(lambda: self.read_requested.emit(self._detail))
        btn_row.addWidget(self.read_btn)

        self.download_btn = QPushButton("下载")
        self.download_btn.clicked.connect(lambda: self.download_requested.emit(self._detail))
        btn_row.addWidget(self.download_btn)

        self.fav_btn = QPushButton("☆ 收藏")
        self.fav_btn.setCheckable(True)
        self.fav_btn.clicked.connect(lambda: self.favorite_requested.emit(self._detail))
        btn_row.addWidget(self.fav_btn)
        layout.addLayout(btn_row)

        self.open_btn = QPushButton("打开源详情页")
        self.open_btn.setFlat(True)
        self.open_btn.clicked.connect(
            lambda: self.open_url_requested.emit(self._detail.url)
        )
        layout.addWidget(self.open_btn)

        self._detail: Detail | None = None

    # ------------------------------------------------------------------ #
    def show_detail(self, detail: Detail) -> None:
        """展示详情。"""
        self._detail = detail
        self.title.setText(detail.title or "无标题")
        self.author.setText(detail.author or "")
        self.status.setText(detail.status or "")
        self.chapters_label.setText(f"共 {len(detail.chapters)} 章节" if detail.chapters else "无章节信息")
        self.summary.setText(detail.summary or "（无简介）")
        if detail.cover:
            self.cover.setText("🖼")
        self.setVisible(True)

    def hide_detail(self) -> None:
        self.setVisible(False)

    def is_open(self) -> bool:
        return not self.isHidden()
