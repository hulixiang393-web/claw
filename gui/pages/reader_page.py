"""阅读器主页面（ReaderPage）。

单 Tab 复用：打开一部作品，按 content_type 切换小说/漫画/视频视图。
对应 ui-reader.md。从发现/搜索详情抽屉「开始阅读」进入。

用法（App 层）：
    reader.open(source_id, book_url, content_type, start_chapter_url="")
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThreadPool, QRunnable, QObject, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from framework.content import Content, Detail
from framework.source_manager import SourceManager

from .reader.novel_view import NovelView
from .reader.comic_view import ComicView
from .reader.video_view import VideoView
from .reader.epub_view import EpubView
from .base_page import BasePage


class ReaderPage(BasePage):
    def __init__(
        self,
        source_manager: SourceManager,
        content: Content,
        reading_progress=None,
        parent=None,
    ):
        super().__init__(parent)
        self._manager = source_manager
        self._content = content
        self._reading_progress = reading_progress
        self._current_source_id = None
        self._current_book_url = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- 作品信息条 ----
        info = QHBoxLayout()
        self.title_label = QLabel("未打开作品")
        self.title_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        info.addWidget(self.title_label)
        self.source_label = QLabel("")
        self.source_label.setStyleSheet("color: palette(dark);")
        info.addWidget(self.source_label)
        info.addStretch(1)
        self.open_btn = QPushButton("打开源详情")
        self.open_btn.clicked.connect(self._open_source_page)
        info.addWidget(self.open_btn)
        self.epub_btn = QPushButton("打开本地 epub")
        self.epub_btn.clicked.connect(self._pick_epub)
        info.addWidget(self.epub_btn)
        layout.addLayout(info)

        # ---- 四视图切换 ----
        self.stack = QStackedWidget()
        self.novel_view = NovelView(content)
        self.comic_view = ComicView(content)
        self.video_view = VideoView(content)
        self.epub_view = EpubView()
        self.stack.addWidget(self.novel_view)
        self.stack.addWidget(self.comic_view)
        self.stack.addWidget(self.video_view)
        self.stack.addWidget(self.epub_view)
        layout.addWidget(self.stack, stretch=1)

        # ---- 阅读进度记忆：换章/换集时自动记录 ----
        if reading_progress is not None:
            self.novel_view.chapter_changed.connect(self._on_progress_signal)
            self.comic_view.chapter_changed.connect(self._on_progress_signal)
            self.video_view.episode_changed.connect(self._on_progress_signal)
            # epub 本地阅读：用文件路径作 key 续读
            self.epub_view.chapter_changed.connect(self._on_progress_signal)

    def _on_progress_signal(self, payload) -> None:
        """记录阅读进度（换章/换集触发）。payload=(detail, title, url)。

        epub 本地书：payload=(epub_path, chapter_title)，用文件路径作 key。
        """
        if self._reading_progress is None or not isinstance(payload, (tuple, list)):
            return
        detail, title = payload[0], payload[1]
        url = payload[2] if len(payload) > 2 else ""
        if detail is None:
            return
        try:
            # epub 本地文件：payload[0] 是路径字符串，用路径作 book_url
            if isinstance(detail, str):
                self._reading_progress.save(
                    "", detail, "epub", detail, title or ""
                )
                return
            self._reading_progress.save(
                detail.source_id,
                detail.url,
                detail.content_type,
                url,
                title,
            )
        except Exception:
            pass  # 记忆失败不影响阅读

    # ------------------------------------------------------------------ #
    def open(self, source_id: str, book_url: str, content_type: str, start_chapter_url: str = "") -> None:
        """打开一部作品。"""
        try:
            source = self._manager.get(source_id)
        except Exception:
            self.title_label.setText(f"源不存在：{source_id}")
            return
        self._current_source_id = source_id
        self._current_book_url = book_url
        self.title_label.setText(f"加载中...")
        self.source_label.setText(source.source_name)

        # 后台拉详情（信号跨线程安全）
        from PySide6.QtCore import QThreadPool

        task = _LoadDetailTask(
            self._content, source, book_url, content_type, start_chapter_url
        )
        task.signals.finished.connect(self._on_detail)
        self._detail_task = task  # 持有引用，防止被 GC
        QThreadPool.globalInstance().start(task)

    def _on_detail(self, detail, err, content_type: str, start_chapter_url: str) -> None:
        if err or detail is None:
            self.title_label.setText(f"加载失败：{err}")
            return
        self.title_label.setText(detail.title or "无标题")
        # 按类型切视图
        if content_type == "novel":
            self.stack.setCurrentWidget(self.novel_view)
            self.novel_view.load(self._manager.get(self._current_source_id), detail, start_chapter_url)
        elif content_type == "comic":
            self.stack.setCurrentWidget(self.comic_view)
            self.comic_view.load(self._manager.get(self._current_source_id), detail, start_chapter_url)
        else:
            self.stack.setCurrentWidget(self.video_view)
            self.video_view.load(self._manager.get(self._current_source_id), detail, start_chapter_url)

    def _open_source_page(self) -> None:
        import webbrowser

        if self._current_book_url:
            webbrowser.open(self._current_book_url)

    def _pick_epub(self) -> None:
        """文件选择：打开本地 epub。"""
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "打开 epub 电子书", "", "EPUB 文件 (*.epub)"
        )
        if path:
            self.open_epub(path)

    def open_epub(self, path: str) -> None:
        """打开本地 epub（独立 epub 阅读器）。"""
        self._current_source_id = ""
        self._current_book_url = ""
        self.title_label.setText("epub 阅读")
        self.source_label.setText(path)
        self.stack.setCurrentWidget(self.epub_view)
        if self.epub_view.open(path):
            self.title_label.setText(self.epub_view._chapters[0].title if self.epub_view._chapters else "epub")
        else:
            self.title_label.setText("epub 打开失败")

    def refresh(self) -> None:
        pass


class _DetailSignals(QObject):
    """详情加载信号。"""
    finished = Signal(object, object, object, object)  # (detail, err, content_type, start_url)


class _LoadDetailTask(QRunnable):
    """后台加载详情（QRunnable）。"""

    def __init__(self, content, source, url, content_type, start_url):
        super().__init__()
        self.signals = _DetailSignals()
        self._content = content
        self._source = source
        self._url = url
        self._content_type = content_type
        self._start_url = start_url

    def run(self) -> None:
        detail, err = None, None
        try:
            detail = self._content.fetch_detail(self._source, self._url)
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(
                detail, err, self._content_type, self._start_url
            )
        except RuntimeError:
            pass
