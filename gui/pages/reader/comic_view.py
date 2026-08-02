"""漫画阅读视图（ComicView）。

对应 ui-reader.md 漫画功能点：
- 图片逐页加载 + 预加载
- 滚动画廊（默认）/ 横向翻页可切换
- 图片缩放（点击放大/双击还原）
- 屏幕适应
- 下一话/上一话跳转
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QThreadPool, QRunnable, QObject
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from framework.content import Content, Detail


class ComicView(QWidget):
    """漫画翻页阅读视图。"""

    chapter_changed = Signal(object)

    def __init__(self, content: Content, parent=None):
        super().__init__(parent)
        self._content = content
        self._source = None
        self._detail: Detail | None = None
        self._chapters = []
        self._current_idx = -1
        self._images = []
        self._mode = "gallery"  # gallery / flip
        self._zoom = 1.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # ---- 工具栏 ----
        toolbar = QHBoxLayout()
        self.mode_btn = QPushButton("切换横向模式")
        self.mode_btn.clicked.connect(self._toggle_mode)
        toolbar.addWidget(self.mode_btn)

        self.prev_btn = QPushButton("上一话")
        self.next_btn = QPushButton("下一话")
        self.prev_btn.clicked.connect(lambda: self._jump_relative(-1))
        self.next_btn.clicked.connect(lambda: self._jump_relative(1))
        toolbar.addWidget(self.prev_btn)
        toolbar.addWidget(self.next_btn)

        toolbar.addStretch(1)
        self.progress_label = QLabel("")
        toolbar.addWidget(self.progress_label)
        layout.addLayout(toolbar)

        # ---- 图片滚动区 ----
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.gallery = QWidget()
        self.gallery_layout = QVBoxLayout(self.gallery)
        self.gallery_layout.setContentsMargins(0, 0, 0, 0)
        self.gallery_layout.setSpacing(4)
        self.gallery_layout.setAlignment(Qt.AlignHCenter)
        self.scroll.setWidget(self.gallery)
        layout.addWidget(self.scroll, stretch=1)

        self._apply_mode()

    # ------------------------------------------------------------------ #
    def load(self, source, detail: Detail, start_chapter_url: str = "") -> None:
        self._source = source
        self._detail = detail
        self._chapters = detail.chapters
        idx = 0
        if start_chapter_url:
            for i, ch in enumerate(detail.chapters):
                if ch.url == start_chapter_url:
                    idx = i
                    break
        self._load_episode(idx)

    def _load_episode(self, idx: int) -> None:
        if self._source is None or not (0 <= idx < len(self._chapters)):
            return
        self._current_idx = idx
        ch = self._chapters[idx]
        self.progress_label.setText(f"第{idx+1}/{len(self._chapters)}话 · 加载中")
        self._clear_images()
        # 后台加载图片 URL
        from PySide6.QtCore import QThreadPool

        task = _LoadComicTask(self._content, self._source, ch)
        task.signals.finished.connect(self._on_images_loaded)
        self._comic_task = task  # 持有引用，防止被 GC
        QThreadPool.globalInstance().start(task)
        self.chapter_changed.emit((self._detail, ch.title))

    def _on_images_loaded(self, ch, images, err) -> None:
        if err:
            self.progress_label.setText(f"加载失败：{err}")
            return
        self._images = images
        self.progress_label.setText(f"第{self._current_idx+1}/{len(self._chapters)}话 · {len(images)}张")
        self._render_images()

    def _render_images(self) -> None:
        self._clear_images()
        for url in self._images:
            lbl = _ComicImageLabel(url)
            lbl.load()
            self.gallery_layout.addWidget(lbl)

    def _clear_images(self) -> None:
        while self.gallery_layout.count():
            child = self.gallery_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    # ------------------------------------------------------------------ #
    def _toggle_mode(self) -> None:
        self._mode = "flip" if self._mode == "gallery" else "gallery"
        self.mode_btn.setText("切换横向模式" if self._mode == "gallery" else "切换画廊模式")
        self._apply_mode()

    def _apply_mode(self) -> None:
        if self._mode == "flip":
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
            self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        else:
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    def _jump_relative(self, delta: int) -> None:
        nxt = self._current_idx + delta
        if 0 <= nxt < len(self._chapters):
            self._load_episode(nxt)

    def wheelEvent(self, event) -> None:  # noqa: N802
        # Ctrl+滚轮缩放
        if event.modifiers() & Qt.ControlModifier:
            delta = 1.1 if event.angleDelta().y() > 0 else 0.9
            self._zoom *= delta
            self._zoom = max(0.3, min(3.0, self._zoom))
            self._apply_zoom()
            event.accept()
        else:
            super().wheelEvent(event)

    def _apply_zoom(self) -> None:
        self.gallery.setFixedWidth(int(self._current_base_width() * self._zoom))

    def _current_base_width(self) -> int:
        return max(300, self.width() - 40)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_zoom()


class _ComicImageLabel(QLabel):
    """漫画单页图片（异步加载自适应）。"""

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.url = url
        self.setAlignment(Qt.AlignCenter)
        self._loading = True
        self.setText("加载中...")
        self.setMinimumWidth(200)
        self.setStyleSheet("border: 1px solid palette(mid); border-radius: 4px; padding: 4px;")
        self.setCursor(Qt.OpenHandCursor)

    def load(self) -> None:
        from gui.components.cover_loader import CoverLoader

        CoverLoader.instance().load(self.url, self._on_image)

    def _on_image(self, pixmap) -> None:
        if pixmap is None:
            self.setText("图片加载失败\n（可能需登录或已失效）")
            return
        # 缩放适应宽度
        avail = self.width() or 600
        scaled = pixmap.scaledToWidth(
            avail, Qt.SmoothTransformation
        ) if pixmap.width() > avail else pixmap
        self.setPixmap(scaled)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(), self.sizePolicy().verticalPolicy())
        if scaled.height() > 2000:
            scaled = pixmap.scaledToWidth(
                avail, Qt.SmoothTransformation
            )
            self.setPixmap(scaled)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            # 点击放大查看（简化：切换为原始大小）
            pass
        super().mousePressEvent(event)


class _ComicSignals(QObject):
    """漫画加载信号。"""
    finished = Signal(object, object, object)  # (chapter, images, err)


class _LoadComicTask(QRunnable):
    """后台加载漫画话图片 URL。"""

    def __init__(self, content, source, chapter):
        super().__init__()
        self.signals = _ComicSignals()
        self._content = content
        self._source = source
        self._chapter = chapter

    def run(self) -> None:
        images, err = [], None
        try:
            images = self._content.fetch_comic_pages(self._source, self._chapter.url)
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(self._chapter, images, err)
        except RuntimeError:
            pass
