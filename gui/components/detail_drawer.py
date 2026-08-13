"""详情抽屉（DetailDrawer）。

点作品后右侧展示：封面/标题/作者/状态/简介 + 章节数 + 操作按钮。
对应 ui-discover.md「详情预览抽屉」。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
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

        # 图文集（视频截图序列等）：横向缩略图条，占位隐藏
        self._gallery_scroll: QScrollArea | None = None
        self._gallery_row: QHBoxLayout | None = None
        self._gallery_thumbs: list[QLabel] = []

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
        # 视频源磁力/番号页（如 avgood 搜索结果的 /c/{id}）：无在线播放，提示用户
        import re as _re

        is_magnet_page = bool(
            detail.content_type == "video"
            and not detail.chapters
            and _re.search(r"/c/\d+\.html", detail.url or "")
        )
        if is_magnet_page:
            self.chapters_label.setText(
                "磁力/番号页（仅下载，不可在线播放）\n"
                "该站搜索接口只返回磁力库条目，線上影片请用「发现」分类浏览"
            )
        else:
            self.chapters_label.setText(
                f"共 {len(detail.chapters)} 章节" if detail.chapters else "无章节信息"
            )
        self.summary.setText(detail.summary or "（无简介）")
        # 抽屉内不显示任何图片（封面/图文集）：隐藏封面区与 gallery 图集条，
        # 避免广告图/截图序列遮挡按钮或干扰阅读（布局自动重排，标题上移）。
        self.cover.setVisible(False)
        if self._gallery_scroll is not None:
            self._gallery_scroll.setVisible(False)
        self.setVisible(True)

    def _show_gallery(self, images: list) -> None:
        """展示图文集缩略图条（横向滚动）；无图则隐藏/清空。"""
        import shiboken6

        if not images:
            if self._gallery_scroll is not None and shiboken6.isValid(self._gallery_scroll):
                self._gallery_scroll.setVisible(False)
                for thumb in self._gallery_thumbs:
                    if shiboken6.isValid(thumb):
                        thumb.deleteLater()
                self._gallery_thumbs = []
            return
        if self._gallery_scroll is None:
            self._gallery_scroll = QScrollArea()
            self._gallery_scroll.setWidgetResizable(True)
            self._gallery_scroll.setFixedHeight(100)
            self._gallery_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self._gallery_scroll.setStyleSheet(
                "QScrollArea { border: none; background: transparent; }"
                "QScrollBar:horizontal { height: 6px; }"
            )
            inner = QWidget()
            self._gallery_row = QHBoxLayout(inner)
            self._gallery_row.setContentsMargins(0, 0, 0, 0)
            self._gallery_row.setSpacing(6)
            self._gallery_scroll.setWidget(inner)
            self.layout().insertWidget(self.layout().count() - 2, self._gallery_scroll)
        else:
            for thumb in self._gallery_thumbs:
                if shiboken6.isValid(thumb):
                    thumb.deleteLater()
            self._gallery_thumbs = []
            while self._gallery_row.count():
                item = self._gallery_row.takeAt(0)
                w = item.widget()
                if w:
                    w.deleteLater()
        self._gallery_scroll.setVisible(True)

        def _make_thumb(url: str) -> QLabel:
            thumb = QLabel()
            thumb.setFixedSize(100, 80)
            thumb.setAlignment(Qt.AlignCenter)
            thumb.setStyleSheet(
                "background: palette(midlight); border-radius: 6px;"
                "font-size: 24px;"
            )
            thumb.setText("🖼")

            def _on_ready(pixmap) -> None:
                import shiboken6 as _shib

                if pixmap is None or not _shib.isValid(thumb):
                    return
                scaled = pixmap.scaled(
                    100, 80, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
                )
                sx = max(0, (scaled.width() - 100) // 2)
                sy = max(0, (scaled.height() - 80) // 2)
                crop = scaled.copy(sx, sy, min(100, scaled.width()), min(80, scaled.height()))
                thumb.setPixmap(crop)

            from gui.components.cover_loader import CoverLoader

            CoverLoader.instance().load(url, _on_ready)
            return thumb

        for url in images:
            thumb = _make_thumb(url)
            self._gallery_row.addWidget(thumb)
            self._gallery_thumbs.append(thumb)

    def _load_cover(self, url: str) -> None:
        """异步加载封面（CoverLoader 全局限流），失败/为空保留占位符。"""
        if not url:
            self.cover.setText("🖼")
            return
        from gui.components.cover_loader import CoverLoader

        def _on_ready(pixmap) -> None:
            import shiboken6

            if pixmap is None or not shiboken6.isValid(self.cover):
                return  # 加载失败或控件已销毁，保留占位
            scaled = pixmap.scaled(
                320, 180, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            sx = max(0, (scaled.width() - 320) // 2)
            sy = max(0, (scaled.height() - 180) // 2)
            cropped = scaled.copy(sx, sy, min(320, scaled.width()), min(180, scaled.height()))
            self.cover.setPixmap(cropped)
            from gui.components.cover_loader import fade_in

            fade_in(self.cover)

        if url.startswith("data:"):
            # data URI（解密后封面）→ 直接解码
            try:
                _, b64 = url.split(",", 1)
                import base64

                data = base64.b64decode(b64)
                pix = QPixmap()
                if pix.loadFromData(data) and not pix.isNull():
                    _on_ready(pix)
                    return
            except Exception:  # noqa: BLE001
                pass
            self.cover.setText("🖼")
            return
        CoverLoader.instance().load(url, _on_ready)

    def hide_detail(self) -> None:
        self.setVisible(False)

    def is_open(self) -> bool:
        return not self.isHidden()
