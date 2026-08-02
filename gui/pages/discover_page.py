"""发现界面（DiscoverPage）。

对应 ui-discover.md：
- 顶部源选择器：只列配置了 endpoints.discovery 的源
- 分类折叠栏：横向滚动，默认折叠；点「展开分类」进完整分类视图
- 作品网格：多列卡片流 + 懒加载滚动（封面异步加载）
- 详情抽屉：点作品拉详情显示右侧抽屉
- 全量抓取：确认弹窗 + 进度 + JSON 索引
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QThreadPool, QRunnable, QObject
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from framework.discovery import Discovery, Work
from framework.content import Content
from framework.bulk_fetch import BulkFetch
from framework.events import EventBus
from framework.source_manager import SourceManager
from framework.theme_manager import ThemeManager

from gui.components import WorkCard, DetailDrawer
from .base_page import BasePage

# 分类折叠栏默认显示的按钮数（其余收起，点展开显示全部）
COLLAPSED_CATEGORY_COUNT = 6
# 作品网格列数
GRID_COLUMNS = 5


class DiscoverPage(BasePage):
    # 对外信号：开始阅读 → 跳阅读器 Tab（App 层接）
    read_requested = Signal(object)

    def __init__(
        self,
        source_manager: SourceManager,
        discovery: Discovery,
        content: Content,
        bulk_fetch: BulkFetch,
        event_bus: EventBus,
        theme_manager: ThemeManager,
        parent=None,
    ):
        super().__init__(parent)
        self._manager = source_manager
        self._discovery = discovery
        self._content = content
        self._bulk_fetch = bulk_fetch
        self._bus = event_bus
        self._theme_manager = theme_manager
        self._current_source = None
        self._current_page = 0
        self._loading = False
        self._has_more = True
        self._current_cat_url = None
        self._work_count = 0
        self._preload_limit = 3  # 最多预加载到第 3 页，防无限翻页
        self._cat_buttons: list = []
        self._cat_collapsed = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # ---- 顶部：源选择器 + 抓取全部 ----
        top = QHBoxLayout()
        self.source_combo = QComboBox()
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        top.addWidget(self.source_combo, stretch=1)

        self.bulk_btn = QPushButton("抓取全部")
        self.bulk_btn.setFixedWidth(90)
        self.bulk_btn.clicked.connect(self._on_bulk_fetch)
        top.addWidget(self.bulk_btn)
        layout.addLayout(top)

        # ---- 分类折叠栏：横向滚动容器 + 展开按钮 ----
        cat_frame = QFrame()
        cat_frame.setObjectName("catBar")
        cat_layout = QVBoxLayout(cat_frame)
        cat_layout.setContentsMargins(8, 4, 8, 4)
        cat_layout.setSpacing(2)

        cat_scroll_row = QHBoxLayout()
        cat_scroll_row.setSpacing(4)

        self.cat_scroll = QScrollArea()
        self.cat_scroll.setWidgetResizable(True)
        self.cat_scroll.setFixedHeight(44)
        self.cat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.cat_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        cat_scroll_row.addWidget(self.cat_scroll, stretch=1)
        self._setup_cat_scroll_drag()

        self.cat_toggle_btn = QPushButton("展开分类 ▾")
        self.cat_toggle_btn.setFixedWidth(90)
        self.cat_toggle_btn.clicked.connect(self._toggle_categories)
        cat_scroll_row.addWidget(self.cat_toggle_btn)

        cat_layout.addLayout(cat_scroll_row)
        layout.addWidget(cat_frame)

        # 分类按钮容器（横向排列）
        self.cat_container = QWidget()
        self.cat_bar = QHBoxLayout(self.cat_container)
        self.cat_bar.setContentsMargins(0, 0, 0, 0)
        self.cat_bar.setSpacing(6)
        self.cat_scroll.setWidget(self.cat_container)

        # ---- 主体：作品网格（懒加载） + 右侧详情抽屉 ----
        body = QHBoxLayout()
        body.setSpacing(12)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body.addWidget(self.scroll, stretch=1)

        self.list_container = QWidget()
        self.grid_layout = QGridLayout(self.list_container)
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_layout.setSpacing(12)
        self.scroll.setWidget(self.list_container)

        # 详情抽屉
        self.detail_drawer = DetailDrawer()
        self.detail_drawer.read_requested.connect(self._on_read)
        self.detail_drawer.open_url_requested.connect(self._on_open_url)
        self.detail_drawer.download_requested.connect(self._on_download)
        body.addWidget(self.detail_drawer)

        layout.addLayout(body, stretch=1)

        # 滚动到底触发
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

        # 底部状态
        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color: palette(dark); padding: 8px;")
        layout.addWidget(self.status_label)

        self._reload_sources()

    # ------------------------------------------------------------------ #
    def _setup_cat_scroll_drag(self) -> None:
        """分类栏支持鼠标拖拽横向平移。"""
        viewport = self.cat_scroll.viewport()
        self._drag_pressed = False
        self._drag_last_x = 0

        def on_press(event):
            self._drag_pressed = True
            self._drag_last_x = event.position().x()
            viewport.setCursor(Qt.ClosedHandCursor)

        def on_move(event):
            if self._drag_pressed:
                dx = event.position().x() - self._drag_last_x
                hbar = self.cat_scroll.horizontalScrollBar()
                hbar.setValue(hbar.value() - int(dx))
                self._drag_last_x = event.position().x()

        def on_release(event):
            self._drag_pressed = False
            viewport.setCursor(Qt.ArrowCursor)

        viewport.mousePressEvent = on_press
        viewport.mouseMoveEvent = on_move
        viewport.mouseReleaseEvent = on_release

    def _reload_sources(self) -> None:
        """只列配置了 discovery 的源。"""
        self.source_combo.clear()
        sources = self._manager.discoverable_sources()
        for s in sources:
            label = f"{s.source_name} ({s.content_type})"
            self.source_combo.addItem(label, s)
        if sources:
            self.source_combo.setCurrentIndex(0)
        else:
            self.status_label.setText("没有可浏览的源：请在源管理中添加配置了发现规则的源")
            self._show_empty()

    def _on_source_changed(self, index: int) -> None:
        if index < 0:
            return
        self._current_source = self.source_combo.itemData(index)
        self._current_page = 0
        self._has_more = True
        self._current_cat_url = None
        self._cat_collapsed = True
        self._load_categories()
        self._reset_works()

    # ------------------------------------------------------------------ #
    def _clear_cat_buttons(self) -> None:
        while self.cat_bar.count():
            child = self.cat_bar.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._cat_buttons = []

    def _load_categories(self) -> None:
        """加载分类按钮到折叠栏。未配置分类 → 直接进作品列表。"""
        self._clear_cat_buttons()
        if self._current_source is None:
            return
        try:
            cats = self._discovery.list_categories(self._current_source)
        except Exception as exc:
            self.status_label.setText(f"分类加载失败：{exc}")
            return

        if not cats:
            # 无分类 → 用作品列表入口
            disc = self._current_source.get_discovery_config()
            self._current_cat_url = (
                disc.get("works_list_url")
                or disc.get("list_url")
                or self._current_source.base_url
            )
            self.cat_toggle_btn.setVisible(False)
            return

        self.cat_toggle_btn.setVisible(True)
        disc = self._current_source.get_discovery_config()
        all_url = (
            disc.get("works_list_url")
            or disc.get("list_url")
            or self._current_source.base_url
        )
        # 全部按钮
        all_btn = self._make_cat_button("全部", None, True)
        self._cat_buttons.append((all_btn, None))
        for cat in cats:
            btn = self._make_cat_button(cat.title, cat, False)
            self._cat_buttons.append((btn, cat))

        self._refresh_cat_buttons(all_url)

    def _make_cat_button(self, text, cat, checked) -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setChecked(checked)
        btn.setStyleSheet("padding: 4px 12px; font-size: 12px; border-radius: 6px;")
        btn.clicked.connect(lambda _, c=cat, b=btn: self._on_category(c, b))
        return btn

    def _refresh_cat_buttons(self, all_url: str) -> None:
        """按折叠状态显示前 N 个或全部分类按钮。"""
        while self.cat_bar.count():
            child = self.cat_bar.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        visible = (
            self._cat_buttons
            if not self._cat_collapsed
            else self._cat_buttons[:COLLAPSED_CATEGORY_COUNT + 1]
        )
        for btn, _ in visible:
            self.cat_bar.addWidget(btn)
        self.cat_bar.addStretch(1)

        # 更新展开按钮文字
        if self._cat_collapsed and len(self._cat_buttons) > COLLAPSED_CATEGORY_COUNT + 1:
            self.cat_toggle_btn.setText(f"展开分类 ▾ ({len(self._cat_buttons)-1})")
        else:
            self.cat_toggle_btn.setText("收起分类 ▴")
        # 默认选中"全部"
        self._current_cat_url = all_url

    def _toggle_categories(self) -> None:
        """展开/收起分类按钮。"""
        self._cat_collapsed = not self._cat_collapsed
        disc = self._current_source.get_discovery_config()
        all_url = (
            disc.get("works_list_url")
            or disc.get("list_url")
            or self._current_source.base_url
        )
        self._refresh_cat_buttons(all_url)

    def _on_category(self, cat, btn) -> None:
        """点分类按钮：单选互斥 + 加载该分类作品。"""
        for b, _ in self._cat_buttons:
            if b is not btn:
                b.setChecked(False)
        btn.setChecked(True)
        disc = self._current_source.get_discovery_config()
        if cat is None:
            self._current_cat_url = (
                disc.get("works_list_url")
                or disc.get("list_url")
                or self._current_source.base_url
            )
        else:
            self._current_cat_url = cat.url
        self._reset_works()

    # ------------------------------------------------------------------ #
    def _reset_works(self) -> None:
        """清空网格，重新从第 1 页加载。"""
        self._clear_works()
        self._current_page = 0
        self._has_more = True
        self._work_count = 0
        self._load_next_page()

    def _clear_works(self) -> None:
        while self.grid_layout.count():
            child = self.grid_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._work_count = 0

    def _load_next_page(self) -> None:
        """异步加载下一页：后台线程抓作品，加载中显示状态，完成后更新网格。"""
        if self._loading or not self._has_more or self._current_source is None:
            return
        if self._current_cat_url is None:
            return
        self._loading = True
        self._current_page += 1
        page = self._current_page
        source = self._current_source
        cat_url = self._current_cat_url
        discovery = self._discovery

        self.status_label.setText("正在加载第 %d 页..." % page)
        self.status_label.setVisible(True)

        # 后台线程抓取
        thread_pool = QThreadPool.globalInstance()
        runnable = _FetchWorksTask(discovery, source, cat_url, page)
        runnable.signals.finished.connect(
            lambda works, err: self._on_page_loaded(works, err, page)
        )
        self._fetch_task = runnable  # 持有引用，防止被 GC
        thread_pool.start(runnable)

    def _on_page_loaded(self, works, err, page: int) -> None:
        """后台抓取完成，更新 UI（回到主线程）。"""
        self._loading = False
        if err:
            self._has_more = False
            self.status_label.setText(f"加载失败：{err}")
            return
        if not works:
            self._has_more = False
            self.status_label.setText("到底啦～")
        else:
            self._append_works(works)
            self.status_label.setText(f"已加载 {self._current_page} 页 · 共 {self._work_count} 部")
        # 自动继续加载下一页直到填满视口（预加载）
        self._maybe_preload()

    def _maybe_preload(self) -> None:
        """内容未填满视口时预加载，但最多额外预加载 PRELOAD_LIMIT 页（防失控）。"""
        if self._loading or not self._has_more:
            return
        preloaded = self._current_page
        if preloaded >= self._preload_limit:
            return
        if self.scroll.verticalScrollBar().maximum() < self.scroll.height():
            self._load_next_page()

    def _append_works(self, works) -> None:
        """把作品卡片按网格排列（每行 GRID_COLUMNS 个）。"""
        for w in works:
            row, col = divmod(self._work_count, GRID_COLUMNS)
            card = WorkCard(w)
            card.clicked.connect(self._on_work_clicked)
            self.grid_layout.addWidget(card, row, col)
            self._work_count += 1

    # ------------------------------------------------------------------ #
    def _on_work_clicked(self, work: Work) -> None:
        """点作品 → 拉详情 → 显示右侧抽屉。"""
        self.status_label.setText(f"加载详情：{work.title}")
        source = self._manager.get(work.source_id) if work.source_id else self._current_source
        if source is None:
            return
        try:
            detail = self._content.fetch_detail(source, work.url)
        except Exception as exc:
            self.status_label.setText(f"详情加载失败：{exc}")
            return
        self.detail_drawer.show_detail(detail)
        self.status_label.setText("")

    def _on_bulk_fetch(self) -> None:
        """全量抓取：确认弹窗 → 后台执行 → 进度。"""
        from PySide6.QtWidgets import QMessageBox

        if self._current_source is None:
            return
        source = self._current_source
        ret = QMessageBox.question(
            self,
            "确认全量抓取",
            f"将遍历「{source.source_name}」全部分类/列表页，"
            f"建立作品元数据索引（不下载正文，默认最多 20 页/分类）。\n\n继续吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return

        self.status_label.setText("全量抓取中...")
        self.bulk_btn.setEnabled(False)
        try:
            stats = self._bulk_fetch.fetch_all(source)
            self.status_label.setText(
                f"全量抓取完成：{stats['categories']} 个分类，{stats['works']} 部作品"
            )
        except Exception as exc:
            self.status_label.setText(f"全量抓取失败：{exc}")
        finally:
            self.bulk_btn.setEnabled(True)

    def _on_read(self, detail) -> None:
        """开始阅读 → 跳阅读器 Tab（占位）。"""
        self.read_requested.emit(detail)

    def _on_open_url(self, url: str) -> None:
        """打开源详情页（浏览器）。"""
        import webbrowser

        webbrowser.open(url)

    def _on_download(self, detail) -> None:
        """下载 → 占位提示（下载界面后续实现）。"""
        self.status_label.setText(f"[下载待实现] {detail.title}")

    def _on_scroll(self, value: int) -> None:
        """滚动接近底部（阈值 200px）触发加载下一页。"""
        vbar = self.scroll.verticalScrollBar()
        if value >= vbar.maximum() - 200:
            self._load_next_page()

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self._reload_sources()

    def _show_empty(self) -> None:
        self._clear_works()


class _FetchWorkerSignals(QObject):
    """后台抓取任务信号。"""

    finished = Signal(object, object)  # (works, err)


class _FetchWorksTask(QRunnable):
    """后台抓取一页作品（QThreadPool）。"""

    def __init__(self, discovery, source, cat_url, page):
        super().__init__()
        self.signals = _FetchWorkerSignals()
        self._discovery = discovery
        self._source = source
        self._cat_url = cat_url
        self._page = page

    def run(self) -> None:
        works, err = [], None
        try:
            works = self._discovery.list_works(self._source, self._cat_url, self._page)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        try:
            self.signals.finished.emit(works, err)
        except RuntimeError:
            pass  # 页面已销毁，忽略信号
