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
        self._auto_loading = False  # 防止自动翻章重复触发
        self._auto_prev_loading = False  # 防止向上自动翻章重复触发
        self._last_auto_nav_ts = 0.0  # 上次自动翻章时间戳（防循环：新章滚到顶部又触发翻章）
        self._prefetch_idx = -2  # 正在后台预加载的章节 idx（<0 表示空闲）

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

        # 自动加载下一章：滚动模式在章尾不再自动翻章 —— 用户须点「下一章」按钮。
        # （_maybe_auto_next 保留但不连接，避免滚动到底意外跳章）

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
        # 懒加载预取：滚动读到 70% → 后台预取下一章（仅预取，不自动翻章）
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll_prefetch)

        # ---- 键盘导航 ----
        self.setFocusPolicy(Qt.StrongFocus)

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

    def _load_chapter(self, idx: int, scroll_to_end: bool = False) -> None:
        """加载指定章节正文（后台线程）。

        scroll_to_end=True：加载后定位到章尾（向上自动翻章时用）；
        滚动模式滚到底，翻页模式跳到最后一页。
        """
        if self._source is None or not (0 <= idx < len(self._chapters)):
            return
        self._current_idx = idx
        ch = self._chapters[idx]

        self._scroll_on_load = getattr(self, "_scroll_on_load", 0)  # 0=顶部; 1=底部
        self._scroll_on_load = 1 if scroll_to_end else 0

        # 已缓存 → 直接显示
        if hasattr(ch, "_cached_text") and ch._cached_text:
            self._auto_loading = False  # 缓存命中也要解除自动翻章锁
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
        # 滚动模式定位（程序化滚动，blockSignals 避免误触发对向自动翻章）
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

    def _scroll_to_bottom_silently(self) -> None:
        """无触发地滚到底（blockSignals 包住，防自动翻章循环）。"""
        vbar = self.scroll.verticalScrollBar()
        vbar.blockSignals(True)
        vbar.setValue(vbar.maximum())
        vbar.blockSignals(False)

    def _on_chapter_loaded(self, ch, text, err) -> None:
        if err:
            self.text.setText(f"加载失败：{err}")
            return
        ch._cached_text = text
        self._auto_loading = False  # 自动翻章完成，解除锁定
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

    def _maybe_auto_next(self, value: int) -> None:
        """滚动近底部 → 自动下一章；滚动回顶部 → 自动上一章。

        用时间冷却（_last_auto_nav_ts）防快速重触发，比边沿触发更自然。
        """
        if self._current_idx < 0:
            return
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() == 0:
            return
        if self._mode != "scroll":
            return  # 翻页模式走 _pager_turn 的章边界跳转
        # 时间冷却：翻章后 2s 内不重复触发
        if time.time() - self._last_auto_nav_ts < 2.0:
            return
        max_v = vbar.maximum()
        # 读到 70% → 预加载下一章（只下一章；_prefetch_next 有锁防重复，翻章时命中缓存秒开）
        if value >= max_v * 0.7:
            self._prefetch_next(self._current_idx)
        # 向下：近底部且非末章 → 下一章
        if value >= max_v - 40:
            if self._auto_loading or self._current_idx >= len(self._chapters) - 1:
                return
            self._auto_loading = True
            self._last_auto_nav_ts = time.time()
            self._load_chapter(self._current_idx + 1)
        # 向上：滚回顶部且非首章 → 上一章末尾
        elif value <= 2:
            if self._auto_prev_loading or self._current_idx <= 0:
                return
            self._auto_prev_loading = True
            self._last_auto_nav_ts = time.time()
            self._load_chapter(self._current_idx - 1, scroll_to_end=True)
            # 跳到上一章末尾在 _on_chapter_loaded 里处理（need_scroll_bottom）

    def _on_scroll_prefetch(self, value: int) -> None:
        """滚动读到 70% → 预取下一章（仅预取，不启用自动翻章/翻回）。

        由滚动条 valueChanged 触发。区别于 _maybe_auto_next（同时含底部自动
        翻章/顶部自动翻回）——这里只保留 70% 预取；自动翻章维持禁用，
        用户手动点「下一章」按钮翻章。
        """
        if self._current_idx < 0:
            return
        if self._mode != "scroll":
            return  # 翻页模式由 _pager_show_page 触发 70% 预取
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() == 0:
            return
        if value >= vbar.maximum() * 0.7:
            self._prefetch_next(self._current_idx)

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
        self.text.setStyleSheet(
            f"font-size: {size}px; line-height: 1.8; padding: 8px 12px;{bg_css}"
        )
        self.paged_label.setStyleSheet(
            f"font-size: {size}px; line-height: 1.8; padding: 12px 20px;{bg_css}"
        )
        self._repaginate()
        self._pager_show_page(self._current_page)

    def set_reading_style(self, bg: str = "", font_size: int = 0) -> None:
        """设置阅读区独立背景色/字号（ui-reader #12）。

        bg：颜色字符串（#RRGGBB），空 = 透明跟随主题；
        font_size：>0 时覆盖全局字号，0 = 跟随全局 font_scale。
        """
        self._reading_bg = bg or ""
        if font_size > 0:
            self._base_font = self._clamp_font(font_size)
        self._apply_font()

    # ------------------------------------------------------------------ #
    def _toggle_mode(self) -> None:
        """滚动 / 翻页 模式切换。"""
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
        if cur_ch is not None and getattr(cur_ch, "_cached_pages", None) == text:
            self._pages = cur_ch._cached_pages["pages"]
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
            cur_ch._cached_pages = {"pages": self._pages}

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
            vbar = self.scroll.verticalScrollBar()
            if key in (Qt.Key_Down, Qt.Key_PageDown):
                vbar.setValue(vbar.value() + self.scroll.height() * 2 // 3)
            elif key in (Qt.Key_Up, Qt.Key_PageUp):
                vbar.setValue(vbar.value() - self.scroll.height() * 2 // 3)
        super().keyPressEvent(event)

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
