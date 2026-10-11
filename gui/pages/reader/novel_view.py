"""小说阅读视图（NovelView）。

对应 ui-reader.md 小说功能点：
- 正文排版区（字号可调）
- 目录侧栏（可折叠，点击跳章）
- 上/下一章导航
- 章节预加载 + 续读记忆
- 正文解密（调用内核，混淆正文解码）
"""

from __future__ import annotations

from html import escape
import hashlib
import re
import time
import webbrowser
from urllib.parse import urljoin, urlsplit, urlunsplit

from PySide6.QtCore import Qt, QTimer, Signal, QThreadPool, QRunnable, QObject, QUrl
from PySide6.QtGui import QKeySequence, QShortcut, QTextDocument
from PySide6.QtWidgets import (
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QTextBrowser,
    QListWidget,
    QListWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QSizePolicy,
    QPushButton,
    QScrollArea,
    QSlider,
    QToolButton,
    QWidget,
)

from framework.content import Content, Detail

from gui.pages.reader.auto_scroll import AutoScrollMixin


from gui.pages.reader.novel_tasks import _LoadChapterSignals, _LoadChapterTask


class NovelView(AutoScrollMixin, QWidget):
    """小说正文阅读视图。"""

    chapter_changed = Signal(object)  # 发出 (detail, chapter_title) 供续读
    position_changed = Signal(object)  # (detail, title, url, position, page) 章内位置续读
    fullscreen_requested = Signal()  # 工具条 ⛶ → ReaderPage 切主窗全屏
    background_cycle_requested = Signal()  # 「背景」按钮 → ReaderPage 循环切换护眼背景色

    def __init__(self, content: Content, font_scale: float = 1.0, parent=None):
        super().__init__(parent)
        self._content = content
        self._source = None
        self._detail: Detail | None = None
        self._chapters = []
        self._current_idx = -1
        self._font_delta = 0
        self._base_font = self._clamp_font(round(17 * float(font_scale or 1.0)))
        self._reading_bg = ""  # 阅读区独立背景色（ui-reader #12，空=透明跟随主题）
        self._reading_fg = ""  # 夜间黑等深色背景下的正文前景色（空=跟随主题）
        self._auto_loading = False  # 翻页边界跳章锁，防重复触发
        self._auto_prev_loading = False  # 向上翻页边界跳章锁，防重复触发
        self._prefetch_idx = -2  # 正在后台预加载的章节 idx（<0 表示空闲）
        self._prefetch_ahead = 1
        self._prefetch_behind = 0
        self._load_generation = 0
        self._image_generation = 0
        self._show_images = True
        self._cached_image_urls = []
        self._bookstack_image_pixmaps = {}
        self._current_chapter_url = ""
        self._toc_generation = 0
        self._toc_next_index = 0
        self._toc_batch_size = 200
        self._toc_stack = []
        self._toc_items = []
        self._toc_timer = QTimer(self)
        self._toc_timer.setInterval(0)
        self._toc_timer.timeout.connect(lambda: self._append_toc_batch(self._toc_generation))
        self._next_prefetch_queue = []
        self._prev_prefetch_queue = []  # 向前缓存队列（串行）
        self._prev_prefetch_idx = -2  # 正在向前预取的章节 idx（<0 表示空闲）
        self._prev_prefetch_task = None  # 持引用防 GC
        self._last_pos_save_ts = 0.0  # 上次章内位置存盘时间戳（节流 1.5s 存一次）
        self._pending_restore = None  # 打开书续读位置 (position, page, location)，首次显示章时定位
        self._restore_generation = 0
        self._restore_cancel_token = 0
        self._auto_scrolling = False  # 自动滚动开关（状态机由 AutoScrollMixin 接管）
        self._auto_pos = 0.0  # 自动滚动记住的位置（浮点，按速度递增，重排不打断）
        # 自动滚动定时器（interval=35ms，高频小步进平滑滚动）
        self._build_auto_scroll_timer()

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

        self.mode_btn = QPushButton("翻页模式")
        self.mode_btn.setFixedWidth(90)
        self.mode_btn.clicked.connect(self._toggle_mode)
        toolbar.addWidget(self.mode_btn)

        self.bg_btn = QPushButton("背景")
        self.bg_btn.setFixedWidth(50)
        self.bg_btn.setToolTip("切换阅读背景色（白/米黄/护眼绿/夜间黑）")
        self.bg_btn.clicked.connect(self.background_cycle_requested.emit)
        toolbar.addWidget(self.bg_btn)

        self.fullscreen_btn = QPushButton("⛶")
        self.fullscreen_btn.setFixedWidth(40)
        self.fullscreen_btn.setToolTip("全屏阅读")
        self.fullscreen_btn.clicked.connect(self.fullscreen_requested.emit)
        toolbar.addWidget(self.fullscreen_btn)

        self.auto_scroll_btn = QPushButton("▶ 自动滚动")
        self.auto_scroll_btn.setFixedWidth(90)
        self.auto_scroll_btn.setToolTip("开启/关闭自动滚动（空格键）")
        self.auto_scroll_btn.clicked.connect(self._toggle_auto_scroll)
        toolbar.addWidget(self.auto_scroll_btn)

        self.auto_scroll_speed_slider = QSlider(Qt.Horizontal)
        self.auto_scroll_speed_slider.setRange(1, 10)
        self.auto_scroll_speed_slider.setValue(3)
        self.auto_scroll_speed_slider.setFixedWidth(100)
        self.auto_scroll_speed_slider.setToolTip("自动滚动速度（1最慢，10最快）")
        self.auto_scroll_speed_slider.setEnabled(False)
        toolbar.addWidget(self.auto_scroll_speed_slider)

        self.auto_scroll_speed_label = QLabel("3")
        self.auto_scroll_speed_label.setFixedWidth(12)
        self.auto_scroll_speed_label.setAlignment(Qt.AlignCenter)
        toolbar.addWidget(self.auto_scroll_speed_label)

        self.auto_scroll_speed_slider.valueChanged.connect(
            lambda v: self.auto_scroll_speed_label.setText(str(v))
        )

        toolbar.addStretch(1)
        self.progress_label = QLabel("")
        toolbar.addWidget(self.progress_label)
        layout.addLayout(toolbar)

        # ---- 目录 + 正文 ----
        body = QHBoxLayout()
        body.setSpacing(8)

        self.toc_list = QListWidget()
        self.toc_list.setFixedWidth(180)
        self.toc_list.setWordWrap(True)  # 长标题换行完整显示，不被截断
        self.toc_list.itemClicked.connect(self._on_toc_clicked)
        self.toc_list.setVisible(False)
        body.addWidget(self.toc_list)

        self.toc_tree = QTreeWidget()
        self.toc_tree.setHeaderHidden(True)
        self.toc_tree.setWordWrap(True)
        self.toc_tree.setMinimumWidth(240)
        self.toc_tree.setMaximumWidth(280)
        self.toc_tree.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.toc_tree.itemClicked.connect(self._on_toc_clicked)
        self.toc_tree.setVisible(False)
        body.addWidget(self.toc_tree)

        # 正文区：滚动模式 + 翻页模式，用 QStackedWidget 切换
        from PySide6.QtWidgets import QStackedWidget, QTextEdit

        self.body_stack = QStackedWidget()

        # -- 滚动模式（默认）--
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_body = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_body)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_layout.setSpacing(8)
        self.text = QLabel()
        self.text.setObjectName("readerBody")
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._rich_body = QTextBrowser()
        self._rich_body.setObjectName("bookstackBody")
        self._rich_body.setMinimumWidth(0)
        self._rich_body.setMaximumWidth(960)
        self._rich_body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._rich_body.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._rich_body.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._rich_body.setOpenExternalLinks(False)
        self._rich_body.setOpenLinks(False)
        self._rich_body.anchorClicked.connect(self._on_rich_anchor_clicked)
        self._rich_body.setReadOnly(True)
        self._rich_body.setVisible(False)
        self.scroll_layout.addWidget(self.text)
        self.scroll_layout.addWidget(self._rich_body)
        self.scroll_layout.setAlignment(self._rich_body, Qt.AlignHCenter)
        self.scroll_layout.addStretch(1)
        self.scroll.setWidget(self.scroll_body)
        self.body_stack.addWidget(self.scroll)

        # -- 翻页模式 --
        self.pager_widget = QWidget()
        self.pager_layout = QVBoxLayout(self.pager_widget)
        self.pager_layout.setContentsMargins(0, 0, 0, 0)
        self.pager_layout.setSpacing(6)
        self.paged_scroll = QScrollArea()
        self.paged_scroll.setWidgetResizable(True)
        self.paged_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.paged_label = QLabel()
        self.paged_label.setObjectName("readerBody")
        self.paged_label.setWordWrap(True)
        self.paged_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.paged_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.paged_scroll.setWidget(self.paged_label)
        self.pager_layout.addWidget(self.paged_scroll, stretch=1)
        # 翻页导航
        self.pager_nav = QHBoxLayout()
        self.pager_prev = QPushButton("← 上一页")
        self.pager_next = QPushButton("下一页 →")
        self.pager_prev.clicked.connect(lambda: self._pager_turn(-1))
        self.pager_next.clicked.connect(lambda: self._pager_turn(1))
        self.pager_nav.addWidget(self.pager_prev)
        self.pager_nav.addStretch(1)
        self.pager_indicator = QLabel("")
        self.pager_nav.addWidget(self.pager_indicator)
        self.pager_nav.addStretch(1)
        self.pager_nav.addWidget(self.pager_next)
        self.pager_layout.addLayout(self.pager_nav)
        self.body_stack.addWidget(self.pager_widget)

        body.addWidget(self.body_stack, stretch=1)
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

        self._mode = "scroll"  # scroll / pager
        self._pages = [""]
        self._page_count = 1
        self._current_page = 0
        self._paged_full_text = ""
        self._apply_font()
        # 章内位置记忆：滚动节流存盘（精准到页）
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll_position)

        # ---- 键盘导航 ----
        self.setFocusPolicy(Qt.StrongFocus)

        # 鼠标侧键翻章 + Ctrl+滚轮字号：应用级事件过滤器。正文 QLabel 带
        # TextSelectableByMouse 会吞掉子控件级鼠标事件，装到视图自身覆盖不到
        # 正文——应用级过滤器可捕获滚动区/正文/目录等所有本视图内子控件事件。
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        # 自动滚动定时器 + 快捷键
        QShortcut(QKeySequence(Qt.Key_Space), self).activated.connect(self._toggle_auto_scroll)
        # 用户手动拖动滚动条时停止自动滚动（否则 tick 会拉回原位，无法拖动）
        self.scroll.verticalScrollBar().sliderPressed.connect(self._on_scrollbar_user_interaction)

    # ------------------------------------------------------------------ #
    def load(
        self,
        source,
        detail: Detail,
        start_chapter_url: str = "",
        restore_position: float | None = None,
        restore_page: int | None = None,
        restore_location: dict | None = None,
    ) -> None:
        """加载小说：设置目录 + 跳到指定章（或续读），并定位到章内位置。

        restore_position：0~1 滚动比例；restore_page：翻页模式页索引（0 基）。
        二者在首次显示章正文后应用（_display_chapter 读取 _pending_restore）。
        """
        self._source = source
        self._detail = detail
        self._restore_generation += 1
        self._restore_cancel_token += 1
        self._load_generation += 1
        self._chapters = detail.chapters
        # 换书：无条件清零续读位置——旧书若章节还在后台加载，本次 load() 的
        # 首章显示会消费到「上一本书残留的 _pending_restore」，把新书滚动条
        # 错滚到旧书位置（跨书串位置的 bug 根因）。清零后再按新书记录设置。
        self._pending_restore = None
        self._prev_prefetch_queue = []  # 换书清空向前缓存队列（旧队列指向旧书章节）
        self._prev_prefetch_idx = -2
        self._next_prefetch_queue = []
        self._prefetch_idx = -2
        self._populate_toc()
        if restore_position is not None or restore_page is not None or restore_location:
            self._pending_restore = (
                (restore_position, restore_page, restore_location)
                if restore_location is not None
                else (restore_position, restore_page)
            )

        # 定位起始章
        idx = 0
        if start_chapter_url:
            for i, ch in enumerate(detail.chapters):
                if ch.url == start_chapter_url:
                    idx = i
                    break
        self._load_chapter(idx)

    def _populate_toc(self) -> None:
        self._toc_generation += 1
        generation = self._toc_generation
        self._toc_timer.stop()
        self.toc_list.clear()
        self.toc_tree.clear()
        self._toc_stack = []
        self._toc_items = []
        self._toc_next_index = 0
        self.toc_list.setVisible(False)
        self.toc_tree.setVisible(False)
        self._append_toc_batch(generation)

    def _append_toc_batch(self, generation: int) -> None:
        if generation != self._toc_generation:
            return
        start = self._toc_next_index
        end = min(start + self._toc_batch_size, len(self._chapters))
        is_bookstack = getattr(self._source, "source_id", "") == "bookstack"
        for i in range(start, end):
            ch = self._chapters[i]
            depth = max(0, int(getattr(ch, "depth", 0) or 0))
            title = ch.title or f"第{i+1}章"
            if is_bookstack:
                while len(self._toc_stack) > depth:
                    self._toc_stack.pop()
                parent = self._toc_stack[-1] if self._toc_stack and depth else None
                item = QTreeWidgetItem([title])
                item.setData(0, Qt.UserRole, i)
                if parent is None:
                    self.toc_tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                self._toc_stack.append(item)
                self._toc_items.append(item)
            else:
                item = QListWidgetItem(title)
                item.setData(Qt.UserRole, i)
                item.setData(Qt.UserRole + 1, depth)
                self.toc_list.addItem(item)
        self._toc_next_index = end
        if end < len(self._chapters):
            self._toc_timer.start()
        else:
            self._toc_timer.stop()
            if getattr(self._source, "source_id", "") == "bookstack":
                if 0 <= self._current_idx < len(self._toc_items):
                    self.toc_tree.setCurrentItem(self._toc_items[self._current_idx])
            elif 0 <= self._current_idx < self.toc_list.count():
                self.toc_list.setCurrentRow(self._current_idx)

    def _load_chapter(self, idx: int, scroll_to_end: bool = False) -> None:
        """加载指定章节正文（后台线程）。

        scroll_to_end=True：加载后定位到章尾（翻页边界向上跳章时用）；
        滚动模式滚到底，翻页模式跳到最后一页。
        """
        if self._source is None or not (0 <= idx < len(self._chapters)):
            return
        self._stop_auto_scroll()
        self._image_generation += 1
        self._clear_images()
        self._current_idx = idx
        if getattr(self._source, "source_id", "") == "bookstack":
            if 0 <= idx < len(self._toc_items):
                self.toc_tree.setCurrentItem(self._toc_items[idx])
        elif idx < self.toc_list.count():
            self.toc_list.setCurrentRow(idx)
        ch = self._chapters[idx]

        self._scroll_on_load = getattr(self, "_scroll_on_load", 0)  # 0=顶部; 1=底部
        self._scroll_on_load = 1 if scroll_to_end else 0

        # 已缓存 → 直接显示
        if hasattr(ch, "_cached_text") and ch._cached_text:
            if self._show_images and not getattr(ch, "_images_attempted", False):
                self.text.setText("正在加载...")
            else:
                self._auto_loading = False
                self._auto_prev_loading = False
                self._display_chapter(ch, ch._cached_text, scroll_to_end)
                self._update_progress()
                self.chapter_changed.emit((self._detail, ch.title, ch.url))
                return

        self.text.setText("正在加载...")
        # 后台加载（信号跨线程安全）
        from PySide6.QtCore import QThreadPool

        task = _LoadChapterTask(self._content, self._source, ch, load_images=self._show_images)
        generation = self._load_generation
        task.signals.finished.connect(
            lambda loaded_ch, text, err, g=generation: self._on_chapter_loaded(loaded_ch, text, err, g)
        )
        self._chapter_task = task  # 持有引用，防止被 GC
        QThreadPool.globalInstance().start(task)
        self.chapter_changed.emit((self._detail, ch.title, ch.url))

    def _display_chapter(self, ch, text: str, scroll_to_end: bool = False) -> None:
        """正文前加章节编号行（如「第12章」），不含标题文字。"""
        from framework.content import chapter_label
        label = chapter_label(ch.title) or f"第{self._current_idx + 1}章"
        full = f"【{label}】\n\n{text}"
        self._current_chapter_url = ch.url
        rich = getattr(ch, "_cached_rich_content", None)
        self._clear_images()
        self._bookstack_image_pixmaps.clear()
        if getattr(self._source, "source_id", "") == "bookstack":
            self._rich_body.setMaximumWidth(960)
        else:
            self.scroll_body.setStyleSheet("")
            self._rich_body.setStyleSheet("")
            self._rich_body.setMinimumHeight(0)
            self._rich_body.setMaximumWidth(16777215)
        if rich is not None and getattr(self._source, "source_id", "") == "bookstack":
            render_generation = self._load_generation
            self.text.setText("正在渲染正文...")
            self.text.setVisible(True)
            self._rich_body.setVisible(False)
            QTimer.singleShot(
                0,
                lambda c=ch, l=label, r=rich, g=render_generation: self._render_bookstack_body(c, l, r, g),
            )
        else:
            self.text.setText(full)
            self.text.setMinimumHeight(self.text.sizeHint().height())
            self._rich_body.setVisible(False)
        entries = getattr(ch, "_cached_image_urls", []) or []
        if rich is not None:
            entries = [{"type": "image", "url": url} for url in getattr(rich, "image_urls", [])]
        fragments = []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("type") in ("text", "image"):
                fragments.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("url"), str):
                fragments.append({"type": "image", "url": entry["url"]})
            elif isinstance(entry, str) and entry:
                fragments.append({"type": "image", "url": entry})
        if self._mode == "scroll" and self._show_images and fragments:
            self.text.setVisible(False)
            generation = self._image_generation
            for fragment in fragments:
                if fragment.get("type") == "text":
                    segment = QLabel(str(fragment.get("text") or ""))
                    segment.setObjectName("readerBody")
                    segment.setWordWrap(True)
                    segment.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    segment.setStyleSheet(self._plain_body_css())
                    segment.setMaximumWidth(960)
                    self.scroll_layout.insertWidget(
                        self.scroll_layout.count() - 1, segment, alignment=Qt.AlignHCenter
                    )
                else:
                    image_url = fragment.get("url")
                    if isinstance(image_url, str) and image_url:
                        self._load_image(image_url, generation, self._load_generation)
        else:
            self.text.setVisible(True)
        # 翻页视图同步（分页结果按章缓存，避免反复全量切分长文）
        self._paged_full_text = full
        self.scroll_body.adjustSize()
        self._repaginate()
        if scroll_to_end:
            self._pager_show_page(self._page_count - 1)  # 定位到本章最后一页
        else:
            self._pager_show_page(0)
        # 滚动模式定位（程序化滚动，blockSignals 避免误触发滚动存盘）
        vbar = self.scroll.verticalScrollBar()
        if scroll_to_end:
            # 等 layout 完成后再滚到底（QTimer 后置，确保 maximum 已更新）
            vbar.blockSignals(True)
            vbar.setValue(0)
            vbar.blockSignals(False)
            QTimer.singleShot(0, self._scroll_to_bottom_silently)
        else:
            vbar.blockSignals(True)
            vbar.setValue(0)
            vbar.blockSignals(False)
        if getattr(self._source, "source_id", "") != "bookstack":
            self._prefetch_next(self._current_idx)
            self._prefetch_prev(self._current_idx)
        # 续读定位：首章显示后恢复到上次的章内位置（页索引/滚动比例）
        if self._pending_restore is not None:
            pending = self._pending_restore
            pos, page = pending[:2]
            location = pending[2] if len(pending) > 2 else None
            self._pending_restore = None
            book = self._detail
            generation = self._restore_generation
            if page is not None:
                self._current_page = page
                self._pager_show_page(page)
            QTimer.singleShot(
                0, lambda b=book, g=generation, p=pos, loc=location: self._restore_location_if_book(p, loc, b, g)
            )
            self._last_pos_save_ts = 0.0
            QTimer.singleShot(0, self._emit_position)
        # 更新自动滚动滑块状态（根据模式和滚动范围）
        QTimer.singleShot(0, self._update_auto_scroll_slider_state)

    def _render_bookstack_body(self, chapter, label, rich, generation) -> None:
        if (
            generation != self._load_generation
            or not (0 <= self._current_idx < len(self._chapters))
            or chapter is not self._chapters[self._current_idx]
            or getattr(self._source, "source_id", "") != "bookstack"
        ):
            return
        self._rich_body.setUpdatesEnabled(False)
        try:
            self._rich_body.setHtml(self._bookstack_html(label, rich.body_html))
            self._apply_bookstack_style()
        finally:
            self._rich_body.setUpdatesEnabled(True)
        self.text.setVisible(False)
        self._rich_body.setVisible(True)
        self._resize_bookstack_body()
        self.scroll_body.adjustSize()

    def _bookstack_html(self, label: str, body_html: str) -> str:
        source_name = escape(str(getattr(self._source, "source_name", "BookStack") or "BookStack"))
        detail_title = escape(str(getattr(self._detail, "title", "") or "当前文档"))
        chapter_title = escape(str(label or "当前章节"))
        return (
            '<div class="bookstack-reader">'
            f'<div class="bookstack-title">{chapter_title}</div>'
            f'<div class="bookstack-meta">来源：{source_name}&nbsp;&middot;&nbsp;{detail_title}</div>'
            f'<div class="markdown-body">{body_html}</div>'
            '</div>'
        )

    def _bookstack_css(self) -> str:
        size = self._clamp_font(self._base_font + self._font_delta)
        return f"""
            body {{ margin: 0; background: transparent; color: #222222; }}
            .bookstack-reader {{ color: #222222; font-family: \"Segoe UI\", \"Microsoft YaHei\", sans-serif; }}
            .bookstack-title {{ margin: 0 0 18px; color: #262626; font-size: 30px; font-weight: 700; line-height: 1.35; text-align: center; }}
            .bookstack-meta {{ margin: 0 0 28px; padding: 0 0 14px; border-bottom: 1px dashed #d7d7d7; color: #666666; font-size: 12px; line-height: 1.5; text-align: center; }}
            .markdown-body {{ color: #222222; font-size: {size}px; line-height: 1.8; }}
            .markdown-body h1, .markdown-body h2, .markdown-body h3, .markdown-body h4 {{ color: #1f1f1f; font-weight: 700; line-height: 1.4; }}
            .markdown-body h1 {{ margin: 30px 0 18px; font-size: 26px; }}
            .markdown-body h2 {{ margin: 28px 0 16px; font-size: 22px; }}
            .markdown-body h3 {{ margin: 24px 0 12px; font-size: 19px; }}
            .markdown-body h4 {{ margin: 20px 0 10px; font-size: 17px; }}
            .markdown-body p {{ margin: 0 0 16px; color: #222222; text-align: justify; }}
            .markdown-body li {{ margin: 0 0 7px; color: #222222; }}
            .markdown-body a {{ color: #315f9b; text-decoration: underline; }}
            .markdown-body blockquote {{ margin: 18px 0; padding: 10px 16px; border-left: 4px solid #b8c2d1; background: #f6f7f8; color: #3b3b3b; }}
            .markdown-body pre {{ margin: 18px 0; padding: 14px 16px; background: #f3f4f5; color: #202020; white-space: pre-wrap; }}
            .markdown-body code {{ color: #242424; background: #f1f2f3; }}
            .markdown-body table {{ border-collapse: collapse; margin: 18px 0; }}
            .markdown-body th, .markdown-body td {{ padding: 8px 10px; border: 1px solid #d8d8d8; color: #222222; }}
            .markdown-body img {{ display: block; max-width: 100%; height: auto; margin: 20px auto; border-radius: 4px; }}
        """

    def _apply_bookstack_style(self) -> None:
        if getattr(self._source, "source_id", "") != "bookstack":
            return
        width = self._rich_body.width()
        side_padding = 50 if width >= 760 else 28 if width >= 560 else 16
        outer_bg = self._reading_bg or "#f4f5f7"
        self.scroll_body.setStyleSheet(f"background-color: {outer_bg};")
        self._rich_body.setStyleSheet(
            "QTextBrowser#bookstackBody {"
            "background-color: #ffffff;"
            "color: #222222;"
            "border: 1px solid #e4e4e4;"
            "border-radius: 8px;"
            f"padding: 30px {side_padding}px;"
            "}"
        )
        document = self._rich_body.document()
        document.setDefaultStyleSheet(self._bookstack_css())
        document.setDocumentMargin(0)
        document.adjustSize()

    def _resize_bookstack_body(self) -> None:
        if getattr(self._source, "source_id", "") != "bookstack":
            return
        document = self._rich_body.document()
        document.adjustSize()
        height = max(120, int(document.size().height()) + 60)
        self._rich_body.setMinimumHeight(height)
        self._rich_body.updateGeometry()

    def _bookstack_image_width(self) -> int:
        width = self._rich_body.width()
        side_padding = 50 if width >= 760 else 28 if width >= 560 else 16
        return max(120, width - side_padding * 2 - 2)

    def _apply_bookstack_image_resources(self) -> None:
        if getattr(self._source, "source_id", "") != "bookstack":
            return
        width = self._bookstack_image_width()
        document = self._rich_body.document()
        for url, pixmap in self._bookstack_image_pixmaps.items():
            scaled = pixmap
            if pixmap.width() > width:
                scaled = pixmap.scaledToWidth(width, Qt.SmoothTransformation)
            document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(url), scaled)
        if self._bookstack_image_pixmaps:
            document.markContentsDirty(0, document.characterCount())
            document.adjustSize()
            self._resize_bookstack_body()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if getattr(self._source, "source_id", "") == "bookstack" and self._rich_body.isVisible():
            QTimer.singleShot(0, self._apply_bookstack_style)
            QTimer.singleShot(0, self._apply_bookstack_image_resources)
            QTimer.singleShot(0, self._resize_bookstack_body)

    @staticmethod
    def _normalize_anchor_text(text: str) -> str:
        lines = str(text or "").splitlines()
        if lines and lines[0].strip().startswith("【") and lines[0].strip().endswith("】"):
            lines = lines[1:]
        return re.sub(r"\s+", " ", " ".join(line.strip() for line in lines)).strip()

    @staticmethod
    def _location_matches_chapter(location: dict | None, chapter_url: str) -> bool:
        return bool(isinstance(location, dict) and chapter_url and location.get("chapter_url") == chapter_url)

    @classmethod
    def _build_location_snapshot(cls, text: str, ratio: float, value: int, maximum: int, chapter_url: str = "") -> dict:
        body = cls._normalize_anchor_text(text)
        offset = max(0, min(len(body), int(round(float(ratio or 0) * len(body)))))
        start = max(0, offset - 80)
        anchor = body[start:start + 160]
        return {
            "version": 1,
            "chapter_url": chapter_url,
            "anchor": anchor,
            "anchor_hash": hashlib.sha1(anchor.encode("utf-8")).hexdigest(),
            "offset": offset - start,
            "content_hash": hashlib.sha1(body.encode("utf-8")).hexdigest(),
            "scroll_ratio": round(max(0.0, min(1.0, float(ratio or 0))), 4),
            "scroll_value": int(value),
            "scroll_maximum": int(maximum),
            "text_length": len(body),
        }

    @classmethod
    def _resolve_location_scroll(cls, text: str, location: dict, maximum: int):
        body = cls._normalize_anchor_text(text)
        if not isinstance(location, dict):
            return None
        if location.get("content_hash") == hashlib.sha1(body.encode("utf-8")).hexdigest():
            old_max = int(location.get("scroll_maximum", 0) or 0)
            old_value = int(location.get("scroll_value", 0) or 0)
            if old_max > 0:
                return max(0, min(int(maximum), round(maximum * old_value / old_max)))
        anchor = cls._normalize_anchor_text(location.get("anchor", ""))
        if anchor:
            hits = [m.start() for m in re.finditer(re.escape(anchor), body)]
            if len(hits) == 1:
                offset = hits[0] + int(location.get("offset", 0) or 0)
                return max(0, min(int(maximum), round(maximum * offset / max(1, len(body)))))
        ratio = location.get("scroll_ratio")
        if ratio is not None:
            return max(0, min(int(maximum), round(float(ratio) * maximum)))
        return None

    def _restore_location_if_book(self, pos, location, book, generation, tries=6, cancel_token=None):
        cancel_token = self._restore_cancel_token if cancel_token is None else cancel_token
        if self._detail is not book or self._restore_generation != generation or self._restore_cancel_token != cancel_token:
            return
        vbar = self.scroll.verticalScrollBar()
        chapter_url = self._chapters[self._current_idx].url if 0 <= self._current_idx < len(self._chapters) else ""
        valid_location = location if self._location_matches_chapter(location, chapter_url) else None
        target = self._resolve_location_scroll(self.text.text(), valid_location, vbar.maximum()) if valid_location else None
        if target is None and pos is not None:
            target = round(float(pos) * vbar.maximum())
        if target is not None and vbar.maximum() > 0:
            vbar.setValue(int(target))
        if valid_location is not None and target is not None:
            return
        if tries > 1:
            QTimer.singleShot(
                100,
                lambda: self._restore_location_if_book(
                    pos, location, book, generation, tries - 1, cancel_token
                ),
            )

    def _build_current_location(self):
        pos, _ = self.position_snapshot()
        vbar = self.scroll.verticalScrollBar()
        chapter_url = self._chapters[self._current_idx].url if 0 <= self._current_idx < len(self._chapters) else ""
        return self._build_location_snapshot(self.text.text(), pos, vbar.value(), vbar.maximum(), chapter_url)

    def _restore_scroll(self, pos: float) -> None:
        """按 0~1 比例恢复滚动位置（打开书续读）。"""
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() > 0:
            vbar.setValue(int(pos * vbar.maximum()))

    def _restore_scroll_if_book(self, pos: float, book) -> None:
        """恢复滚动位置，但仅当当前仍是同一本书（防换书后旧恢复回调串位置）。"""
        if self._detail is not book:
            return  # 用户已换书，旧书恢复定位作废
        self._restore_scroll(pos)

    def _scroll_to_bottom_silently(self) -> None:
        """无触发地滚到底（blockSignals 包住，防滚动事件重入）。"""
        vbar = self.scroll.verticalScrollBar()
        vbar.blockSignals(True)
        vbar.setValue(vbar.maximum())
        vbar.blockSignals(False)

    def _clear_images(self) -> None:
        self._bookstack_image_pixmaps.clear()
        keep = {self.text, self._rich_body}
        while self.scroll_layout.count() > 1:
            item = self.scroll_layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget not in keep:
                widget.deleteLater()
        if self.scroll_layout.indexOf(self.text) < 0:
            self.scroll_layout.insertWidget(0, self.text)
        if self.scroll_layout.indexOf(self._rich_body) < 0:
            self.scroll_layout.insertWidget(1, self._rich_body)
        self.text.setVisible(True)
        self._rich_body.setVisible(False)

    def set_show_images(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._show_images:
            return
        self._show_images = enabled
        self._image_generation += 1
        if getattr(self._source, "source_id", "") == "bookstack":
            self._bookstack_image_pixmaps.clear()
            chapter = self._chapters[self._current_idx] if 0 <= self._current_idx < len(self._chapters) else None
            has_rich_body = chapter is not None and getattr(chapter, "_cached_rich_content", None) is not None
            self._rich_body.setVisible(has_rich_body)
            self.text.setVisible(not has_rich_body)
            if not enabled:
                return
        else:
            self._clear_images()
        if self._mode == "scroll" and 0 <= self._current_idx < len(self._chapters):
            ch = self._chapters[self._current_idx]
            entries = getattr(ch, "_cached_image_urls", []) or []
            for entry in entries:
                image_url = entry.get("url", "") if isinstance(entry, dict) else entry
                if image_url:
                    self._load_image(image_url, self._image_generation, self._load_generation)

    def _load_image(self, image_url, image_generation, load_generation):
        if not self._show_images or image_generation != self._image_generation or load_generation != self._load_generation:
            return False
        from gui.components.cover_loader import CoverLoader
        if getattr(self._source, "source_id", "") != "bookstack" and not self._rich_body.isVisible():
            label = QLabel("加载中...")
            label.setAlignment(Qt.AlignCenter)
            label.setProperty("image_url", image_url)
            self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, label)
        CoverLoader.instance().load(
            image_url,
            lambda pixmap, url=image_url, ig=image_generation, lg=load_generation: self._on_image_loaded(url, ig, lg, pixmap),
            referer=self._current_chapter_url or None,
            cache=False,
            persist=False,
            source_id=getattr(self._source, "source_id", ""),
            source=self._source,
        )
        return True

    def _on_image_loaded(self, image_url, image_generation, load_generation, pixmap=None):
        if not self._show_images or image_generation != self._image_generation or load_generation != self._load_generation:
            return False
        if pixmap is None:
            for i in range(self.scroll_layout.count()):
                widget = self.scroll_layout.itemAt(i).widget()
                if widget is not None and widget.property("image_url") == image_url:
                    widget.deleteLater()
                    self.scroll_layout.takeAt(i)
                    break
            return False
        if getattr(self._source, "source_id", "") == "bookstack":
            self._bookstack_image_pixmaps[image_url] = pixmap
            self._apply_bookstack_image_resources()
            self._rich_body.updateGeometry()
            self.scroll_body.adjustSize()
            return True
        for i in range(self.scroll_layout.count()):
            widget = self.scroll_layout.itemAt(i).widget()
            if widget is not None and widget.property("image_url") == image_url:
                widget.setPixmap(pixmap.scaledToWidth(max(100, self.scroll.width() - 20), Qt.SmoothTransformation))
                return True
        return False

    def _on_chapter_loaded(self, ch, text, err, generation=None) -> None:
        if generation is not None and generation != self._load_generation:
            return
        if err:
            self._auto_loading = False  # 加载失败也要解锁，防死锁
            self._auto_prev_loading = False
            self.text.setText(f"加载失败：{err}")
            return
        ch._cached_text = text
        if not hasattr(ch, "_cached_image_urls"):
            ch._cached_image_urls = []
        ch._images_attempted = True
        self._auto_loading = False  # 翻页边界跳章完成，解除锁定
        self._auto_prev_loading = False
        if self._current_idx >= 0 and ch.url == self._chapters[self._current_idx].url:
            scroll_to_end = bool(getattr(self, "_scroll_on_load", 0))
            self._display_chapter(ch, text, scroll_to_end)
            self._update_progress()

    # ------------------------------------------------------------------ #
    def _toggle_toc(self) -> None:
        toc = self.toc_tree if getattr(self._source, "source_id", "") == "bookstack" else self.toc_list
        toc.setVisible(not toc.isVisible())

    def _find_chapter_index(self, url: str) -> int:
        target = str(url or "")
        base_url = ""
        if 0 <= self._current_idx < len(self._chapters):
            base_url = str(getattr(self._chapters[self._current_idx], "url", "") or "")
        if not base_url:
            base_url = str(getattr(self._source, "base_url", "") or "")
        if base_url:
            target = urljoin(base_url, target)
        target_parts = urlsplit(target)
        target = urlunsplit((target_parts.scheme, target_parts.netloc, target_parts.path, target_parts.query, "")).rstrip("/")
        for index, chapter in enumerate(self._chapters):
            chapter_url = str(getattr(chapter, "url", "") or "")
            parts = urlsplit(chapter_url)
            chapter_url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, "")).rstrip("/")
            if chapter_url == target:
                return index
        return -1

    def _on_rich_anchor_clicked(self, url: QUrl) -> None:
        target = url.toString()
        if target.startswith("#"):
            self._rich_body.scrollToAnchor(target[1:])
            return
        target_url = target
        if getattr(self._source, "source_id", "") == "bookstack":
            index = self._find_chapter_index(target)
            if index >= 0:
                self._load_chapter(index)
                return
            base_url = ""
            if 0 <= self._current_idx < len(self._chapters):
                base_url = str(getattr(self._chapters[self._current_idx], "url", "") or "")
            if not base_url:
                base_url = str(getattr(self._source, "base_url", "") or "")
            if base_url:
                target_url = urljoin(base_url, target)
        scheme = urlsplit(target_url).scheme.lower()
        if scheme not in {"http", "https"}:
            return
        webbrowser.open(target_url)

    def _on_toc_clicked(self, item, column=0) -> None:
        if isinstance(item, QTreeWidgetItem):
            if item.childCount() > 0:
                return
            role = item.data(0, Qt.UserRole)
        else:
            role = item.data(Qt.UserRole)
        self._load_chapter(int(role))

    def _jump_relative(self, delta: int) -> None:
        nxt = self._current_idx + delta
        if 0 <= nxt < len(self._chapters):
            self._load_chapter(nxt)

    # ------------------------------------------------------------------ #
    def _on_scroll_position(self, value: int) -> None:
        """滚动节流：距上次存盘 ≥1.5s 才落盘章内位置（避免高频写盘）。"""
        if self._detail is None or self._current_idx < 0:
            return
        now = time.monotonic()
        if now - self._last_pos_save_ts < 1.5:
            return
        self._last_pos_save_ts = now
        self._emit_position()

    def position_snapshot(self):
        """当前章内位置：(滚动比例 float 或翻页页/总页 float, 页索引 int 或 None)。"""
        if self._mode == "pager":
            ratio = self._current_page / self._page_count if self._page_count > 0 else 0.0
            return ratio, self._current_page
        vbar = self.scroll.verticalScrollBar()
        return (vbar.value() / vbar.maximum() if vbar.maximum() > 0 else 0.0), None

    def current_context(self):
        """当前阅读上下文 (detail, 章标题, 章URL)；未打开/未加载则 None。"""
        if self._detail is None or not (0 <= self._current_idx < len(self._chapters)):
            return None
        ch = self._chapters[self._current_idx]
        return (self._detail, ch.title, ch.url)

    def _emit_position(self) -> None:
        if self._detail is None or not (0 <= self._current_idx < len(self._chapters)):
            return
        ch = self._chapters[self._current_idx]
        pos, page = self.position_snapshot()
        try:
            location = self._build_current_location() if self._mode == "scroll" else None
            self.position_changed.emit((self._detail, ch.title, ch.url, pos, page, location))
        except RuntimeError:
            pass

    # ---- 自动滚动 ----


    def _on_scrollbar_user_interaction(self) -> None:
        """用户手动拖动滚动条（sliderPressed）时停止自动滚动，让手动接管。"""
        self._restore_cancel_token += 1
        self._restore_generation += 1
        self._stop_auto_scroll()




    def _auto_scroll_step(self, dt: float) -> float:
        """每帧推进量：固定步进（档位 × 5px，忽略 dt），保持原行为。"""
        return self.auto_scroll_speed_slider.value() * 5

    def _auto_scroll_allowed(self) -> bool:
        """滚动模式允许自动滚动。"""
        return self._mode == "scroll"

    def wheelEvent(self, event) -> None:  # noqa: N802
        """用户普通滚轮（无 Ctrl）→ 停止自动滚动。"""
        if self._auto_scrolling and not (event.modifiers() & Qt.ControlModifier):
            self._stop_auto_scroll()
        super().wheelEvent(event)

    @staticmethod
    def _clamp_font(size: int) -> int:
        return max(12, min(48, size))

    def set_font_scale(self, scale: float) -> None:
        """外部设置字体缩放（设置页实时生效）。"""
        self._font_delta = 0
        self._base_font = self._clamp_font(round(17 * float(scale or 1.0)))
        self._apply_font()

    def _adjust_font(self, delta: int) -> None:
        self._font_delta += delta
        self._apply_font()

    def _plain_body_css(self) -> str:
        size = self._clamp_font(self._base_font + self._font_delta)
        bg = self._reading_bg
        bg_css = f" background-color: {bg};" if bg else " background: transparent;"
        fg_css = f" color: {self._reading_fg};" if self._reading_fg else ""
        return (
            "font-family: Georgia, 'Times New Roman', 'Noto Serif CJK SC', SimSun, serif; "
            f"font-size: {size}px; line-height: 1.8; padding: 12px 20px;"
            " text-indent: 2em; margin-bottom: 0.9em;"
            f"{bg_css}{fg_css}"
        )

    def _apply_plain_body_style(self) -> None:
        css = self._plain_body_css()
        widgets = [self.text, self.paged_label]
        widgets.extend(self.scroll_body.findChildren(QLabel, "readerBody"))
        seen = set()
        for widget in widgets:
            if id(widget) in seen:
                continue
            seen.add(id(widget))
            widget.setStyleSheet(css)
            widget.setMaximumWidth(960)
        self.scroll_layout.setAlignment(self.text, Qt.AlignHCenter)

    def _apply_font(self) -> None:
        self._apply_plain_body_style()
        if getattr(self._source, "source_id", "") == "bookstack":
            self._apply_bookstack_style()
            self._resize_bookstack_body()
        self._repaginate()
        self._pager_show_page(self._current_page)

    def set_reading_style(self, bg: str = "", font_size: int = 0, fg: str = "") -> None:
        """设置阅读区独立背景色/字号/前景色（ui-reader #12）。

        bg：颜色字符串（#RRGGBB），空 = 透明跟随主题；
        font_size：>0 时覆盖全局字号，0 = 跟随全局 font_scale；
        fg：正文前景色（夜间黑等深色背景配浅字），空 = 跟随主题。
        """
        self._reading_bg = bg or ""
        self._reading_fg = fg or ""
        if font_size > 0:
            self._font_delta = 0
            self._base_font = self._clamp_font(font_size)
        self._apply_font()

    # ------------------------------------------------------------------ #
    def _toggle_mode(self) -> None:
        """滚动 / 翻页 模式切换。"""
        self._stop_auto_scroll()
        if self._mode == "scroll":
            self._mode = "pager"
            self.mode_btn.setText("滚动模式")
            self.body_stack.setCurrentWidget(self.pager_widget)
            self._repaginate()
            self._pager_show_page(self._current_page)
        else:
            self._mode = "scroll"
            self.mode_btn.setText("翻页模式")
            self.body_stack.setCurrentWidget(self.scroll)
            self.scroll.verticalScrollBar().setValue(0)
        self._update_auto_scroll_slider_state()

    def _repaginate(self):
        """按字数把正文拆成多页（每页约 CHARS_PER_PAGE 字）。

        分页结果按当前章缓存到 ch._cached_pages，换章才重切；
        章内翻页/字号调整复用缓存，避免长文反复全量切分导致卡顿。
        """
        text = getattr(self, "_paged_full_text", "") or ""
        if not text:
            self._pages = [""]
            self._page_count = 1
            self._current_page = 0
            return
        # 当前章缓存命中 → 直接复用
        cur_ch = self._chapters[self._current_idx] if 0 <= self._current_idx < len(self._chapters) else None
        cache = getattr(cur_ch, "_cached_pages", None) if cur_ch is not None else None
        if cache is not None and cache.get("text") == text:
            self._pages = cache["pages"]
            self._page_count = len(self._pages)
            if self._current_page >= self._page_count:
                self._current_page = 0
            return
        # 每页按字数切（字号/宽度动态变化时字数固定，行为确定）
        chars_per = 900
        self._pages = [
            text[i:i + chars_per] for i in range(0, len(text), chars_per)
        ]
        self._page_count = len(self._pages)
        if self._current_page >= self._page_count:
            self._current_page = 0
        # 写缓存
        if cur_ch is not None:
            cur_ch._cached_pages = {"pages": self._pages, "text": text}

    def _pager_show_page(self, page: int) -> None:
        """跳到第 page 页（0 基）。"""
        if not hasattr(self, "_pages") or not self._pages:
            self._repaginate()
        if page < 0 or page >= self._page_count:
            return
        self._current_page = page
        self.paged_label.setText(self._pages[page])
        self.paged_scroll.verticalScrollBar().setValue(0)
        self.pager_indicator.setText(f"{page + 1} / {self._page_count} 页")
        # 翻页后节流存盘位置（精准到页）
        if time.monotonic() - self._last_pos_save_ts >= 1.0:
            self._last_pos_save_ts = time.monotonic()
            self._emit_position()
        # 分页读到 70% → 预加载下一章（只下一章，防提前加载过多）
        if self._page_count > 0 and page >= self._page_count * 0.7:
            self._prefetch_next(self._current_idx)

    def _pager_turn(self, delta: int) -> None:
        """翻到上一页/下一页；越过章边界时自动切换章节（自然衔接）。

        - 下一页到最后页后再翻 → 下一章第一页
        - 上一页到第一页后再翻 → 上一章最后一页
        """
        self._repaginate()
        nxt = self._current_page + delta
        if 0 <= nxt < self._page_count:
            self._pager_show_page(nxt)
            return
        # 越过章边界
        if delta > 0:
            if self._auto_loading or self._current_idx >= len(self._chapters) - 1:
                return
            self._auto_loading = True
            self._load_chapter(self._current_idx + 1, scroll_to_end=False)
        else:
            if self._auto_prev_loading or self._current_idx <= 0:
                return
            self._auto_prev_loading = True
            self._load_chapter(self._current_idx - 1, scroll_to_end=True)


    def _update_progress(self) -> None:
        total = len(self._chapters)
        self.progress_label.setText(f"第{self._current_idx + 1}/{total}章")

    # ------------------------------------------------------------------ #
    def keyPressEvent(self, event) -> None:  # noqa: N802
        """键盘导航：翻页模式←翻页/→翻页，上下滚动/翻章节。"""
        key = event.key()
        if self._mode == "pager":
            if key == Qt.Key_Left:
                self._pager_turn(-1)
            elif key == Qt.Key_Right:
                self._pager_turn(1)
            elif key == Qt.Key_PageDown:
                self._pager_turn(1)
            elif key == Qt.Key_PageUp:
                self._pager_turn(-1)
            elif key == Qt.Key_Down:
                self._pager_turn(1)
            elif key == Qt.Key_Up:
                self._pager_turn(-1)
        else:
            self._stop_auto_scroll()
            vbar = self.scroll.verticalScrollBar()
            if key in (Qt.Key_Down, Qt.Key_PageDown):
                vbar.setValue(vbar.value() + self.scroll.height() * 2 // 3)
            elif key in (Qt.Key_Up, Qt.Key_PageUp):
                vbar.setValue(vbar.value() - self.scroll.height() * 2 // 3)
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    def _is_descendant(self, obj) -> bool:
        """obj（或其父链）是否属于本视图 —— 应用级过滤器只处理本视图内事件。"""
        w = obj if isinstance(obj, QWidget) else None
        while w is not None:
            if w is self:
                return True
            w = w.parentWidget()
        return False

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """应用级事件过滤器：鼠标侧键翻章 + Ctrl+滚轮调字号（本视图内）。

        正文 QLabel 带 TextSelectableByMouse 会吞掉子控件级鼠标事件，故用
        应用级过滤器覆盖滚动区/正文/目录等所有子控件；非本视图事件直接放行。
        """
        # 视图不可见时（如切到其他页/弹窗创建控件）直接放行，
        # 避免对无关控件的每个事件做父链遍历（应用级过滤器全量触发）卡顿弹窗。
        if not self.isVisible() or not self._is_descendant(obj):
            return super().eventFilter(obj, event)
        if event.type() == event.Type.MouseButtonPress:
            btn = event.button()
            if btn == Qt.XButton2:
                self._jump_relative(1)  # 鼠标前侧键 → 下一章
                return True
            if btn == Qt.XButton1:
                self._jump_relative(-1)  # 鼠标后侧键 → 上一章
                return True
        elif event.type() == event.Type.Wheel and (event.modifiers() & Qt.ControlModifier):
            delta = 1 if event.angleDelta().y() > 0 else -1
            self._adjust_font(delta)  # Ctrl+滚轮 → 调字号
            event.accept()
            return True
        return super().eventFilter(obj, event)

    def set_prefetch_config(self, enabled: bool, ahead: int, behind: int) -> None:
        """设置预加载数量；无效值回退默认值，不改变串行模型。"""
        if not enabled:
            self._prefetch_ahead = 0
            self._prefetch_behind = 0
            return
        try:
            ahead = int(ahead)
        except (TypeError, ValueError):
            ahead = 1
        try:
            behind = int(behind)
        except (TypeError, ValueError):
            behind = 0
        self._prefetch_ahead = max(0, min(1, ahead))
        self._prefetch_behind = 0

    @staticmethod
    def _prefetch_back_queue(total: int, idx: int, behind: int) -> list[int]:
        if behind <= 0:
            return []
        return [
            k for k in range(idx - 1, max(idx - 1 - behind, -1), -1)
            if 0 <= k < total
        ]

    def _prefetch_next(self, idx: int) -> None:
        """后台串行预加载后续配置数量的章节。"""
        if self._source is None or not self._chapters:
            return
        self._next_prefetch_queue = []
        total = len(self._chapters)
        for k in range(idx + 1, min(idx + 1 + self._prefetch_ahead, total)):
            ch = self._chapters[k]
            if getattr(ch, "_cached_text", None):
                continue
            if k == self._prefetch_idx:
                continue
            self._next_prefetch_queue.append(k)
        if self._next_prefetch_queue and self._prefetch_idx < 0:
            self._pump_next_prefetch()

    def _pump_next_prefetch(self) -> None:
        if not self._next_prefetch_queue:
            return
        k = self._next_prefetch_queue.pop(0)
        if not (0 <= k < len(self._chapters)):
            return
        self._prefetch_idx = k
        task = _LoadChapterTask(self._content, self._source, self._chapters[k], load_images=self._show_images)
        generation = self._load_generation
        task.signals.finished.connect(
            lambda ch, text, err, g=generation: self._on_prefetch_done(ch, text, err, g)
        )
        self._prefetch_task = task
        QThreadPool.globalInstance().start(task)

    def _on_prefetch_done(self, ch, text, err, generation=None) -> None:
        """预取完成：写缓存并继续泵下一个，保持 -2 空闲哨兵。"""
        if generation is not None and generation != self._load_generation:
            return
        self._prefetch_idx = -2
        if not err and text:
            ch._cached_text = text
        if self._next_prefetch_queue:
            self._pump_next_prefetch()

    # ------------------------------------------------------------------ #
    def _prefetch_prev(self, idx: int) -> None:
        """后台串行预加载当前章之前配置数量的章节。"""
        if self._source is None or not self._chapters:
            return
        self._prev_prefetch_queue = []
        for k in self._prefetch_back_queue(
            len(self._chapters), idx, self._prefetch_behind
        ):
            ch = self._chapters[k]
            if getattr(ch, "_cached_text", None):
                continue
            if k == self._prev_prefetch_idx:
                continue
            self._prev_prefetch_queue.append(k)
        if self._prev_prefetch_queue and self._prev_prefetch_idx < 0:
            self._pump_prev_prefetch()

    def _pump_prev_prefetch(self) -> None:
        """取向前缓存队列下一个章节开始预取（串行）。"""
        if not self._prev_prefetch_queue:
            self._prev_prefetch_idx = -2
            return
        k = self._prev_prefetch_queue.pop(0)
        self._prev_prefetch_idx = k
        ch = self._chapters[k]
        from PySide6.QtCore import QThreadPool

        task = _LoadChapterTask(self._content, self._source, ch, load_images=self._show_images)
        generation = self._load_generation
        task.signals.finished.connect(
            lambda loaded_ch, text, err, g=generation: self._on_prev_prefetch_done(loaded_ch, text, err, g)
        )
        self._prev_prefetch_task = task  # 持引用防 GC
        QThreadPool.globalInstance().start(task)

    def _on_prev_prefetch_done(self, ch, text, err, generation=None) -> None:
        """向前预取完成：写缓存，继续队列下一个；失败则终止本轮（随显示章再补）。"""
        if generation is not None and generation != self._load_generation:
            return
        self._prev_prefetch_idx = -2
        if err or not text:
            self._prev_prefetch_queue.clear()
            return
        ch._cached_text = text
        self._pump_prev_prefetch()
