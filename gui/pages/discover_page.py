"""发现界面（DiscoverPage）。

对应 ui-discover.md：
- 顶部源选择器：只列配置了 endpoints.discovery 的源
- 分类折叠栏：横向滚动，默认折叠；点「展开分类」进完整分类视图
- 作品网格：多列卡片流 + 懒加载滚动（封面异步加载）
- 详情抽屉：点作品拉详情显示右侧抽屉
- 全量抓取：确认弹窗 + 进度 + JSON 索引
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

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

# 反爬挑战错误特征：错误文本（小写）命中任一 → 判定被站点反爬挑战拦截
# （Cloudflare「Just a moment」Turnstile / WAF 403 / AntiScrapeError 等），
# 用于停止对同一源的无脑自动重试（持续轰炸反而加剧 Cloudflare 拦截）。
CHALLENGE_MARKERS = (
    "反爬",
    "blocked",
    "just a moment",
    "attention required",
    "captcha",
    "验证",
    "challenge",
    "http 403",
)


class DiscoverPage(BasePage):
    # 对外信号：开始阅读 → 跳阅读器 Tab（App 层接）
    read_requested = Signal(object)
    # 对外信号：下载 → App 层弹章节范围对话框并入队
    download_requested = Signal(object)
    # 对外信号：收藏/取消收藏 → App 层写书架收藏库
    favorite_requested = Signal(object)

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
        self._has_more = True
        self._current_cat_url = None
        self._work_count = 0
        self._source_epoch = 0  # 源切换序号，防止旧请求回调竞态
        self._preload_ahead = 2  # 预加载缓冲深度：第 1 页后最多再预加载 2 页，防一次拉太多
        self._active_pages: set = set()  # 正在抓取的页码（防重复请求）
        self._loaded_pages: set = set()  # 已完成且有数据的页码
        self._page_tasks: list = []  # 多页并发任务引用（防 GC）
        self._cover_tasks: list = []  # 封面恢复任务持有（防 GC，逐页覆盖引用会丢早任务）
        self._backfill_pool = QThreadPool(self)  # 详情封面回填专用池（限流，≤3 并发）
        self._backfill_pool.setMaxThreadCount(3)
        self._cover_backfill_tasks: list = []  # 详情封面回填任务持有（防 GC）
        self._cover_backfill_submitted: set = set()  # 已提交回填的 url（防逐页/重排重复）
        self._cat_buttons: list = []
        self._cat_collapsed = True
        self._cat_bar_populated = False
        self._works: list = []  # 已加载作品全量（resize 自适应列宽时重排用）
        self._seen_urls: set = set()  # 已出现作品 URL（跨页去重，防分页异常重复显示）
        self._last_columns = 0
        # 反爬挑战拦截：源级计数与停止标记（连续 ≥2 次命中挑战 → 停自动重试）
        self._challenge_failures: dict = {}  # source_id → 连续挑战拦截失败次数
        self._challenge_blocked_sources: set = set()  # 已因挑战拦截停止自动重试的源

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # ---- 顶部：类型筛选 + 源选择器 + 抓取全部 ----
        top = QHBoxLayout()
        self.type_filter = QComboBox()
        self.type_filter.addItem("全部类型", "")
        self.type_filter.addItem("小说", "novel")
        self.type_filter.addItem("漫画", "comic")
        self.type_filter.addItem("视频", "video")
        self.type_filter.setFixedWidth(96)
        self.type_filter.currentIndexChanged.connect(self._on_type_filter_changed)
        top.addWidget(self.type_filter)

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
        # 高度 60：给横向滚动条（分类多时弹出）预留空间，避免遮住分类按钮文字
        self.cat_scroll.setFixedHeight(60)
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
        self.detail_drawer.favorite_requested.connect(self._on_favorite)
        body.addWidget(self.detail_drawer)

        # 点作品区空白处 → 关闭详情抽屉
        self.scroll.viewport().installEventFilter(self)
        self._close_drawer_on_outside_click = True

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

    def _on_type_filter_changed(self, idx: int) -> None:
        """类型筛选变化 → 按类型重建源列表（数据源分类显示）。"""
        self._reload_sources()

    def _reload_sources(self) -> None:
        """只列配置了 discovery 的源（避免多次触发切换）。加健康灯前缀。

        type_filter 筛选：只列当前所选类型（全部/小说/漫画/视频）的源。
        """
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        ftype = self.type_filter.currentData() if hasattr(self, "type_filter") else ""
        sources = self._manager.discoverable_sources()
        if ftype:
            sources = [s for s in sources if s.content_type == ftype]
        for s in sources:
            label = f"{self._health_icon(s.source_id)} {s.source_name} ({s.content_type})"
            self.source_combo.addItem(label, s)
            # 下拉项 ToolTip：健康灯 + 错误详情（若失效）
            health = self._manager.get_health(s.source_id)
            tip = self._health_tip(s, health)
            idx = self.source_combo.count() - 1
            self.source_combo.setItemData(idx, tip, Qt.ToolTipRole)
        if sources:
            self.source_combo.setCurrentIndex(0)
        self.source_combo.blockSignals(False)
        if not sources:
            # 该类型无可用源：清空网格与分类，避免残留旧源内容
            self.status_label.setText("该类型暂无可用源")
            self._clear_works()
            self._clear_cat_buttons()
            return
        # 手动触发一次源切换
        self._on_source_changed(self.source_combo.currentIndex())

    def _health_icon(self, source_id: str) -> str:
        """源健康灯图标：🟢正常 🟠软失败 🔴失效。"""
        from framework.source_manager import HEALTH_OK, HEALTH_WARN, HEALTH_BROKEN

        state = self._manager.get_health(source_id).state
        return {HEALTH_OK: "🟢", HEALTH_WARN: "🟠", HEALTH_BROKEN: "🔴"}.get(state, "⚪")

    def _health_tip(self, source, health) -> str:
        """健康 ToolTip：状态 + 错误详情。"""
        from framework.source_manager import HEALTH_OK, HEALTH_WARN, HEALTH_BROKEN

        state = health.state
        label = {HEALTH_OK: "正常", HEALTH_WARN: "自检软失败", HEALTH_BROKEN: "失效"}.get(state, state)
        tip = f"{source.source_name}\n健康状态：{label}"
        if health.last_error:
            tip += f"\n{health.last_error}"
        return tip

    def _on_source_changed(self, index: int) -> None:
        if index < 0:
            return
        # 递增序号：使旧源的加载回调失效（防止快速切换竞态/卡死）
        self._source_epoch += 1
        self._current_source = self.source_combo.itemData(index)
        self._current_page = 0
        self._has_more = True
        self._current_cat_url = None
        self._cat_collapsed = True
        self.status_label.setText("正在加载分类...")
        # 立即清空旧源的作品网格，避免换源瞬间旧内容残留/溢出
        self._clear_works()
        self._clear_cat_buttons()
        self._load_categories()
        # 作品加载在 _on_categories_loaded 分类就绪后触发

    # ------------------------------------------------------------------ #
    def _clear_cat_buttons(self) -> None:
        while self.cat_bar.count():
            child = self.cat_bar.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._cat_buttons = []
        self._cat_bar_populated = False

    def _load_categories(self) -> None:
        """后台加载分类（不阻塞 UI）。"""
        self._clear_cat_buttons()
        if self._current_source is None:
            return
        epoch = self._source_epoch
        task = _FetchCategoriesTask(self._discovery, self._current_source)
        task.signals.finished.connect(
            lambda cats, err, e=epoch: self._on_categories_loaded(cats, err, e)
        )
        self._cat_task = task  # 持有引用，防止被 GC（信号才可靠）
        QThreadPool.globalInstance().start(task)

    def _on_categories_loaded(self, cats, err, epoch: int) -> None:
        """分类加载完成。若源已切换（epoch 不符）则丢弃。"""
        if epoch != self._source_epoch:
            return  # 旧源请求，丢弃
        if err:
            # 分类加载失败（网络/站点改版）→ 用作品列表入口兜底加载，
            # 避免出现"必须点一下分类才出作品"的空窗
            if self._mark_challenge_blocked(self._current_source, err):
                return  # 被站点挑战拦截：停止本次会话加载并提示
            disc = self._current_source.get_discovery_config()
            self._current_cat_url = (
                disc.get("works_list_url")
                or disc.get("list_url")
                or self._current_source.base_url
            )
            self.cat_toggle_btn.setVisible(False)
            self.status_label.setText(f"分类加载失败：{err}，正在加载作品...")
            self._reset_works()
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
            self._reset_works()
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
        # 分类就绪后加载作品
        self._reset_works()

    def _make_cat_button(self, text, cat, checked) -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setChecked(checked)
        # min-height 26：横向滚动条弹出时按钮文字不被压住
        btn.setStyleSheet(
            "padding: 4px 12px; font-size: 12px; border-radius: 6px; min-height: 26px;"
        )
        btn.clicked.connect(lambda _, c=cat, b=btn: self._on_category(c, b))
        return btn

    def _refresh_cat_buttons(self, all_url: str) -> None:
        """按折叠状态显示前 N 个或全部分类按钮。

        按钮对象一次性建好，折叠/展开只切换可见性（不重建，避免"消失"）。
        """
        # 若按钮已加入布局，直接切换可见性
        if self._cat_bar_populated:
            self._apply_visibility()
        else:
            self._populate_cat_bar()
            self._apply_visibility()

        # 更新展开按钮文字
        if self._cat_collapsed and len(self._cat_buttons) > COLLAPSED_CATEGORY_COUNT + 1:
            self.cat_toggle_btn.setText(f"展开分类 ▾ ({len(self._cat_buttons)-1})")
        else:
            self.cat_toggle_btn.setText("收起分类 ▴")
        # 默认选中"全部"
        self._current_cat_url = all_url

    def _populate_cat_bar(self) -> None:
        """把全部按钮加入布局（只做一次）。"""
        for btn, _ in self._cat_buttons:
            self.cat_bar.addWidget(btn)
        self.cat_bar.addStretch(1)
        self._cat_bar_populated = True

    def _apply_visibility(self) -> None:
        """按折叠状态隐藏/显示按钮。"""
        for idx, (btn, _) in enumerate(self._cat_buttons):
            visible = idx <= COLLAPSED_CATEGORY_COUNT or not self._cat_collapsed
            btn.setVisible(visible)

    def _toggle_categories(self) -> None:
        """展开/收起分类按钮（只切换状态，不重建）。"""
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
        """清空网格，首屏并发加载第 1 页 + 预加载缓冲页（快速填满视口）。

        首屏并发爬 1+缓冲页（_preload_ahead），谁先完成先显示（_on_page_loaded
        边收边渲染）——比等第 1 页完成再串行补预加载更快。其余靠 80% 滚动
        懒加载（_on_scroll 保留）。封面解密/恢复已独立 Session + 任务列表
        持有，多页并发不丢封面；CoverLoader 全局限流防封面风暴。
        """
        self._clear_works()
        self._current_page = 0
        self._has_more = True
        self._work_count = 0
        self._active_pages = set()
        self._loaded_pages = set()
        self._page_tasks = []
        self._cover_tasks = []  # 换源/切分类时清空旧封面恢复任务
        self._cover_backfill_tasks = []  # 换源/切分类时清空旧回填任务引用
        self._cover_backfill_submitted = set()  # 新一轮会话可重新提交回填
        self._source_epoch += 1
        # 换源/重选分类：解除该源挑战拦截标记，允许用户手动重新触发
        if self._current_source is not None:
            sid = self._current_source.source_id
            self._challenge_blocked_sources.discard(sid)
            self._challenge_failures.pop(sid, None)
        for p in range(1, 1 + self._preload_ahead + 1):
            self._load_next_page(page=p)

    def _clear_works(self) -> None:
        while self.grid_layout.count():
            child = self.grid_layout.takeAt(0)
            w = child.widget()
            if w is not None:
                w.setParent(None)  # 立即解除父子关系（防换源闪旧内容）
                w.deleteLater()
        self._work_count = 0
        self._works = []
        self._seen_urls = set()

    def _clear_grid_widgets(self) -> None:
        """只清空网格卡片（保留 self._works 数据）。

        供需要重排列数、但不丢弃已加载数据的场景（_reflow）使用。
        区别于 _clear_works（后者连 _works 一起清空，用于换源/切分类）。
        """
        while self.grid_layout.count():
            child = self.grid_layout.takeAt(0)
            w = child.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._work_count = 0

    def _is_challenge_blocked(self, err_text) -> bool:
        """错误文本是否命中反爬挑战特征（Cloudflare 验证 / WAF 403 等）。

        只用于失败路径判定：命中任一特征即认为该请求被站点挑战拦截，
        从而停止对同一源的无脑自动重试（避免轰炸反而加剧 Cloudflare 拦截）。
        普通网络超时/404 等错误不含这些特征，不进入拦截判定。
        """
        if not err_text:
            return False
        low = str(err_text).lower()
        return any(marker in low for marker in CHALLENGE_MARKERS)

    def _mark_challenge_blocked(self, source, err) -> bool:
        """连续 ≥2 次挑战拦截 → 标记该源停止自动重试（返回 True）。

        首次命中只累计计数（保留一次重试观察）；连续再次命中才真正停止。
        被标记的源在换源/重选分类（_reset_works）时解除，允许用户手动重试。
        """
        if source is None or not self._is_challenge_blocked(err):
            return False
        sid = source.source_id
        count = self._challenge_failures.get(sid, 0) + 1
        self._challenge_failures[sid] = count
        if count < 2:
            return False  # 首次命中不立即停，再重试一次确认
        self._challenge_blocked_sources.add(sid)
        self.status_label.setText(
            f"{source.source_name} 被站点反爬拦截（Cloudflare 验证），"
            "已停止自动重试。请稍后再试或换源。"
        )
        self.status_label.setVisible(True)
        return True

    def _is_challenge_stopped(self) -> bool:
        """当前源是否已因挑战拦截停止自动重试（调度页面前先查）。"""
        return (
            self._current_source is not None
            and self._current_source.source_id in self._challenge_blocked_sources
        )

    def _load_next_page(self, page: int = -1) -> None:
        """异步加载某一页（后台线程）。page 为 -1 时自动取下一页（滚动加载用）。

        去掉 _loading 互斥锁：多页并发加载时每页独立追踪，_active_pages
        防重复请求；_on_page_loaded 按 epoch 防旧源/旧分类结果污染。
        """
        if self._current_source is None or self._current_cat_url is None:
            return
        # 该源被站点反爬挑战拦截 → 本次会话停止调度任何新页面（不再轰炸）
        if self._is_challenge_stopped():
            return
        if page < 1:
            # 滚动加载：取下一个未请求页 = 已请求最大页 + 1。
            # 关键修复：不从 1 重扫（旧逻辑会跳过 active 中的页，滚动条
            # 范围因内容加载变化时，一次触发序列会连环加载 N+1/N+2/N+3，
            # 误加载好多页）。现在 page 固定，已在加载的页被 _active_pages 挡掉。
            requested_max = max(self._active_pages | self._loaded_pages, default=0)
            page = requested_max + 1
            loaded_max = max(self._loaded_pages, default=0)
            if page > loaded_max + self._preload_ahead:
                return  # 预加载缓冲已到位，等用户继续滚动
        if page in self._active_pages or page in self._loaded_pages:
            return  # 已请求或已加载
        if not self._has_more and page > max(self._loaded_pages or [0]):
            return  # 没更多页了

        self._active_pages.add(page)
        self._current_page = max(self._current_page, page)
        epoch = self._source_epoch

        self.status_label.setText(f"正在加载第 {page} 页...")
        self.status_label.setVisible(True)

        runnable = _FetchWorksTask(self._discovery, self._current_source, self._current_cat_url, page)
        runnable.signals.finished.connect(
            lambda works, err, e=epoch, p=page: self._on_page_loaded(works, err, p, e)
        )
        self._page_tasks.append(runnable)  # 持有引用防 GC
        QThreadPool.globalInstance().start(runnable)

    def _on_page_loaded(self, works, err, page: int, epoch: int) -> None:
        """后台抓取完成，立即追加到网格（多页并发，第1页秒出）。

        epoch 不匹配 → 整页丢弃（用户已换源/切分类，这是旧请求的回调）。
        """
        if epoch != self._source_epoch:
            return
        self._active_pages.discard(page)
        if err:
            # 单页失败不标记 _has_more=False（其他页可能成功，继续加载）
            log.warning("[discover] 第 %d 页失败：%s", page, err)
            # 挑战拦截：连续命中即停止该源自动重试并提示；否则保持原有重试
            if self._mark_challenge_blocked(self._current_source, err):
                return
            self._maybe_preload()
            return
        if not works:
            if page > max(self._loaded_pages or [0]) + 1:
                pass  # 不是连续末页（中间有空洞）→ 不标记无更多
            else:
                self._has_more = False
            self._maybe_preload()
            return
        self._loaded_pages.add(page)
        self._append_works(works)
        self.status_label.setText(
            f"已加载 {len(self._loaded_pages)} 页 · 共 {self._work_count} 部"
        )
        # 封面异步恢复（AES 解密 / Playwright 渲染）
        if self._needs_cover_recovery(works):
            self._start_cover_recovery(epoch, page, page_works=works)
        # 详情封面回填（cover_backfill 源：列表纯文本无封面，后台按需抓详情封面）
        if self._cover_backfill_enabled():
            self._submit_cover_backfill(epoch, page, page_works=works)
        # 视口未填满 → 继续补足（并发加载）
        self._maybe_preload()

    def _needs_cover_recovery(self, works) -> bool:
        """检查这批作品是否需要封面恢复（AES 加密 或 Playwright 渲染站）。"""
        return any(
            getattr(w, "_needs_cover_recovery", False)
            or getattr(w, "_needs_cover_decrypt", False)
            for w in works
        )

    def _start_cover_recovery(self, epoch: int, page: int, page_works=None) -> None:
        """后台恢复当前页封面，完成后刷新卡片封面图。

        - AES 加密站（18mh 类）：_needs_cover_decrypt → 下载字节+AES 解密成 data URI
        - Playwright 渲染站（分片加密）：_needs_cover_recovery → 整页渲染恢复

        page: 本批作品对应的页码（并发多页时不再依赖 _current_page，防竞态）。
        page_works: 本次加载的那一批作品（page 粒度），避免传全量导致
        恢复页与实际作品不对齐（翻页后新页作品不在恢复页里）。
        """
        source = self._current_source
        base_url = self._current_cat_url or (
            source.get_discovery_config().get("works_list_url")
            or source.get_discovery_config().get("list_url")
            or source.base_url
        )
        targets = page_works if page_works is not None else self._works
        use_decrypt = any(getattr(w, "_needs_cover_decrypt", False) for w in targets)
        if use_decrypt:
            task = _CoverDecryptTask(self._discovery, source, targets, epoch)
        else:
            # 构造当前页 URL（含 ?page=N），使 Playwright 渲染的页面与作品批次一致
            from urllib.parse import urlencode

            sep = "&" if "?" in base_url else "?"
            works_url = f"{base_url}{sep}{urlencode({'page': page})}"
            book_urls = [w.url for w in targets if w.url]
            if not book_urls:
                return
            task = _CoverRecoveryTask(source, works_url, book_urls, epoch)
        task.signals.finished.connect(
            lambda covers, e=epoch: self._on_covers_recovered(covers, e)
        )
        # 列表持有所有封面恢复任务（防 GC）：只用 _cover_task 覆盖引用时，
        # 先提交的任务失去 Python 引用可能被 GC → run() 不执行 → 该页封面
        # 永不恢复（实测 18mh 发现页奇数页封面空白）。列表持有保证全部执行。
        self._cover_tasks.append(task)
        self._cover_task = task  # 兼容旧引用
        QThreadPool.globalInstance().start(task)
        self.status_label.setText("正在恢复封面...")

    def _on_covers_recovered(self, covers: dict, epoch: int) -> None:
        """封面恢复完成，刷新对应 WorkCard 封面图。"""
        if epoch != self._source_epoch:
            return
        if not covers:
            self.status_label.setText(f"已加载 {self._current_page} 页 · 共 {self._work_count} 部")
            return
        # 用已恢复的 data URI 刷新 Work._cover 和 WorkCard
        for w in self._works:
            if w.url in covers:
                w.cover = covers[w.url]
        # 重建卡片封面（复用 WorkCard 的统一裁剪，与初始加载尺寸一致）
        hit = 0
        cards = self.list_container.findChildren(WorkCard)
        for card in cards:
            if card.work.url in covers:
                from PySide6.QtGui import QPixmap
                # data URI → QPixmap
                data_uri = covers[card.work.url]
                if data_uri.startswith("data:"):
                    import base64
                    try:
                        _, b64 = data_uri.split(",", 1)
                        pix = QPixmap()
                        pix.loadFromData(base64.b64decode(b64))
                        if not pix.isNull():
                            card.set_cover_pixmap(pix)
                            hit += 1
                    except Exception:
                        pass
        log.info("[discover] 封面恢复命中 %d 张 / %d 卡片", hit, len(cards))
        self.status_label.setText(f"已加载 {self._current_page} 页 · 共 {self._work_count} 部")

    # ------------------------------------------------------------------ #
    # 详情封面回填（cover_backfill 源：列表纯文本无封面，后台按需抓详情封面）
    # ------------------------------------------------------------------ #
    def _cover_backfill_enabled(self) -> bool:
        """该源是否启用详情封面回填（works_list_item.cover_backfill）。"""
        try:
            disc = self._current_source.get_discovery_config()
            return bool((disc.get("works_list_item") or {}).get("cover_backfill"))
        except Exception:  # noqa: BLE001
            return False

    def _submit_cover_backfill(self, epoch: int, page: int, page_works=None) -> None:
        """后台按需抓详情页封面回填（cover_backfill 源，列表纯文本无封面）。

        只提交当前批（page 粒度）cover 为空且 url 非空的作品，不是全部历史页；
        一个 QRunnable 内串行 fetch_cover 并带反爬间隔，并发由 _backfill_pool
        限流（≤3）。已提交过的 url 跳过（_cover_backfill_submitted 去重，
        逐页/重排不重复提交）。失败静默：fetch_cover 空/异常 → 该条跳过。
        """
        source = self._current_source
        if source is None:
            return
        targets = page_works if page_works is not None else self._works
        pending = [
            w for w in targets
            if w.url and not w.cover
            and w.url not in self._cover_backfill_submitted
        ]
        if not pending:
            return
        for w in pending:
            self._cover_backfill_submitted.add(w.url)
        task = _CoverBackfillTask(self._content, source, pending, epoch)
        task.signals.finished.connect(
            lambda covers, e=epoch: self._on_covers_backfilled(covers, e)
        )
        # 持有引用防 GC（独立列表，与恢复/解密任务分开，互不干扰）
        self._cover_backfill_tasks.append(task)
        self._backfill_pool.start(task)

    def _on_covers_backfilled(self, covers: dict, epoch: int) -> None:
        """详情封面回填完成：回写 work.cover + 经 CoverLoader 刷新对应卡片。

        epoch 不符（换源/切分类）→ 丢弃；卡片已销毁由 set_cover_pixmap 内
        shiboken6 校验跳过；空封面条目静默跳过，不弹错误。
        """
        if epoch != self._source_epoch:
            return
        if not covers:
            return
        # 回写 work.cover（后续复用：reflow 重建卡片 / 加入书架 / 再次刷新）
        for w in self._works:
            url = getattr(w, "url", "")
            if url in covers and covers[url]:
                w.cover = covers[url]
        # 经 CoverLoader 异步加载并刷新卡片（全局限流，裁剪与初始加载一致）
        from gui.components.cover_loader import CoverLoader

        cards = self.list_container.findChildren(WorkCard)
        for card in cards:
            cover_url = covers.get(getattr(card.work, "url", ""))
            if not cover_url:
                continue
            CoverLoader.instance().load(
                cover_url, lambda pix, c=card: c.set_cover_pixmap(pix)
            )

    def _maybe_preload(self) -> None:
        """内容未填满视口时补足加载（只预加载下 2 页缓冲）。

        首屏第 1 页 + 缓冲 2 页 = 最多 3 页，不再像之前那样视口不满就
        连续补到填满（一次爬多页 → 封面跟不上 / 反爬风险高）。
        预加载深度到顶后停止，剩下的交给滚动 80% 触发（_on_scroll）。
        _has_more=False 或缓冲页已在加载中 → 跳过。
        """
        if not self._has_more:
            return
        if self.scroll.verticalScrollBar().maximum() >= self.scroll.height():
            return  # 视口已填满，等滚动触发下一页
        loaded_max = max(self._loaded_pages or {0})
        next_page = loaded_max + 1
        if next_page > 1 + self._preload_ahead:
            return  # 预加载深度到顶（只缓冲 2 页），等滚动
        if next_page in self._active_pages or next_page in self._loaded_pages:
            return
        self._load_next_page(page=next_page)

    def _columns(self) -> int:
        """按可视宽度计算作品列数，避免固定列数导致横向溢出。"""
        view_w = self.scroll.viewport().width() or self.width() or 900
        return self.grid_columns(view_w)

    def _apply_column_stretch(self, cols: int) -> None:
        """让网格每列等宽，卡片均匀分布（避免某列标题长导致列宽参差）。"""
        self.apply_column_stretch(self.grid_layout, cols)

    def _append_card(self, w, cols) -> None:
        """建一张作品卡片并放进网格（按 cols 列排列）。

        只负责「建卡 + 放网格 + 计数」；cols 必须由调用方在循环前
        用 _columns() 算好传入（助手内不再重算，防滚动条宽度变化导致
        与外部列数不一致、卡片错乱）。
        """
        row, col = divmod(self._work_count, cols)
        card = WorkCard(w)
        card.clicked.connect(self._on_work_clicked)
        self.grid_layout.addWidget(card, row, col)
        self._work_count += 1

    def _reflow(self) -> None:
        """窗口尺寸变化时按新列数重排作品网格。

        注意：只重排卡片，不清空 self._works 数据（区别于 _clear_works）。
        否则抽屉显示/隐藏导致列数变化时，_clear_works 会把已加载作品
        列表一并清空，重建后网格变空（点详情页"内容消失"）。
        """
        cols = self._columns()
        if cols == self._last_columns:
            return
        self._last_columns = cols
        self._clear_grid_widgets()
        self._work_count = 0
        for w in self._works:
            self._append_card(w, cols)
        self._apply_column_stretch(cols)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._reflow()

    def _append_works(self, works) -> None:
        """把作品卡片按网格排列（每行自适应列数个）。

        跨页按 URL 去重：分页异常/并发预加载时同一作品可能重复出现
        （如 maccms 分页失效返回相同内容），已显示过的跳过，不重复渲染。
        """
        fresh = []
        for w in works:
            if w.url in self._seen_urls:
                continue
            self._seen_urls.add(w.url)
            fresh.append(w)
        if not fresh:
            return
        self._works.extend(fresh)
        cols = self._columns()
        for w in fresh:
            self._append_card(w, cols)
        self._apply_column_stretch(cols)

    # ------------------------------------------------------------------ #
    def _on_work_clicked(self, work: Work) -> None:
        """点作品 → 后台拉详情 → 显示右侧抽屉（不阻塞 UI）。"""
        self.status_label.setText(f"加载详情：{work.title}")
        source = self._manager.get(work.source_id) if work.source_id else self._current_source
        if source is None:
            return
        # 后台拉详情，避免主线程卡顿
        task = _FetchDetailTask(self._content, source, work.url)
        task.signals.finished.connect(self._on_detail_loaded)
        self._detail_task = task  # 持有引用
        QThreadPool.globalInstance().start(task)

    def _on_detail_loaded(self, detail, err) -> None:
        if err or detail is None:
            if self._is_challenge_blocked(err):
                # 站点反爬挑战拦截（如 linovelib 个别书被 Cloudflare WAF 403 硬拦）
                self.status_label.setText(
                    "该作品被站点反爬拦截（Cloudflare 验证），请稍后再试或换源阅读。"
                )
            else:
                self.status_label.setText(f"详情加载失败：{err}")
            return
        self.detail_drawer.show_detail(detail)
        self.status_label.setText("")

    def _on_bulk_fetch(self) -> None:
        """全量抓取：确认弹窗 → 后台执行 → 进度（不阻塞 UI）。"""
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

        def _done(stats):
            self.bulk_btn.setEnabled(True)
            if isinstance(stats, dict) and stats.get("error"):
                self.status_label.setText(f"全量抓取失败：{stats['error']}")
            elif isinstance(stats, dict):
                self.status_label.setText(
                    f"全量抓取完成：{stats.get('categories', 0)} 个分类，{stats.get('works', 0)} 部作品"
                )

        from PySide6.QtCore import QThreadPool

        # 后台线程执行，完成回主线程更新 UI
        task = _BulkFetchTask(self._bulk_fetch, source)
        task.signals.finished.connect(_done)
        self._bulk_task = task  # 持引用防 GC
        QThreadPool.globalInstance().start(task)

    def _on_read(self, detail) -> None:
        """开始阅读 → 跳阅读器 Tab（占位）。"""
        self.read_requested.emit(detail)

    def _on_open_url(self, url: str) -> None:
        """打开源详情页（浏览器）。"""
        import webbrowser

        webbrowser.open(url)

    def _on_download(self, detail) -> None:
        """下载 → 转发给 App 层（弹章节范围对话框并入队）。"""
        self.download_requested.emit(detail)

    def _on_favorite(self, detail) -> None:
        """收藏/取消收藏 → 转发给 App 层（写书架收藏库）。"""
        self.favorite_requested.emit(detail)

    def _on_scroll(self, value: int) -> None:
        """滚动到 80% 触发加载下一页（懒加载 + 预加载缓冲）。

        提前到 80% 而非贴底：滚动到底前下一批已在后台抓取，视觉无停顿；
        又不一次性并发爬多页（防封面加载不过来 / 反爬）。
        """
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() > 0 and value >= vbar.maximum() * 0.8:
            self._load_next_page()

    def eventFilter(self, obj, event):  # noqa: N802
        """点作品网格空白区域 → 关闭详情抽屉。

        点击任何作品卡片（含其内部子控件）都不关闭；只有真正点到
        网格空白处才关闭。向上遍历 parent 判断是否在卡片内。
        """
        from PySide6.QtCore import QEvent
        from gui.components import WorkCard

        if obj is self.scroll.viewport() and event.type() == QEvent.MouseButtonPress:
            pos = self.list_container.mapFrom(self.scroll.viewport(), event.position().toPoint())
            hit = self.list_container.childAt(pos)
            # 向上遍历：点击是否落在某个作品卡片（或其子控件）内
            node = hit
            inside_card = False
            while node is not None:
                if isinstance(node, WorkCard):
                    inside_card = True
                    break
                node = node.parentWidget()
            if not inside_card and self.detail_drawer.is_open():
                self.detail_drawer.hide_detail()
                self.status_label.setText("")
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self._reload_sources()


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


class _FetchDetailTask(QRunnable):
    """后台拉详情（不阻塞 UI）。"""

    def __init__(self, content, source, url):
        super().__init__()
        self.signals = _FetchWorkerSignals()  # finished(detail, err)
        self._content = content
        self._source = source
        self._url = url

    def run(self) -> None:
        detail, err = None, None
        try:
            detail = self._content.fetch_detail(self._source, self._url)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        try:
            self.signals.finished.emit(detail, err)
        except RuntimeError:
            pass


class _FetchCategoriesTask(QRunnable):
    """后台加载分类（不阻塞 UI）。"""

    def __init__(self, discovery, source):
        super().__init__()
        self.signals = _FetchWorkerSignals()  # finished(cats, err)
        self._discovery = discovery
        self._source = source

    def run(self) -> None:
        cats, err = [], None
        try:
            cats = self._discovery.list_categories(self._source)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        try:
            self.signals.finished.emit(cats, err)
        except RuntimeError:
            pass


class _CoverRecoverySignals(QObject):
    """封面恢复信号。"""

    finished = Signal(object)  # (covers: dict{work_url: data_uri})


class _CoverRecoveryTask(QRunnable):
    """后台恢复一页漫画封面（Playwright canvas 提取 → data URI）。"""

    def __init__(self, source, booklist_url, book_urls, epoch):
        super().__init__()
        self.signals = _CoverRecoverySignals()
        self._source = source
        self._booklist_url = booklist_url
        self._book_urls = book_urls
        self._epoch = epoch

    def run(self) -> None:
        covers = {}
        try:
            from urllib.parse import urljoin

            from framework.comic_cover_recovery import recover_booklist_covers_sync

            base = self._source.base_url
            abs_url = self._booklist_url if self._booklist_url.startswith("http") else urljoin(base, self._booklist_url)
            covers = recover_booklist_covers_sync(
                abs_url,
                self._book_urls,
                proxy=self._source.transports().get("proxy"),
            )
        except Exception:
            covers = {}
        try:
            self.signals.finished.emit(covers)
        except RuntimeError:
            pass


class _CoverDecryptTask(QRunnable):
    """后台下载+AES 解密封面（18mh 类加密站）→ {work_url: data_uri}。"""

    def __init__(self, discovery, source, works, epoch):
        super().__init__()
        self.signals = _CoverRecoverySignals()  # finished(covers: dict{work_url: data_uri})
        self._discovery = discovery
        self._source = source
        self._works = works
        self._epoch = epoch

    def run(self) -> None:
        covers = {}
        try:
            covers = self._discovery.decrypt_covers(self._source, self._works)
        except Exception:  # noqa: BLE001
            covers = {}
        try:
            self.signals.finished.emit(covers)
        except RuntimeError:
            pass


class _CoverBackfillSignals(QObject):
    """详情封面回填信号。"""

    finished = Signal(object)  # (covers: dict{work_url: cover_url})


class _CoverBackfillTask(QRunnable):
    """后台按需抓详情页封面（cover_backfill 源）。

    列表页纯文本无封面、详情页有封面：对 cover 为空的作品逐个
    fetch_cover（轻量，只取详情封面字段），复用 Content._detail_html_cache
    免重复下载；请求间带源 transports.interval_ms 反爬间隔。单条失败/空 →
    跳过（静默），不影响其它条目。
    """

    def __init__(self, content, source, works, epoch):
        super().__init__()
        self.signals = _CoverBackfillSignals()
        self._content = content
        self._source = source
        self._works = works
        self._epoch = epoch

    def run(self) -> None:
        covers = {}
        try:
            import time

            # 反爬间隔：跟随源 transports.interval_ms（下限 0.2s）
            interval = float(self._source.transports().get("interval_ms") or 200) / 1000.0
            if interval < 0.2:
                interval = 0.2
            for i, w in enumerate(self._works):
                if i > 0:
                    time.sleep(interval)
                try:
                    cover_url = self._content.fetch_cover(self._source, w.url) or ""
                except Exception:  # noqa: BLE001
                    cover_url = ""
                if cover_url:
                    covers[w.url] = cover_url
        except Exception:  # noqa: BLE001
            pass  # 单条已内层容错，整批异常兜底静默
        try:
            self.signals.finished.emit(covers)
        except RuntimeError:
            pass  # 页面已销毁，忽略信号


class _BulkFetchSignals(QObject):
    """全量抓取信号。"""
    finished = Signal(object)  # (stats dict 或 {"error": str})


class _BulkFetchTask(QRunnable):
    """后台执行全量抓取（不阻塞 UI）。"""

    def __init__(self, bulk_fetch, source):
        super().__init__()
        self.signals = _BulkFetchSignals()
        self._bulk_fetch = bulk_fetch
        self._source = source

    def run(self) -> None:
        try:
            stats = self._bulk_fetch.fetch_all(self._source)
        except Exception as exc:  # noqa: BLE001
            stats = {"error": str(exc)}
        try:
            self.signals.finished.emit(stats)
        except RuntimeError:
            pass
