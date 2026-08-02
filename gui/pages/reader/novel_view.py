"""小说阅读视图（NovelView）。

对应 ui-reader.md 小说功能点：
- 正文排版区（字号可调）
- 目录侧栏（可折叠，点击跳章）
- 上/下一章导航
- 章节预加载 + 续读记忆
- 正文解密（调用内核，混淆正文解码）
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QThreadPool, QRunnable, QObject
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from framework.content import Content, Detail


class NovelView(QWidget):
    """小说正文阅读视图。"""

    chapter_changed = Signal(object)  # 发出 (detail, chapter_title) 供续读

    def __init__(self, content: Content, parent=None):
        super().__init__(parent)
        self._content = content
        self._source = None
        self._detail: Detail | None = None
        self._chapters = []
        self._current_idx = -1
        self._font_size = 17

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- 工具栏 ----
        toolbar = QHBoxLayout()
        self.toc_btn = QToolButton()
        self.toc_btn.setText("☰ 目录")
        self.toc_btn.clicked.connect(self._toggle_toc)
        toolbar.addWidget(self.toc_btn)

        self.font_down = QPushButton("A-")
        self.font_up = QPushButton("A+")
        self.font_down.setFixedWidth(40)
        self.font_up.setFixedWidth(40)
        self.font_down.clicked.connect(lambda: self._adjust_font(-1))
        self.font_up.clicked.connect(lambda: self._adjust_font(1))
        toolbar.addWidget(self.font_down)
        toolbar.addWidget(self.font_up)

        toolbar.addStretch(1)
        self.progress_label = QLabel("")
        toolbar.addWidget(self.progress_label)
        layout.addLayout(toolbar)

        # ---- 目录 + 正文 ----
        body = QHBoxLayout()
        body.setSpacing(8)

        self.toc_list = QListWidget()
        self.toc_list.setFixedWidth(180)
        self.toc_list.itemClicked.connect(self._on_toc_clicked)
        self.toc_list.setVisible(False)
        body.addWidget(self.toc_list)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.scroll.setWidget(self.text)
        body.addWidget(self.scroll, stretch=1)
        layout.addLayout(body, stretch=1)

        # ---- 底部导航 ----
        nav = QHBoxLayout()
        self.prev_btn = QPushButton("← 上一章")
        self.next_btn = QPushButton("下一章 →")
        self.prev_btn.clicked.connect(lambda: self._jump_relative(-1))
        self.next_btn.clicked.connect(lambda: self._jump_relative(1))
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.next_btn)
        layout.addLayout(nav)

        self._apply_font()

    # ------------------------------------------------------------------ #
    def load(self, source, detail: Detail, start_chapter_url: str = "") -> None:
        """加载小说：设置目录 + 跳到指定章（或续读）。"""
        self._source = source
        self._detail = detail
        self._chapters = detail.chapters
        self._populate_toc()

        # 定位起始章
        idx = 0
        if start_chapter_url:
            for i, ch in enumerate(detail.chapters):
                if ch.url == start_chapter_url:
                    idx = i
                    break
        self._load_chapter(idx)

    def _populate_toc(self) -> None:
        self.toc_list.clear()
        for i, ch in enumerate(self._chapters):
            item = QListWidgetItem(ch.title or f"第{i+1}章")
            item.setData(Qt.UserRole, i)
            self.toc_list.addItem(item)

    def _load_chapter(self, idx: int) -> None:
        """加载指定章节正文（后台线程）。"""
        if self._source is None or not (0 <= idx < len(self._chapters)):
            return
        self._current_idx = idx
        ch = self._chapters[idx]

        # 已缓存 → 直接显示
        if hasattr(ch, "_cached_text") and ch._cached_text:
            self.text.setText(ch._cached_text)
            self.scroll.verticalScrollBar().setValue(0)
            self._update_progress()
            self.chapter_changed.emit((self._detail, ch.title))
            return

        self.text.setText("正在加载...")
        # 后台加载（信号跨线程安全）
        from PySide6.QtCore import QThreadPool

        task = _LoadChapterTask(self._content, self._source, ch)
        task.signals.finished.connect(self._on_chapter_loaded)
        self._chapter_task = task  # 持有引用，防止被 GC
        QThreadPool.globalInstance().start(task)
        self.chapter_changed.emit((self._detail, ch.title))

    def _on_chapter_loaded(self, ch, text, err) -> None:
        if err:
            self.text.setText(f"加载失败：{err}")
            return
        ch._cached_text = text
        if self._current_idx >= 0 and ch.url == self._chapters[self._current_idx].url:
            self.text.setText(text)
            self.scroll.verticalScrollBar().setValue(0)
            self._update_progress()

    # ------------------------------------------------------------------ #
    def _toggle_toc(self) -> None:
        self.toc_list.setVisible(not self.toc_list.isVisible())

    def _on_toc_clicked(self, item) -> None:
        idx = item.data(Qt.UserRole)
        self._load_chapter(idx)

    def _jump_relative(self, delta: int) -> None:
        nxt = self._current_idx + delta
        if 0 <= nxt < len(self._chapters):
            self._load_chapter(nxt)

    def _adjust_font(self, delta: int) -> None:
        self._font_size += delta
        self._font_size = max(12, min(28, self._font_size))
        self._apply_font()

    def _apply_font(self) -> None:
        self.text.setStyleSheet(
            f"font-size: {self._font_size}px; line-height: 1.8; padding: 8px 12px;"
        )

    def _update_progress(self) -> None:
        total = len(self._chapters)
        self.progress_label.setText(f"第{self._current_idx + 1}/{total}章")


class _LoadChapterSignals(QObject):
    """章节加载信号。"""

    finished = Signal(object, object, object)  # (chapter, text, err)


class _LoadChapterTask(QRunnable):
    """后台加载章节正文（信号跨线程安全）。"""

    def __init__(self, content, source, chapter):
        super().__init__()
        self.signals = _LoadChapterSignals()
        self._content = content
        self._source = source
        self._chapter = chapter

    def run(self) -> None:
        text, err = "", None
        try:
            text = self._content.fetch_chapter(self._source, self._chapter.url)
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(self._chapter, text, err)
        except RuntimeError:
            pass  # 页面已销毁
