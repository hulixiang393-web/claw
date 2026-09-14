"""小说阅读视图（NovelView）。

对应 ui-reader.md 小说功能点：
- 正文排版区（字号可调）
- 目录侧栏（可折叠，点击跳章）
- 上/下一章导航
- 章节预加载 + 续读记忆
- 正文解密（调用内核，混淆正文解码）
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal, QThreadPool, QRunnable, QObject
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from framework.content import Content, Detail


class NovelView(QWidget):
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
        self._prev_prefetch_queue = []  # 向前缓存队列（串行，最近前 3 章）
        self._prev_prefetch_idx = -2  # 正在向前预取的章节 idx（<0 表示空闲）
        self._prev_prefetch_task = None  # 持引用防 GC
        self._last_pos_save_ts = 0.0  # 上次章内位置存盘时间戳（节流 1.5s 存一次）
        self._pending_restore = None  # 打开书续读位置 (position, page)，首次显示章时定位
        self._auto_scrolling = False  # 自动滚动开关
        self._auto_pos = 0.0  # 自动滚动记住的位置（浮点，按速度递增，重排不打断）
        self._auto_timer = QTimer(self)  # 自动滚动定时器（interval=35ms，高频小步进平滑滚动）
        self._auto_timer.setInterval(35)

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
        self.auto_scroll_btn.setToolTip("开启/关闭自动滚动（Ctrl+Alt+A）")
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

        # 正文区：滚动模式 + 翻页模式，用 QStackedWidget 切换
        from PySide6.QtWidgets import QStackedWidget, QTextEdit

        self.body_stack = QStackedWidget()

        # -- 滚动模式（默认）--
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text = QLabel()
        self.text.setObjectName("readerBody")
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.scroll.setWidget(self.text)
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
        self._auto_timer.timeout.connect(self._auto_scroll_tick)
        QShortcut(QKeySequence("Ctrl+Alt+A"), self).activated.connect(self._toggle_auto_scroll)

    # ------------------------------------------------------------------ #
    def load(
        self,
        source,
        detail: Detail,
        start_chapter_url: str = "",
        restore_position: float | None = None,
        restore_page: int | None = None,
    ) -> None:
        """加载小说：设置目录 + 跳到指定章（或续读），并定位到章内位置。

        restore_position：0~1 滚动比例；restore_page：翻页模式页索引（0 基）。
        二者在首次显示章正文后应用（_display_chapter 读取 _pending_restore）。
        """
        self._source = source
        self._detail = detail
        self._chapters = detail.chapters
        # 换书：无条件清零续读位置——旧书若章节还在后台加载，本次 load() 的
        # 首章显示会消费到「上一本书残留的 _pending_restore」，把新书滚动条
        # 错滚到旧书位置（跨书串位置的 bug 根因）。清零后再按新书记录设置。
        self._pending_restore = None
        self._prev_prefetch_queue = []  # 换书清空向前缓存队列（旧队列指向旧书章节）
        self._prev_prefetch_idx = -2
        self._populate_toc()
        if restore_position is not None or restore_page is not None:
            self._pending_restore = (restore_position, restore_page)

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

    def _load_chapter(self, idx: int, scroll_to_end: bool = False) -> None:
        """加载指定章节正文（后台线程）。

        scroll_to_end=True：加载后定位到章尾（翻页边界向上跳章时用）；
        滚动模式滚到底，翻页模式跳到最后一页。
        """
        if self._source is None or not (0 <= idx < len(self._chapters)):
            return
        self._stop_auto_scroll()
        self._current_idx = idx
        ch = self._chapters[idx]

        self._scroll_on_load = getattr(self, "_scroll_on_load", 0)  # 0=顶部; 1=底部
        self._scroll_on_load = 1 if scroll_to_end else 0

        # 已缓存 → 直接显示
        if hasattr(ch, "_cached_text") and ch._cached_text:
            self._auto_loading = False  # 缓存命中也要解除翻页边界跳章锁
            self._auto_prev_loading = False
            self._display_chapter(ch, ch._cached_text, scroll_to_end)
            self._update_progress()
            self.chapter_changed.emit((self._detail, ch.title, ch.url))
            return

        self.text.setText("正在加载...")
        # 后台加载（信号跨线程安全）
        from PySide6.QtCore import QThreadPool

        task = _LoadChapterTask(self._content, self._source, ch)
        task.signals.finished.connect(self._on_chapter_loaded)
        self._chapter_task = task  # 持有引用，防止被 GC
        QThreadPool.globalInstance().start(task)
        self.chapter_changed.emit((self._detail, ch.title, ch.url))

    def _display_chapter(self, ch, text: str, scroll_to_end: bool = False) -> None:
        """正文前加章节编号行（如「第12章」），不含标题文字。"""
        from framework.content import chapter_label
        label = chapter_label(ch.title) or f"第{self._current_idx + 1}章"
        full = f"【{label}】\n\n{text}"
        self.text.setText(full)
        # 翻页视图同步（分页结果按章缓存，避免反复全量切分长文）
        self._paged_full_text = full
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
        # 后台预加载下一章：翻章时命中缓存秒开，不用现场等网络
        self._prefetch_next(self._current_idx)
        # 向前缓存：预取本章之前 3 章，向上翻章命中缓存秒开
        self._prefetch_prev(self._current_idx)
        # 续读定位：首章显示后恢复到上次的章内位置（页索引/滚动比例）
        if self._pending_restore is not None:
            pos, page = self._pending_restore
            self._pending_restore = None
            # 捕获当前 book detail：QTimer 触发时自校验仍为这本书（防旧书
            # 恢复回调在用户快速换书后把新书滚动条错滚到旧书位置）
            book = self._detail
            if page is not None:
                self._current_page = page
                self._pager_show_page(page)
            if pos is not None and pos > 0:
                vbar = self.scroll.verticalScrollBar()
                QTimer.singleShot(
                    0, lambda b=book: self._restore_scroll_if_book(pos, b)
                )
            # 强制落盘恢复后的位置（节流会吞掉恢复事件，防下次仍回顶部/第0页）
            self._last_pos_save_ts = 0.0
            QTimer.singleShot(0, self._emit_position)
        # 更新自动滚动滑块状态（根据模式和滚动范围）
        QTimer.singleShot(0, self._update_auto_scroll_slider_state)

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

    def _on_chapter_loaded(self, ch, text, err) -> None:
        if err:
            self._auto_loading = False  # 加载失败也要解锁，防死锁
            self._auto_prev_loading = False
            self.text.setText(f"加载失败：{err}")
            return
        ch._cached_text = text
        self._auto_loading = False  # 翻页边界跳章完成，解除锁定
        self._auto_prev_loading = False
        if self._current_idx >= 0 and ch.url == self._chapters[self._current_idx].url:
            scroll_to_end = bool(getattr(self, "_scroll_on_load", 0))
            self._display_chapter(ch, text, scroll_to_end)
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
            self.position_changed.emit((self._detail, ch.title, ch.url, pos, page))
        except RuntimeError:
            pass

    # ---- 自动滚动 ----
    def _toggle_auto_scroll(self) -> None:
        """切换自动滚动：停止/启动，按钮文字同步更新。"""
        if self._auto_scrolling:
            self._stop_auto_scroll()
        else:
            vbar = self.scroll.verticalScrollBar()
            if vbar.maximum() <= 0 or self._mode != "scroll":
                return
            self._auto_scrolling = True
            self._auto_pos = float(vbar.value())  # 记住当前位置，按速度递增
            self.auto_scroll_btn.setText("⏸ 停止")
            self._auto_timer.start()

    def _stop_auto_scroll(self) -> None:
        """停止自动滚动并复位按钮。"""
        if not self._auto_scrolling:
            return
        self._auto_scrolling = False
        self._auto_timer.stop()
        self.auto_scroll_btn.setText("▶ 自动滚动")

    def _auto_scroll_tick(self) -> None:
        """把记住的位置按设定速度递增，平滑向下滚动；到底自动停止。

        用独立浮点位置累积，不读回 vbar.value()：正文/图片重排不会打断推进，
        也不会与重排修正叠加导致跳过内容。
        """
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() <= 0:
            self._stop_auto_scroll()
            return
        self._auto_pos += self.auto_scroll_speed_slider.value() * 5
        if self._auto_pos >= vbar.maximum():
            vbar.setValue(vbar.maximum())
            self._stop_auto_scroll()
            return
        vbar.setValue(int(self._auto_pos))

    def _update_auto_scroll_slider_state(self) -> None:
        """根据当前模式和滚动范围启用/禁用自动滚动速度滑块。"""
        if self._mode == "scroll" and self.scroll.verticalScrollBar().maximum() > 0:
            self.auto_scroll_speed_slider.setEnabled(True)
        else:
            self.auto_scroll_speed_slider.setEnabled(False)

    def wheelEvent(self, event) -> None:  # noqa: N802
        """用户普通滚轮（无 Ctrl）→ 停止自动滚动。"""
        if self._auto_scrolling and not (event.modifiers() & Qt.ControlModifier):
            self._stop_auto_scroll()
        super().wheelEvent(event)

    @staticmethod
    def _clamp_font(size: int) -> int:
        return max(12, min(28, size))

    def set_font_scale(self, scale: float) -> None:
        """外部设置字体缩放（设置页实时生效）。保留用户 A+/A- 微调量。"""
        self._base_font = self._clamp_font(round(17 * float(scale or 1.0)))
        self._apply_font()

    def _adjust_font(self, delta: int) -> None:
        self._font_delta += delta
        self._apply_font()

    def _apply_font(self) -> None:
        size = self._clamp_font(self._base_font + self._font_delta)
        bg = self._reading_bg  # 阅读区独立背景（空 = 透明跟随主题）
        bg_css = f" background-color: {bg};" if bg else " background: transparent;"
        fg_css = f" color: {self._reading_fg};" if self._reading_fg else ""
        self.text.setStyleSheet(
            f"font-size: {size}px; line-height: 1.8; padding: 8px 12px;{bg_css}{fg_css}"
        )
        self.paged_label.setStyleSheet(
            f"font-size: {size}px; line-height: 1.8; padding: 12px 20px;{bg_css}{fg_css}"
        )
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
        if not self._is_descendant(obj):
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

    def _prefetch_next(self, idx: int) -> None:
        """后台预加载下一章（idx+1），翻章时命中缓存秒开。

        显示某章后触发：下一章未缓存且无进行中预取 → 后台抓取存 _cached_text。
        串行：同一时间只预取 1 章，避免并发拉多个章节抢占网络/内存。
        """
        if self._source is None:
            return
        nxt = idx + 1
        if not (0 <= nxt < len(self._chapters)):
            return
        # 资源就绪则无需预取
        nxt_ch = self._chapters[nxt]
        if hasattr(nxt_ch, "_cached_text") and nxt_ch._cached_text:
            return
        if self._prefetch_idx == nxt:
            return  # 该章已在预取中
        if self._prefetch_idx >= 0 and self._prefetch_idx != nxt:
            return  # 已有其他章在预取（串行）
        self._prefetch_idx = nxt
        from PySide6.QtCore import QThreadPool
        task = _LoadChapterTask(self._content, self._source, nxt_ch)
        task.signals.finished.connect(self._on_prefetch_done)
        self._prefetch_task = task  # 持引用防 GC
        QThreadPool.globalInstance().start(task)

    def _on_prefetch_done(self, ch, text, err) -> None:
        """预取完成：若有正文则写缓存，供翻章命中秒开。"""
        self._prefetch_idx = -2  # 清预取锁，允许下一个
        if err or not text:
            return
        ch._cached_text = text

    # ------------------------------------------------------------------ #
    def _prefetch_prev(self, idx: int) -> None:
        """后台预加载本章之前 3 章（向前缓存），向上翻章命中缓存秒开。

        与 _prefetch_next 独立串行（各自一把锁，最多同时预取 1 前 + 1 后）。
        窗口随当前章移动：每显示一章按新基点重建队列，只保留未缓存的最近 3 章。
        """
        if self._source is None or not self._chapters:
            return
        self._prev_prefetch_queue = []
        for k in range(idx - 1, max(idx - 4, -1), -1):
            if not (0 <= k < len(self._chapters)):
                continue
            ch = self._chapters[k]
            if hasattr(ch, "_cached_text") and ch._cached_text:
                continue
            if k == self._prev_prefetch_idx:  # 已在预取中，不必重复入队
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

        task = _LoadChapterTask(self._content, self._source, ch)
        task.signals.finished.connect(self._on_prev_prefetch_done)
        self._prev_prefetch_task = task  # 持引用防 GC
        QThreadPool.globalInstance().start(task)

    def _on_prev_prefetch_done(self, ch, text, err) -> None:
        """向前预取完成：写缓存，继续队列下一个；失败则终止本轮（随显示章再补）。"""
        self._prev_prefetch_idx = -2
        if err or not text:
            self._prev_prefetch_queue.clear()
            return
        ch._cached_text = text
        self._pump_prev_prefetch()


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
