"""小说阅读视图（NovelView）。

对应 PROJECT_GUIDE.md §6.7 小说功能点：
- 正文排版区（字号可调）
- 目录侧栏（可折叠，点击跳章）
- 上/下一章导航
- 章节预加载 + 续读记忆
- 正文解密（调用内核，混淆正文解码）
"""

from __future__ import annotations

from html import escape
import hashlib
import logging
import re
import time
import webbrowser
from urllib.parse import urljoin, urlsplit, urlunsplit

from PySide6.QtCore import Qt, QTimer, Signal, QThreadPool, QRunnable, QObject, QUrl
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut, QTextDocument
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
    QMenu,
    QWidget,
)

from framework.content import Content, Detail

from gui.pages.reader.auto_scroll import AutoScrollMixin


from gui.pages.reader.novel_tasks import _LoadChapterSignals, _LoadChapterTask

log = logging.getLogger(__name__)


class NovelView(AutoScrollMixin, QWidget):
    """小说正文阅读视图。"""

    READER_FONT_FAMILIES = {
        "system": (
            "系统默认",
            "Georgia, 'Times New Roman', 'Noto Serif CJK SC', SimSun, serif",
        ),
        "yahei": ("微软雅黑", "'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif"),
        "song": ("宋体", "SimSun, 'Songti SC', serif"),
        "hei": ("黑体", "SimHei, 'Heiti SC', sans-serif"),
        "kai": ("楷体", "KaiTi, 'Kaiti SC', serif"),
    }

    chapter_changed = Signal(object)  # 发出 (detail, chapter_title) 供续读
    position_changed = Signal(object)  # (detail, title, url, position, page) 章内位置续读
    fullscreen_requested = Signal()  # 工具条 ⛶ → ReaderPage 切主窗全屏
    background_cycle_requested = Signal()  # 「背景」按钮 → ReaderPage 循环切换护眼背景色

    def __init__(
        self, content: Content, font_scale: float = 1.0, parent=None, settings=None
    ):
        super().__init__(parent)
        self._content = content
        self._settings = settings
        self._font_scale = float(font_scale or 1.0)
        self._source = None
        self._detail: Detail | None = None
        self._chapters = []
        self._current_idx = -1
        self._font_delta = 0
        self._base_font = self._clamp_font(round(17 * self._font_scale))
        self._reader_font_size = 0
        self._reader_font_family = "system"
        self._reader_language = "zh-cn"
        self._line_spacing = 1.8
        self._content_width = 960
        self._paragraph_indent = True
        # 段落/文字排版（常见小说阅读器的可调项）
        self._paragraph_spacing = 0.9   # 段间距（em）
        self._indent_width = 2          # 首行缩进（字）
        self._text_align = "justify"    # 对齐：justify | left
        self._letter_spacing = 0.0      # 字间距（em）
        self._displayed_plain_text = ""
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
        # 片段渲染（正文夹图片）：文本片段是独立 QLabel，改排版设置时
        # 必须重写它们的 HTML，否则段距/行距/字间距在这些源上毫无反应。
        self._fragment_text_labels = []  # [(QLabel, 原始片段文本), ...]
        self._fragment_title_label = None
        self._fragment_title_text = ""
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
        self._mode = "scroll"  # scroll / pager
        self._load_reader_options()
        # 自动滚动定时器（interval=35ms，高频小步进平滑滚动）
        self._build_auto_scroll_timer()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- 工具栏 ----
        toolbar_host = QWidget()
        toolbar_host.setObjectName("readerToolbar")
        toolbar_host.setStyleSheet(
            "QPushButton, QToolButton { min-height: 30px; padding: 4px 10px; }"
        )
        toolbar = QHBoxLayout(toolbar_host)
        toolbar.setContentsMargins(4, 2, 4, 2)
        toolbar.setSpacing(6)
        self.toc_btn = QToolButton()
        self.toc_btn.setText("☰ 目录")
        self.toc_btn.clicked.connect(self._toggle_toc)
        toolbar.addWidget(self.toc_btn)

        self.font_down = QPushButton("A-")
        self.font_up = QPushButton("A+")
        self.font_down.clicked.connect(lambda: self._adjust_font(-1))
        self.font_up.clicked.connect(lambda: self._adjust_font(1))
        toolbar.addWidget(self.font_down)
        toolbar.addWidget(self.font_up)

        self.mode_btn = QPushButton("翻页模式")
        self.mode_btn.clicked.connect(self._toggle_mode)
        toolbar.addWidget(self.mode_btn)

        self.bg_btn = QPushButton("背景")
        self.bg_btn.setToolTip("切换阅读背景色（白/米黄/护眼绿/夜间黑）")
        self.bg_btn.clicked.connect(self.background_cycle_requested.emit)
        toolbar.addWidget(self.bg_btn)

        self.language_btn = QPushButton("简体")
        self.language_btn.setToolTip("切换正文语言：简体中文 / 繁体中文")
        self.language_btn.clicked.connect(self._toggle_reader_language)
        self._sync_language_button()
        toolbar.addWidget(self.language_btn)

        self.settings_btn = QToolButton()
        self.settings_btn.setText("⚙")
        self.settings_btn.setToolTip("阅读设置")
        self._settings_menu = QMenu(self)
        self._build_settings_menu()
        self.settings_btn.setMenu(self._settings_menu)
        self.settings_btn.setPopupMode(QToolButton.InstantPopup)
        toolbar.addWidget(self.settings_btn)

        self.fullscreen_btn = QPushButton("⛶")
        self.fullscreen_btn.setToolTip("全屏阅读")
        self.fullscreen_btn.clicked.connect(self.fullscreen_requested.emit)
        toolbar.addWidget(self.fullscreen_btn)

        self.auto_scroll_btn = QPushButton("▶ 自动滚动")
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
        layout.addWidget(toolbar_host)

        # Keep the work title and current chapter visible while the body scrolls.
        self.reader_header = QWidget()
        header_layout = QVBoxLayout(self.reader_header)
        self._reader_header_layout = header_layout
        header_layout.setContentsMargins(12, 6, 12, 6)
        header_layout.setSpacing(2)
        self.book_title_label = QLabel("未命名作品")
        self.book_title_label.setObjectName("readerBookTitle")
        self.book_title_label.setStyleSheet("font-size: 18px; font-weight: 600; color: palette(text);")
        self.book_title_label.setWordWrap(True)
        self.book_title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.book_title_label.setToolTip("")
        self.chapter_title_label = QLabel("")
        self.chapter_title_label.setObjectName("readerChapterTitle")
        self.chapter_title_label.setStyleSheet("font-size: 13px; color: palette(mid);")
        self.chapter_title_label.setWordWrap(True)
        self.chapter_title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        header_layout.addWidget(self.book_title_label)
        header_layout.addWidget(self.chapter_title_label)
        # Do not fix this height: long book/chapter titles must be allowed to
        # wrap, otherwise the second line is clipped and appears misaligned.
        self.reader_header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        layout.addWidget(self.reader_header)

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
        self._rich_body.setMaximumWidth(self._content_width)
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
    def _load_reader_options(self) -> None:
        """Load only the small set of options that affect novel layout."""
        if self._settings is None:
            return
        get = self._settings.get
        try:
            size = int(get("reader", "font_size", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        self._reader_font_size = max(0, min(48, size))
        if self._reader_font_size:
            self._base_font = self._clamp_font(self._reader_font_size)
        family = str(get("reader", "font_family", "system") or "system")
        self._reader_font_family = (
            family if family in self.READER_FONT_FAMILIES else "system"
        )
        language = str(get("reader", "language", "zh-cn") or "zh-cn").lower()
        self._reader_language = language if language in {"zh-cn", "zh-tw"} else "zh-cn"
        try:
            spacing = float(get("reader", "line_spacing", 1.8) or 1.8)
        except (TypeError, ValueError):
            spacing = 1.8
        self._line_spacing = max(1.4, min(2.4, spacing))
        try:
            width = int(get("reader", "content_width", 960) or 960)
        except (TypeError, ValueError):
            width = 960
        self._content_width = max(640, min(1200, width))
        self._paragraph_indent = bool(get("reader", "paragraph_indent", True))
        try:
            paragraph_spacing = float(get("reader", "paragraph_spacing", 0.9))
        except (TypeError, ValueError):
            paragraph_spacing = 0.9
        self._paragraph_spacing = max(0.0, min(2.4, paragraph_spacing))
        try:
            indent_width = int(get("reader", "paragraph_indent_width", 2) or 2)
        except (TypeError, ValueError):
            indent_width = 2
        self._indent_width = max(0, min(4, indent_width))
        align = str(get("reader", "text_align", "justify") or "justify").lower()
        self._text_align = align if align in {"justify", "left"} else "justify"
        try:
            letter_spacing = float(get("reader", "letter_spacing", 0.0) or 0.0)
        except (TypeError, ValueError):
            letter_spacing = 0.0
        self._letter_spacing = max(0.0, min(0.3, letter_spacing))
        self._show_images = bool(get("reader", "show_images", True))

    def _persist_reader_options(self) -> None:
        if self._settings is None:
            return
        self._settings.set("reader", "font_size", self._reader_font_size)
        self._settings.set("reader", "font_family", self._reader_font_family)
        self._settings.set("reader", "language", self._reader_language)
        self._settings.set("reader", "line_spacing", self._line_spacing)
        self._settings.set("reader", "content_width", self._content_width)
        self._settings.set("reader", "paragraph_indent", self._paragraph_indent)
        self._settings.set("reader", "paragraph_spacing", self._paragraph_spacing)
        self._settings.set("reader", "paragraph_indent_width", self._indent_width)
        self._settings.set("reader", "text_align", self._text_align)
        self._settings.set("reader", "letter_spacing", self._letter_spacing)
        self._settings.save()

    def _build_settings_menu(self) -> None:
        menu = self._settings_menu

        font_menu = menu.addMenu("字号")
        self._font_group = QActionGroup(font_menu)
        self._font_group.setExclusive(True)
        self._font_actions = {}
        for size in (14, 16, 18, 20, 24, 28):
            action = QAction(f"{size}px", font_menu, checkable=True)
            action.setData(size)
            action.triggered.connect(lambda _checked, value=size: self._set_reader_font_size(value))
            self._font_group.addAction(action)
            font_menu.addAction(action)
            self._font_actions[size] = action
        custom = QAction("跟随全局设置", font_menu, checkable=True)
        custom.setData(0)
        custom.triggered.connect(lambda: self._set_reader_font_size(0))
        self._font_group.addAction(custom)
        font_menu.addAction(custom)
        self._font_actions[0] = custom

        family_menu = menu.addMenu("字体样式")
        self._font_family_group = QActionGroup(family_menu)
        self._font_family_group.setExclusive(True)
        self._font_family_actions = {}
        for family, (label, _css) in self.READER_FONT_FAMILIES.items():
            action = QAction(label, family_menu, checkable=True)
            action.setData(family)
            action.triggered.connect(
                lambda _checked, value=family: self._set_reader_font_family(value)
            )
            self._font_family_group.addAction(action)
            family_menu.addAction(action)
            self._font_family_actions[family] = action

        spacing_menu = menu.addMenu("行距")
        self._spacing_group = QActionGroup(spacing_menu)
        self._spacing_group.setExclusive(True)
        self._spacing_actions = {}
        for spacing, label in ((1.5, "紧凑"), (1.8, "标准"), (2.1, "宽松"), (2.4, "超宽")):
            action = QAction(label, spacing_menu, checkable=True)
            action.setData(spacing)
            action.triggered.connect(
                lambda _checked, value=spacing: self._set_line_spacing(value)
            )
            self._spacing_group.addAction(action)
            spacing_menu.addAction(action)
            self._spacing_actions[spacing] = action

        para_menu = menu.addMenu("段间距")
        self._para_group = QActionGroup(para_menu)
        self._para_group.setExclusive(True)
        self._para_actions = {}
        for value, label in ((0.2, "无"), (0.6, "紧凑"), (0.9, "标准"),
                             (1.4, "宽松"), (2.0, "开阔")):
            action = QAction(label, para_menu, checkable=True)
            action.setData(value)
            action.triggered.connect(
                lambda _checked, spacing=value: self._set_paragraph_spacing(spacing)
            )
            self._para_group.addAction(action)
            para_menu.addAction(action)
            self._para_actions[value] = action

        indent_menu = menu.addMenu("首行缩进")
        self._indent_group = QActionGroup(indent_menu)
        self._indent_group.setExclusive(True)
        self._indent_actions = {}
        for value, label in ((0, "不缩进"), (1, "缩进一字"), (2, "缩进两字"),
                             (3, "缩进三字")):
            action = QAction(label, indent_menu, checkable=True)
            action.setData(value)
            action.triggered.connect(
                lambda _checked, width=value: self._set_indent_width(width)
            )
            self._indent_group.addAction(action)
            indent_menu.addAction(action)
            self._indent_actions[value] = action

        align_menu = menu.addMenu("对齐方式")
        self._align_group = QActionGroup(align_menu)
        self._align_group.setExclusive(True)
        self._align_actions = {}
        for value, label in (("justify", "两端对齐"), ("left", "左对齐")):
            action = QAction(label, align_menu, checkable=True)
            action.setData(value)
            action.triggered.connect(
                lambda _checked, align=value: self._set_text_align(align)
            )
            self._align_group.addAction(action)
            align_menu.addAction(action)
            self._align_actions[value] = action

        letter_menu = menu.addMenu("字间距")
        self._letter_group = QActionGroup(letter_menu)
        self._letter_group.setExclusive(True)
        self._letter_actions = {}
        for value, label in ((0.0, "标准"), (0.05, "稍宽"), (0.1, "宽松"),
                             (0.15, "很宽")):
            action = QAction(label, letter_menu, checkable=True)
            action.setData(value)
            action.triggered.connect(
                lambda _checked, spacing=value: self._set_letter_spacing(spacing)
            )
            self._letter_group.addAction(action)
            letter_menu.addAction(action)
            self._letter_actions[value] = action

        width_menu = menu.addMenu("版心宽度")
        self._width_group = QActionGroup(width_menu)
        self._width_group.setExclusive(True)
        self._width_actions = {}
        for width, label in ((680, "窄"), (820, "适中"), (960, "标准"), (1100, "宽")):
            action = QAction(label, width_menu, checkable=True)
            action.setData(width)
            action.triggered.connect(
                lambda _checked, value=width: self._set_content_width(value)
            )
            self._width_group.addAction(action)
            width_menu.addAction(action)
            self._width_actions[width] = action

        mode_menu = menu.addMenu("阅读方式")
        self._mode_group = QActionGroup(mode_menu)
        self._mode_group.setExclusive(True)
        self._mode_actions = {}
        for mode, label in (("scroll", "上下滚动"), ("pager", "翻页阅读")):
            action = QAction(label, mode_menu, checkable=True)
            action.setData(mode)
            action.triggered.connect(
                lambda _checked, value=mode: self._set_mode(value)
            )
            self._mode_group.addAction(action)
            mode_menu.addAction(action)
            self._mode_actions[mode] = action

        self.indent_action = QAction("段首缩进", menu, checkable=True)
        self.indent_action.toggled.connect(self._set_paragraph_indent)
        menu.addAction(self.indent_action)
        self.images_action = QAction("显示正文图片", menu, checkable=True)
        self.images_action.toggled.connect(self.set_show_images)
        menu.addAction(self.images_action)
        menu.addSeparator()
        self.reset_reader_action = QAction("恢复阅读默认设置", menu)
        self.reset_reader_action.triggered.connect(self._reset_reader_options)
        menu.addAction(self.reset_reader_action)
        self._sync_settings_actions()

    def _sync_settings_actions(self) -> None:
        # setChecked 会触发 toggled → 又回到 _set_* → 再 sync；这里用标志位
        # 挡住构建/同步期间的级联回调（否则构造时就会反复重排正文/写配置）。
        self._syncing_settings = True
        try:
            self._sync_settings_actions_impl()
        finally:
            self._syncing_settings = False

    def _sync_settings_actions_impl(self) -> None:
        current_size = self._reader_font_size
        for size, action in self._font_actions.items():
            action.setChecked(size == current_size)
        for family, action in self._font_family_actions.items():
            action.setChecked(family == self._reader_font_family)
        spacing = min(self._spacing_actions, key=lambda value: abs(value - self._line_spacing))
        for value, action in self._spacing_actions.items():
            action.setChecked(value == spacing)
        width = min(self._width_actions, key=lambda value: abs(value - self._content_width))
        for value, action in self._width_actions.items():
            action.setChecked(value == width)
        for mode, action in self._mode_actions.items():
            action.setChecked(mode == self._mode)
        para = min(self._para_actions, key=lambda value: abs(value - self._paragraph_spacing))
        for value, action in self._para_actions.items():
            action.setChecked(value == para)
        indent = max(self._indent_actions, key=lambda value: value if value <= self._indent_width else -1)
        for value, action in self._indent_actions.items():
            action.setChecked(value == indent)
        for align, action in self._align_actions.items():
            action.setChecked(align == self._text_align)
        letter = min(self._letter_actions, key=lambda value: abs(value - self._letter_spacing))
        for value, action in self._letter_actions.items():
            action.setChecked(value == letter)
        self.indent_action.setChecked(self._paragraph_indent)
        self.images_action.setChecked(self._show_images)

    def _set_reader_font_size(self, size: int) -> None:
        if getattr(self, "_syncing_settings", False):
            return
        self._reader_font_size = max(0, min(48, int(size)))
        self._font_delta = 0
        self._base_font = (
            self._clamp_font(self._reader_font_size)
            if self._reader_font_size
            else self._clamp_font(round(17 * self._font_scale))
        )
        self._apply_font()
        self._persist_reader_options()
        self._sync_settings_actions()

    def _set_reader_font_family(self, family: str) -> None:
        if getattr(self, "_syncing_settings", False):
            return
        if family not in self.READER_FONT_FAMILIES:
            return
        self._reader_font_family = family
        self._apply_font()
        self._persist_reader_options()
        self._sync_settings_actions()

    def _set_line_spacing(self, spacing: float) -> None:
        if getattr(self, "_syncing_settings", False):
            return
        self._line_spacing = max(1.4, min(2.4, float(spacing)))
        self._apply_font()
        self._persist_reader_options()
        self._sync_settings_actions()

    def _set_paragraph_spacing(self, spacing: float) -> None:
        """段间距（em）：常见阅读器的「段落留白」档位。"""
        if getattr(self, "_syncing_settings", False):
            return
        self._paragraph_spacing = max(0.0, min(2.4, float(spacing)))
        self._apply_font()
        self._persist_reader_options()
        self._sync_settings_actions()

    def _set_indent_width(self, width: int) -> None:
        """首行缩进宽度（字）；0 = 不缩进（等价于关掉段首缩进）。"""
        if getattr(self, "_syncing_settings", False):
            return
        self._indent_width = max(0, min(4, int(width)))
        self._paragraph_indent = self._indent_width > 0
        self._apply_font()
        self._persist_reader_options()
        self._sync_settings_actions()

    def _set_text_align(self, align: str) -> None:
        if getattr(self, "_syncing_settings", False):
            return
        align = str(align or "").lower()
        if align not in {"justify", "left"}:
            return
        self._text_align = align
        self._apply_font()
        self._persist_reader_options()
        self._sync_settings_actions()

    def _set_letter_spacing(self, spacing: float) -> None:
        """字间距（em）；0 = 标准。"""
        if getattr(self, "_syncing_settings", False):
            return
        self._letter_spacing = max(0.0, min(0.3, float(spacing)))
        self._apply_font()
        self._persist_reader_options()
        self._sync_settings_actions()

    def _set_content_width(self, width: int) -> None:
        if getattr(self, "_syncing_settings", False):
            return
        self._content_width = max(640, min(1200, int(width)))
        self._apply_font()
        self._persist_reader_options()
        self._sync_settings_actions()

    def _set_paragraph_indent(self, enabled: bool) -> None:
        if getattr(self, "_syncing_settings", False):
            return
        self._paragraph_indent = bool(enabled)
        if self._paragraph_indent and self._indent_width <= 0:
            self._indent_width = 2
        self._apply_font()
        self._persist_reader_options()
        self._sync_settings_actions()

    def _reset_reader_options(self) -> None:
        self._reader_font_size = 0
        self._font_delta = 0
        self._base_font = self._clamp_font(round(17 * self._font_scale))
        self._reader_font_family = "system"
        self._reader_language = "zh-cn"
        self._line_spacing = 1.8
        self._content_width = 960
        self._paragraph_indent = True
        self._paragraph_spacing = 0.9
        self._indent_width = 2
        self._text_align = "justify"
        self._letter_spacing = 0.0
        self.set_show_images(True)
        self._apply_font()
        self._persist_reader_options()
        self._sync_language_button()
        self._sync_settings_actions()

    def _ensure_decoded(self, ch, text: str) -> str:
        """渲染前兜底：正文若还带反爬字体假字，就地解一次。

        章节文本可能来自新抓取、正文缓存、书架缓存仓库或离线预缓存——后三者
        存的是**解密后**文本（TTL 7 天），解码上线前写入的条目会原样带假字
        （「张起灵녈头」）。这里统一过一遍 Content 的自愈逻辑，正常文本零开销。
        """
        body = str(text or "")
        if not body:
            return body
        content = getattr(self, "_content", None)
        redo = getattr(content, "_redecode_if_obfuscated", None)
        if not callable(redo):
            return body
        try:
            fixed = redo(self._source, str(getattr(ch, "url", "") or ""), body)
        except Exception:  # noqa: BLE001 —— 兜底失败就用原文
            return body
        if fixed and fixed != body:
            try:
                ch._cached_text = fixed
            except Exception:  # noqa: BLE001
                pass
            fake = sum(1 for ch_ in body if 0xAC00 <= ord(ch_) <= 0xD7A3)
            log.info("阅读器兜底解码：%d 个假字 → 0（%s）", fake,
                     str(getattr(ch, "url", "") or "")[-40:])
        out = fixed or body
        leftover = sum(1 for ch_ in out if 0xAC00 <= ord(ch_) <= 0xD7A3)
        if leftover:
            # 兜底之后还有假字：把源和样本打进日志，方便按源定位（不要猜）
            log.warning(
                "阅读器仍见假字：源 %s 章节 %s 剩余 %d 个，样本 %s",
                getattr(getattr(self, "_source", None), "source_id", "?"),
                str(getattr(ch, "url", "") or "")[-40:],
                leftover,
                "".join(ch_ for ch_ in out if 0xAC00 <= ord(ch_) <= 0xD7A3)[:12],
            )
        return out

    def _reader_convert_text(self, text: str) -> str:
        """Convert display text without changing the cached source text."""
        from framework.zh_convert import convert

        return convert(str(text or ""), self._reader_language)

    def _sync_language_button(self) -> None:
        if not hasattr(self, "language_btn"):
            return
        label = "简体" if self._reader_language == "zh-cn" else "繁体"
        target = "繁体中文" if self._reader_language == "zh-cn" else "简体中文"
        self.language_btn.setText(label)
        self.language_btn.setToolTip(f"当前：{label}，点击切换为{target}")

    def _refresh_toc_language(self) -> None:
        for index, chapter in enumerate(self._chapters):
            title = self._reader_convert_text(getattr(chapter, "title", "") or f"第{index + 1}章")
            if getattr(self._source, "source_id", "") == "bookstack":
                if index < len(self._toc_items):
                    self._toc_items[index].setText(0, title)
            elif index < self.toc_list.count():
                self.toc_list.item(index).setText(title)

    def _set_reader_language(self, language: str) -> None:
        language = str(language or "").lower()
        if language not in {"zh-cn", "zh-tw"} or language == self._reader_language:
            self._sync_language_button()
            return
        position, page = self.position_snapshot()
        self._reader_language = language
        self._persist_reader_options()
        self._sync_language_button()
        self._refresh_toc_language()
        self._update_reader_header()
        if not (0 <= self._current_idx < len(self._chapters)):
            return
        chapter = self._chapters[self._current_idx]
        source_text = getattr(chapter, "_cached_text", "") or ""
        if not source_text:
            return
        self._display_chapter(chapter, source_text)
        if self._mode == "pager":
            QTimer.singleShot(0, lambda p=page: self._pager_show_page(min(p or 0, self._page_count - 1)))
        else:
            QTimer.singleShot(0, lambda p=position: self._restore_scroll(p))

    def _toggle_reader_language(self) -> None:
        self._set_reader_language("zh-tw" if self._reader_language == "zh-cn" else "zh-cn")

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
        self._chapters = detail.chapters
        self.language_btn.setEnabled(
            getattr(source, "content_type", "novel") == "novel"
            and getattr(source, "source_id", "") != "bookstack"
        )
        self._current_idx = -1
        self._update_reader_header()
        self._restore_generation += 1
        self._restore_cancel_token += 1
        self._load_generation += 1
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
        self._displayed_plain_text = ""
        if getattr(self._source, "source_id", "") == "bookstack":
            if 0 <= idx < len(self._toc_items):
                self.toc_tree.setCurrentItem(self._toc_items[idx])
        elif idx < self.toc_list.count():
            self.toc_list.setCurrentRow(idx)
        ch = self._chapters[idx]
        self._update_reader_header(ch)

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
        text = self._ensure_decoded(ch, text)
        label = chapter_label(ch.title) or f"第{self._current_idx + 1}章"
        self._update_reader_header(ch, label)
        display_text = self._prepare_reader_body(ch, text)
        full = f"【{self._reader_convert_text(label)}】\n\n{display_text}"
        self._current_chapter_url = ch.url
        rich = getattr(ch, "_cached_rich_content", None)
        self._clear_images()
        self._bookstack_image_pixmaps.clear()
        if getattr(self._source, "source_id", "") == "bookstack":
            self._rich_body.setMaximumWidth(self._content_width)
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
            self._displayed_plain_text = full
            self._set_reader_text(self.text, full)
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
            title_widget = QLabel()
            title_widget.setTextFormat(Qt.RichText)
            title_widget.setObjectName("readerBody")
            title_widget.setWordWrap(True)
            title_widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
            title_widget.setStyleSheet(self._plain_body_css())
            title_widget.setMaximumWidth(self._content_width)
            self._set_reader_text(title_widget, f"【{label}】", has_title=True)
            self._fragment_title_label = title_widget
            self._fragment_title_text = f"【{label}】"
            self.scroll_layout.insertWidget(
                self.scroll_layout.count() - 1, title_widget, alignment=Qt.AlignHCenter
            )
            for fragment in fragments:
                if fragment.get("type") == "text":
                    # 片段正文与主正文用同一套段落排版：早先这里直接 setText(纯文本)，
                    # 图片夹在正文里时整章就退化成一个没有段距/缩进的 QLabel，
                    # 表现就是「所有段落挤成一段」。
                    raw = str(fragment.get("text") or "")
                    segment = QLabel()
                    segment.setTextFormat(Qt.RichText)
                    segment.setObjectName("readerBody")
                    segment.setWordWrap(True)
                    segment.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    segment.setStyleSheet(self._plain_body_css())
                    segment.setMaximumWidth(self._content_width)
                    self._set_reader_text(
                        segment,
                        self._split_merged_paragraphs(self._reader_convert_text(raw)),
                        has_title=False,
                    )
                    self._fragment_text_labels.append((segment, raw))
                    self.scroll_layout.insertWidget(
                        self.scroll_layout.count() - 1, segment, alignment=Qt.AlignHCenter
                    )
                else:
                    image_url = fragment.get("url")
                    if isinstance(image_url, str) and image_url:
                        self._load_image(image_url, generation, self._load_generation)
        else:
            self.text.setVisible(True)
            self._fragment_text_labels = []
            self._fragment_title_label = None
            self._fragment_title_text = ""
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
        family = self.READER_FONT_FAMILIES[self._reader_font_family][1]
        return f"""
            body {{ margin: 0; background: transparent; color: #222222; }}
            .bookstack-reader {{ color: #222222; font-family: {family}; }}
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
        # QLabel's wrapped height is calculated after its parent width changes.
        # Refresh after the layout pass so the body starts below the full header.
        QTimer.singleShot(0, self._refresh_reader_header_geometry)
        if getattr(self._source, "source_id", "") == "bookstack" and self._rich_body.isVisible():
            QTimer.singleShot(0, self._apply_bookstack_style)
            QTimer.singleShot(0, self._apply_bookstack_image_resources)
            QTimer.singleShot(0, self._resize_bookstack_body)

    def _refresh_reader_header_geometry(self) -> None:
        """Recalculate wrapped title rows after text or window width changes."""
        if not hasattr(self, "reader_header"):
            return
        self.book_title_label.updateGeometry()
        self.chapter_title_label.updateGeometry()
        self._reader_header_layout.activate()
        self.reader_header.setMinimumHeight(self._reader_header_layout.sizeHint().height())
        self.reader_header.updateGeometry()

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
        target = self._resolve_location_scroll(self._displayed_plain_text, valid_location, vbar.maximum()) if valid_location else None
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
        return self._build_location_snapshot(self._displayed_plain_text, pos, vbar.value(), vbar.maximum(), chapter_url)

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
        # 片段标签已被清掉（上面的循环），引用一并作废，避免重排时访问死指针
        self._fragment_text_labels = []
        self._fragment_title_label = None
        self._fragment_title_text = ""

    def set_show_images(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._show_images:
            return
        self._show_images = enabled
        if self._settings is not None:
            self._settings.set("reader", "show_images", enabled)
            self._settings.save()
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
            label.setMaximumWidth(self._content_width)
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
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
                width = min(
                    self._content_width,
                    max(100, self.scroll.viewport().width() - 20),
                )
                scaled = pixmap
                if pixmap.width() > width:
                    scaled = pixmap.scaledToWidth(width, Qt.SmoothTransformation)
                widget.setText("")
                widget.setAlignment(Qt.AlignHCenter)
                widget.setPixmap(scaled)
                return True
        return False

    def _on_chapter_loaded(self, ch, text, err, generation=None) -> None:
        if generation is not None and generation != self._load_generation:
            return
        if err:
            self._auto_loading = False  # 加载失败也要解锁，防死锁
            self._auto_prev_loading = False
            self._displayed_plain_text = ""
            self.text.setText(f"加载失败：{err}")
            return
        ch._cached_text = text
        if not hasattr(ch, "_cached_image_urls"):
            ch._cached_image_urls = []
        ch._images_attempted = True
        # 预取/离线路径可能直接带旧文本进来，先兜底解码再渲染
        text = self._ensure_decoded(ch, text)
        ch._cached_text = text
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
        self._font_scale = float(scale or 1.0)
        self._reader_font_size = 0
        self._base_font = self._clamp_font(round(17 * self._font_scale))
        self._apply_font()
        self._sync_settings_actions()

    def _adjust_font(self, delta: int) -> None:
        self._font_delta += delta
        self._apply_font()

    def _plain_body_css(self) -> str:
        size = self._clamp_font(self._base_font + self._font_delta)
        bg = self._reading_bg
        bg_css = f" background-color: {bg};" if bg else " background: transparent;"
        # Keep a concrete foreground color when a reading background is set;
        # otherwise Qt's palette handles light/dark application themes.
        fg_css = f" color: {self._reading_fg or 'palette(text)'};"
        family_css = self.READER_FONT_FAMILIES[self._reader_font_family][1]
        return (
            f"font-family: {family_css}; "
            f"font-size: {size}px; line-height: {self._line_spacing};"
            f" padding: 12px 20px;"
            # Indentation is materialized as ideographic spaces below;
            # CSS text-indent would double-indent fetched text.
            f" text-indent: 0; margin-bottom: {round(self._paragraph_spacing, 2)}em;"
            f"{bg_css}{fg_css}"
        )

    def _apply_plain_body_style(self) -> None:
        css = self._plain_body_css()
        # The settings menu is built before the body widgets. QAction.setChecked
        # can emit toggled during construction, so style application must be
        # harmless until the reader body exists.
        if not hasattr(self, "text") or not hasattr(self, "paged_label"):
            return
        widgets = [self.text, self.paged_label]
        widgets.extend(self.scroll_body.findChildren(QLabel, "readerBody"))
        seen = set()
        for widget in widgets:
            if id(widget) in seen:
                continue
            seen.add(id(widget))
            widget.setStyleSheet(css)
            widget.setMaximumWidth(self._content_width)
        self.scroll_layout.setAlignment(self.text, Qt.AlignHCenter)

    def _rich_text_enabled(self) -> bool:
        return self._line_spacing != 1.8 or not self._paragraph_indent

    # 正文行距兜底：用户没调行距（默认 1.8）时正文块用 2.0，中文长段落最省眼。
    READER_BODY_LINE_HEIGHT = 2.0

    def _reader_line_height(self) -> float:
        """当前正文行距（默认档位映射到更舒展的 2.0）。"""
        try:
            spacing = float(self._line_spacing)
        except (TypeError, ValueError):
            spacing = 0.0
        if not spacing or abs(spacing - 1.8) < 1e-6:
            return self.READER_BODY_LINE_HEIGHT
        return round(spacing, 2)

    def _indent_prefix(self) -> str:
        """首行缩进前缀（用全角空格 materialize，Qt 不认 CSS text-indent）。"""
        if not self._paragraph_indent:
            return ""
        return "　" * max(0, int(self._indent_width))

    def _reader_body_html(
        self, text: str, has_title: bool = True, noise_re=None,
        center_tokens=(),
    ) -> str:
        """通用正文排版（所有纯文本小说源共用）。

        这些源的正文都是「一段一行」的纯文本：整块丢进 QTextEdit 会挤成一片
        文字墙——段间没有留白、行距偏紧、章节标题混在正文里。这里统一按段落
        生成 <p> 块：段间距/行距/缩进/对齐/字间距全部跟随阅读设置
        （默认 段距 0.9em、行距 2.0、缩进两字、两端对齐、标准字距）；
        章节标题单独居中并拉开字距。字号/字体/颜色同样跟随阅读设置。

        noise_re：可选行过滤正则（站点广告/求票/域名提示行）。
        center_tokens：可选「居中短行」集合（如插图占位），命中则居中显示。
        """
        lines = self._format_reader_text(text, has_title=has_title).splitlines()
        size = self._clamp_font(self._base_font + self._font_delta)
        family = escape(self.READER_FONT_FAMILIES[self._reader_font_family][1], quote=True)
        line_height = self._reader_line_height()
        color = escape(self._reading_fg or "palette(text)", quote=True)
        para_spacing = round(float(self._paragraph_spacing), 2)
        align = "left" if self._text_align == "left" else "justify"
        letter = round(float(self._letter_spacing), 3)
        letter_css = f" letter-spacing:{letter}em;" if letter > 0 else ""
        blocks = []
        title_pending = bool(has_title)
        for raw in lines:
            line = str(raw).strip()
            if not line:
                continue
            content = escape(line)
            if title_pending:
                title_pending = False
                blocks.append(
                    '<div class="chapter-marker" style="text-align:center; '
                    'font-size:0.86em; letter-spacing:0.18em; color:palette(mid);'
                    f' margin:0 0 1.2em 0;">{content}</div>'
                )
                continue
            if noise_re is not None and noise_re.search(line):
                continue
            if center_tokens and line.strip("（）()【】[] ") in center_tokens:
                blocks.append(
                    '<div style="text-align:center; font-size:0.86em; '
                    f'color:palette(mid); margin:0.5em 0 1em 0;">{content}</div>'
                )
                continue
            # 缩进用「全角空格」materialize：Qt 的富文本导入不认 CSS
            # text-indent（实测 QTextBlockFormat.textIndent() 恒为 0），
            # 而 U+3000 在 white-space:pre-wrap 下稳定保留。
            indented = str(raw).rstrip()
            prefix = self._indent_prefix()
            if prefix:
                indented = prefix + indented.lstrip(" \t　")
            blocks.append(
                f'<p style="margin:0 0 {para_spacing}em 0; line-height:'
                f'{line_height}; text-align:{align};">{escape(indented)}</p>'
            )
        return (
            f'<div style="font-family:{family}; font-size:{size}px; '
            f'line-height:{line_height}; color:{color};'
            f'{letter_css} white-space:pre-wrap;">'
            + "".join(blocks)
            + "</div>"
        )

    def _qk17_reader_html(self, text: str, has_title: bool = True) -> str:
        """17K：一段一行 → 段落块（沿用通用排版）。"""
        return self._reader_body_html(text, has_title=has_title)

    def _set_reader_text(self, widget, text: str, has_title: bool = True) -> None:
        """正文一律按段落渲染（通用排版），源内差异只体现在过滤/居中规则上。"""
        source_id = getattr(self._source, "source_id", "")
        if source_id == "timotxt":
            html = self._timotxt_reader_html(text, has_title=has_title)
        elif source_id == "linovelib":
            html = self._linovelib_reader_html(text, has_title=has_title)
        else:
            html = self._reader_body_html(text, has_title=has_title)
        # 同内容重复渲染（调字号/切模式）时不必再写一遍文档
        if widget.text() != html:
            widget.setText(html)

    def _format_reader_text(self, text: str, has_title: bool = True) -> str:
        """Normalize fetched leading whitespace and apply the configured indent."""
        lines = str(text or "").split("\n")
        if getattr(self._source, "source_id", "") == "timotxt":
            lines = [
                line for line in lines
                if not re.fullmatch(r"[_=\-]{4,}", line.strip())
            ]
        prefix = self._indent_prefix()
        out = []
        for index, line in enumerate(lines):
            stripped = line.lstrip(" \t　")
            if stripped and prefix and (not has_title or index > 0):
                stripped = prefix + stripped
            out.append(stripped)
        return "\n".join(out)

    # 段首标记：两个全角空格（站点/上游规范化后的段落缩进）
    _MERGED_PARA_SPLIT_RE = re.compile(r"(?<=[。！？!?…”」』）)】])\s*(?=　　)")
    _BR_TAG_RE = re.compile(r"(?i)<\s*br\s*/?\s*>|</\s*p\s*>|</\s*div\s*>")
    # 句末标点：没有任何段标记时按句子聚合分段
    _SENTENCE_RE = re.compile(r"[^。！？!?…]*[。！？!?…”」』]+|[^。！？!?…]+$")
    # 无标记长文兜底分段：单段目标字数
    _PACK_MIN_CHARS = 110
    _PACK_MAX_CHARS = 190

    @classmethod
    def _pack_sentences(cls, line: str) -> str:
        """把整段「文字墙」按句子重新聚成段落（兜底，仅无段标记时使用）。"""
        sentences = [s for s in cls._SENTENCE_RE.findall(line) if s.strip()]
        if not sentences:
            return line
        out, buf = [], ""
        for sentence in sentences:
            if buf and len(buf) + len(sentence) > cls._PACK_MAX_CHARS:
                out.append(buf.strip())
                buf = sentence
                continue
            buf += sentence
            if len(buf) >= cls._PACK_MIN_CHARS:
                out.append(buf.strip())
                buf = ""
        if buf.strip():
            out.append(buf.strip())
        return "\n".join(out) if len(out) > 1 else line

    @classmethod
    def _split_merged_paragraphs(cls, text: str) -> str:
        """把「整章挤成一行」的正文重新切回段落（兜底）。

        正文选择器指向容器时，上游可能只给出一行（站点用 `<br>` 分段、
        `text_content()` 又把 `<br>` 吃掉），前端就会表现为「整章只有一段」。
        这里按 `<br>`/`</p>` 残留、段首「　　两个全角空格」找回段边界；两者
        都没有时按句末标点聚合成长度适中的段落。多行文本原样返回。
        """
        value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not value.strip():
            return value
        if sum(1 for line in value.split("\n") if line.strip()) > 1:
            return value
        # 先按显式标记（<br>/</p>、段首两个全角空格）找段边界：有标记就按标记切，
        # 与文本长短无关（短章节、对话体正文同样会被粘成一行）。
        with_breaks = cls._BR_TAG_RE.sub("\n", value)
        if sum(1 for line in with_breaks.split("\n") if line.strip()) > 1:
            return with_breaks
        parts = cls._MERGED_PARA_SPLIT_RE.split(with_breaks)
        if len(parts) > 1:
            merged = "\n".join(part.strip() for part in parts if part.strip())
            log.info("[reader] 正文为单行，按段首全角缩进拆成 %d 段", len(parts))
            return merged
        # 完全没有段标记：只有长文才值得按句子聚合（短文保持原样）
        if len(with_breaks) < 120:
            return with_breaks
        return cls._pack_sentences(with_breaks)

    def _prepare_reader_body(self, chapter, text: str) -> str:
        """Normalize TimoTXT's one-line-per-paragraph response for display."""
        converted = self._split_merged_paragraphs(self._reader_convert_text(text))
        if getattr(self._source, "source_id", "") == "linovelib":
            # 目录页/正文页首段常重复一次章节名（重排后会出现「【第1章】第1章 ...」
            # 的双标题），这里把与章节名相同的首行去掉。
            lines = str(converted or "").splitlines()
            while lines and not lines[0].strip():
                lines.pop(0)
            chapter_title = self._reader_convert_text(
                getattr(chapter, "title", "") or ""
            ).strip()
            if chapter_title and lines and lines[0].strip() == chapter_title:
                lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)
            return "\n".join(lines)
        if getattr(self._source, "source_id", "") != "timotxt":
            return converted
        lines = str(converted or "").splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        chapter_title = self._reader_convert_text(getattr(chapter, "title", "") or "")
        first = lines[0].strip() if lines else ""
        if chapter_title and first == chapter_title.strip():
            lines.pop(0)
        elif lines and re.match(r"^第\s*\d+\s*章(?:\s|$)", first):
            lines.pop(0)
        lines = [
            line for line in lines
            if not re.fullmatch(r"[_=\-]{4,}", line.strip())
        ]
        return "\n".join(lines)

    def _timotxt_reader_html(self, text: str, has_title: bool = True) -> str:
        """TimoTXT：段落块 + 稍大的段间距（沿用通用排版）。"""
        return self._reader_body_html(text, has_title=has_title)

    # 站点广告/求票/域名提示行（哔哩轻小说正文里混排的噪声）
    _LINOVELIB_NOISE_RE = re.compile(
        r"(哔哩轻小说|linovelib|请记住本站|最新网址|手机阅读|章节报错|加入书签"
        r"|投推荐票|本章未完|点击下一页|全文阅读)"
    )

    def _linovelib_reader_html(self, text: str, has_title: bool = True) -> str:
        """哔哩轻小说正文重排：通用段落排版 + 站点噪声过滤 + 插图居中。

        该站正文除段落外还混排广告/求票/域名行，插图位置只有「插图」占位文字，
        这两类交给通用排版的可选参数处理，段落/行距/缩进与其它源保持一致。
        """
        return self._reader_body_html(
            text,
            has_title=has_title,
            noise_re=self._LINOVELIB_NOISE_RE,
            center_tokens=("插图", "插圖", "illustration"),
        )

    def _apply_font(self) -> None:
        if not hasattr(self, "text") or not hasattr(self, "paged_label"):
            return
        self._apply_plain_body_style()
        self._rich_body.setMaximumWidth(self._content_width)
        if getattr(self._source, "source_id", "") == "bookstack":
            self._apply_bookstack_style()
            self._resize_bookstack_body()
        self._repaginate()
        if self._mode == "pager":
            self._pager_show_page(self._current_page)
        if self._displayed_plain_text:
            # 字号/行距/段距/缩进/字距都写在正文 HTML 里，改设置时按新样式重排一遍
            self._set_reader_text(self.text, self._displayed_plain_text)
        self._refresh_fragment_body()

    def _refresh_fragment_body(self) -> None:
        """按当前排版重写「片段渲染」里的正文标签。

        带图片的章节（哔哩轻小说/全本/timo/纵横等）走的是片段渲染：正文在
        若干独立 QLabel 里，`self.text` 是隐藏的。之前 `_apply_font` 只重排
        `self.text`，于是这些源的段间距/行距/首行缩进/字间距设置点了没反应。
        """
        labels = list(getattr(self, "_fragment_text_labels", []) or [])
        if not labels:
            return
        for widget, raw in labels:
            text = self._split_merged_paragraphs(self._reader_convert_text(raw))
            self._set_reader_text(widget, text, has_title=False)
            widget.setStyleSheet(self._plain_body_css())
            widget.setMaximumWidth(self._content_width)
        title_widget = getattr(self, "_fragment_title_label", None)
        if title_widget is not None:
            self._set_reader_text(
                title_widget, self._fragment_title_text or "", has_title=True
            )
            title_widget.setStyleSheet(self._plain_body_css())
            title_widget.setMaximumWidth(self._content_width)
        self.scroll_body.adjustSize()

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
    def _set_mode(self, mode: str) -> None:
        """切换滚动/翻页阅读方式，并同步工具栏与设置菜单状态。"""
        if mode not in {"scroll", "pager"}:
            return
        self._stop_auto_scroll()
        if mode == self._mode:
            self._sync_settings_actions()
            self._update_auto_scroll_slider_state()
            return
        self._mode = mode
        if mode == "pager":
            self.mode_btn.setText("滚动模式")
            self.body_stack.setCurrentWidget(self.pager_widget)
            self._repaginate()
            self._pager_show_page(self._current_page)
        else:
            self.mode_btn.setText("翻页模式")
            self.body_stack.setCurrentWidget(self.scroll)
            self.scroll.verticalScrollBar().setValue(0)
        self._sync_settings_actions()
        self._update_auto_scroll_slider_state()

    def _toggle_mode(self) -> None:
        """滚动 / 翻页 模式切换。"""
        self._set_mode("pager" if self._mode == "scroll" else "scroll")

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
        self._set_reader_text(
            self.paged_label, self._pages[page], has_title=(page == 0)
        )
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


    def _update_reader_header(self, chapter=None, label: str = "") -> None:
        """Update the fixed reader header without rebuilding the reading body."""
        detail_title = getattr(self._detail, "title", "") if self._detail is not None else ""
        detail_title = self._reader_convert_text(detail_title)
        self.book_title_label.setText(str(detail_title or "未命名作品"))
        self.book_title_label.setToolTip(str(detail_title or ""))
        if chapter is None and 0 <= self._current_idx < len(self._chapters):
            chapter = self._chapters[self._current_idx]
        chapter_title = str(getattr(chapter, "title", "") or "") if chapter is not None else ""
        chapter_title = self._reader_convert_text(chapter_title)
        if not label:
            from framework.content import chapter_label

            label = chapter_label(chapter_title) if chapter_title else ""
        display = chapter_title or self._reader_convert_text(label) or (
            f"第{self._current_idx + 1}章" if self._current_idx >= 0 else ""
        )
        self.chapter_title_label.setText(f"当前章节：{display}" if display else "")
        self.chapter_title_label.setToolTip(display)
        self._refresh_reader_header_geometry()

    def _update_progress(self) -> None:
        total = len(self._chapters)
        self.progress_label.setText(f"第{self._current_idx + 1}/{total}章")
        self._update_reader_header()

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
