"""搜索界面（SearchPage）。

对应 ui-search.md：
- 关键词输入 + 类型筛选 + 源范围 + 搜索
- 结果卡片流（复用 WorkCard）
- 来源角标可筛选
- 跨源搜索（后台线程）
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal, QThreadPool, QRunnable, QObject
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from framework.search import Search, SearchResult
from framework.source_manager import SourceManager

from gui.components import WorkCard
from .base_page import BasePage


class _NonClosingMenu(QMenu):
    """点击 checkable 菜单项不自动关闭的 QMenu，仅点击外部或 Escape 关闭。"""

    def mouseReleaseEvent(self, event):
        action = self.actionAt(event.pos())
        if action and action.isCheckable():
            action.setChecked(not action.isChecked())
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() != Qt.Key_Escape:
            return
        super().keyPressEvent(event)


# 反爬挑战错误特征：错误文本（小写）命中任一 → 判定被站点反爬挑战拦截
# （Cloudflare「Just a moment」Turnstile / WAF 403 / AntiScrapeError 等）。
# 搜索结果加载失败是「一次性」无自动重试，命中时只在状态处给出明确提示。
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

# 封面随卡片立即加载（CoverLoader 异步限流），不做滚动 mapTo 扫描——
# 与发现页一致（发现页滚动丝滑）。以下常量保留注释说明：
# （原手动封面泵的预加载窗口 PREFETCH_COVER_EXTRA 已移除）

# 源多选菜单限高：源行估算高度超过该阈值时菜单内出现滚动条（源多时不
# 一屏铺满全屏）；源少时菜单随内容自适应，不含滚动条。仅限高，不改变
# _NonClosingMenu「点行不收起、点外部/Escape 收起」行为。
SRC_MENU_SCROLL_LIMIT = 400


class _SearchSignals(QObject):
    finished = Signal(object, object, object, object)  # (source, results, err, epoch)
    page = Signal(object, object, object)  # (source, 本页新增结果, epoch) 边抓边显示


class _SearchTask(QRunnable):
    """后台单源搜索（每源一个任务，驱动源状态条）。

    通过 search_one 的 on_page 回调逐页把结果发到主线程（page 信号），
    第 1 页秒出、后续页抓到即追加；finished 在全部页抓完后发（全量）。

    epoch：本次搜索会话标记。换源/换关键词再搜时 epoch 自增，旧任务的
    结果回调到达主线程后因 epoch 过期被丢弃，避免「闪出上次没换源时的内容」。
    """

    def __init__(self, search_obj, source, keyword, epoch=0):
        super().__init__()
        self.signals = _SearchSignals()
        self._search = search_obj
        self._source = source
        self._keyword = keyword
        self._epoch = epoch

    def run(self) -> None:
        results, err = [], None

        def on_page(source, page, new_results):
            try:
                self.signals.page.emit(source, new_results, self._epoch)
            except RuntimeError:
                pass

        try:
            results = self._search.search_one(
                self._source, self._keyword, on_page=on_page
            )
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(self._source, results, err, self._epoch)
        except RuntimeError:
            pass


class _SearchCoverDecryptSignals(QObject):
    finished = Signal(object, object, object)  # (source, {result.url: data_uri}, epoch)


class _SearchCoverDecryptTask(QRunnable):
    """后台批量解密封面（加密站搜索结果，18mh 类）。

    搜索返回时封面是加密 URL 直接加载不出图，后台复用 discovery 的
    AES 解密（decrypt_search_covers）后，GUI 把 data URI 回填刷新。
    非加密源不启动此任务（decrypt_search_covers 返回 {}）。

    epoch：发起时搜索会话标记。旧搜索的解密结果后到（epoch 过期）会被
    _on_covers_decrypted 丢弃，避免回填到新搜索的 _results/卡片。
    """

    def __init__(self, search_obj, source, results, epoch=0):
        super().__init__()
        self.signals = _SearchCoverDecryptSignals()
        self._search = search_obj
        self._source = source
        self._results = results
        self._epoch = epoch

    def run(self) -> None:
        covers = {}
        try:
            covers = self._search.decrypt_search_covers(self._source, self._results)
        except Exception:  # noqa: BLE001
            covers = {}
        try:
            self.signals.finished.emit(self._source, covers, self._epoch)
        except RuntimeError:
            pass


class _SearchCoverBackfillSignals(QObject):
    finished = Signal(object, object, object)  # (source, {result.url: cover_url}, epoch)


class _SearchCoverBackfillTask(QRunnable):
    """后台按需抓详情页封面（cover_backfill 源，搜索列表纯文本无封面）。

    与发现页 _CoverBackfillTask 同模式：对 cover 为空的结果逐个
    fetch_cover（轻量，只取详情封面字段，复用 Content 详情缓存免重复下载），
    请求间带源 transports.interval_ms 反爬间隔；并发由 SearchPage._backfill_pool
    限流（≤3）。单条失败/空 → 跳过（静默），不影响其它条目。

    epoch：发起时搜索会话标记。旧搜索的回填结果后到（epoch 过期）会被
    _on_covers_backfilled 丢弃，避免回填到新搜索的 _results/卡片。
    """

    def __init__(self, content, source, results, epoch=0):
        super().__init__()
        self.signals = _SearchCoverBackfillSignals()
        self._content = content
        self._source = source
        self._results = results
        self._epoch = epoch

    def run(self) -> None:
        covers = {}
        try:
            import time

            # 反爬间隔：跟随源 transports.interval_ms（下限 0.2s）
            interval = float(self._source.transports().get("interval_ms") or 200) / 1000.0
            if interval < 0.2:
                interval = 0.2
            for i, r in enumerate(self._results):
                if i > 0:
                    time.sleep(interval)
                try:
                    cover_url = self._content.fetch_cover(self._source, r.url) or ""
                except Exception:  # noqa: BLE001
                    cover_url = ""
                if cover_url:
                    covers[r.url] = cover_url
        except Exception:  # noqa: BLE001
            pass  # 单条已内层容错，整批异常兜底静默
        try:
            self.signals.finished.emit(self._source, covers, self._epoch)
        except RuntimeError:
            pass  # 页面已销毁，忽略信号


class SearchPage(BasePage):
    search_clicked = Signal(str)  # 搜索触发（首页接）
    open_requested = Signal(str, str, str)  # (source_id, url, content_type) 打开作品
    # 批量操作（ui-search.md #8）：加入书架 / 加入下载
    add_to_shelf_requested = Signal(object)   # list[SearchResult]
    batch_download_requested = Signal(object)  # list[SearchResult]

    def __init__(self, source_manager: SourceManager, search: Search, content=None, parent=None):
        super().__init__(parent)
        self._manager = source_manager
        self._search = search
        self._content = content  # 可选：详情封面回填（cover_backfill 源）用
        self._results = []
        self._filter_source = ""
        self._saved_unfiltered_shown = None  # 进入来源筛选前的渲染进度（清除筛选后恢复）
        self._status_chips: dict = {}  # source_id → (QLabel, QLabel状态) 或组合控件
        self._pending_count = 0  # 未完成搜索的源数
        self._work_count = 0  # 当前网格卡片计数（追加/重建共用）
        self._shown_count = 0  # 已渲染到 _results 的条数（分批懒加载用）
        self._page_size = 12  # 每批渲染条数：首屏更早出内容（12 张更快），余下滚动补
        self._selected: dict = {}  # 勾选批量：url → SearchResult
        self._select_mode = False  # 是否进入勾选模式
        self._cover_tasks = []  # 封面解密后台任务引用（防 GC）
        self._cover_decrypt_submitted: set = set()  # 已提交封面解密的结果 url（防逐页/done 重复触发）
        self._backfill_pool = QThreadPool(self)  # 详情封面回填专用池（限流，≤3 并发）
        self._backfill_pool.setMaxThreadCount(3)
        self._cover_backfill_tasks: list = []  # 详情封面回填任务持有（防 GC）
        self._cover_backfill_submitted: set = set()  # 已提交回填的 url（防逐页/done 重复）
        self._streamed: set = set()  # 已边抓边渲染的源（finished 不重复追加）
        self._search_epoch = 0  # 搜索会话标记：换源/换关键词自增，过期任务结果丢弃
        self._results_display = None  # 合并模式渲染列表；None 时用 _results（新搜索须重置）
        self._more_pending = False  # 滚动加载合并标记：同一事件循环内的连续滚动事件只铺一批
        self._restoring_scroll = False  # 懒加载锚定/重建恢复滚动位置时抑制 on_scroll 级联
        self._last_columns = 0  # 已应用的网格列数：列数不变时跳过重复列拉伸（避免每批全量重排）
        self._all_action = None  # 源菜单「全部」QAction 引用（就地同步勾选态，不重建菜单）
        self._src_rows: dict = {}  # source_id → 源行 QPushButton（就地同步勾选态，不重建菜单）

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # ---- 搜索输入区 ----
        top = QHBoxLayout()
        self.keyword_input = QLineEdit()
        self.keyword_input.setPlaceholderText("输入关键词搜索...")
        self.keyword_input.returnPressed.connect(self._on_search)
        self.type_combo = QComboBox()
        self.type_combo.addItem("全部类型", "")
        for t in ("novel", "comic", "video"):
            self.type_combo.addItem(t, t)
        self._all_selected: bool = True
        self._selected_sources: set = set()
        self.src_btn = QPushButton("源：全部")
        self._src_menu = _NonClosingMenu(self.src_btn)
        # 「全部」QAction 走 QMenu 默认渲染：显式声明字色/选中背景，
        # 避免与源行按钮的 QSS 混用后出现菜单项看不清或选中态跳变。
        self._src_menu.setStyleSheet(
            "QMenu { background: palette(window); border: 1px solid palette(mid);"
            " border-radius: 8px; padding: 4px; }"
            "QMenu::item { color: palette(text); padding: 7px 24px 7px 12px;"
            " border-radius: 6px; }"
            "QMenu::item:selected { background: palette(highlight);"
            " color: palette(highlightedText); }"
            "QMenu::item:disabled { color: palette(mid); }"
            "QMenu::separator { height: 1px; background: palette(midlight);"
            " margin: 4px 8px; }"
        )
        self.src_btn.setMenu(self._src_menu)
        self._rebuild_sources_menu()
        self.merge_check = QCheckBox("合并相似")
        self.merge_check.setToolTip("按书名+作者模糊匹配，合并同书多源版本（默认关）")
        self.merge_check.setChecked(False)
        self.merge_check.toggled.connect(self._on_merge_toggled)
        self.search_btn = QPushButton("搜索")
        self.search_btn.clicked.connect(self._on_search)
        top.addWidget(self.keyword_input, stretch=1)
        top.addWidget(self.type_combo)
        top.addWidget(self.src_btn)
        top.addWidget(self.merge_check)
        top.addWidget(self.search_btn)
        layout.addLayout(top)

        # ---- 每源状态条（跨源并发进度，ui-search.md #4）----
        self.status_bar = QWidget()
        self.status_bar_layout = QHBoxLayout(self.status_bar)
        self.status_bar_layout.setContentsMargins(0, 0, 0, 0)
        self.status_bar_layout.setSpacing(8)
        self.status_bar.setVisible(False)
        layout.addWidget(self.status_bar)

        # ---- 来源过滤 chip ----
        self.filter_bar = QHBoxLayout()
        self.filter_label = QLabel("")
        self.filter_label.setStyleSheet("color: palette(accent); font-weight: bold;")
        self.filter_clear_btn = QPushButton("× 清除筛选")
        self.filter_clear_btn.setFixedWidth(80)
        self.filter_clear_btn.clicked.connect(self._clear_filter)
        self.filter_bar.addWidget(self.filter_label)
        self.filter_bar.addWidget(self.filter_clear_btn)
        self.filter_bar.addStretch(1)
        self.filter_bar_widget = QWidget()
        self.filter_bar_widget.setLayout(self.filter_bar)
        self.filter_bar_widget.setVisible(False)
        layout.addWidget(self.filter_bar_widget)

        # ---- 搜索状态 ----
        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color: palette(dark); padding: 8px;")
        layout.addWidget(self.status_label)

        # ---- 结果卡片网格 ----
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.scroll, stretch=1)

        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_layout.setSpacing(12)
        self.scroll.setWidget(self.grid_container)

        # 懒加载滚动
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

        # ---- 批量操作栏（勾选 ≥1 条后显示，ui-search.md #8）----
        self.batch_bar = QWidget()
        self.batch_bar.setVisible(False)
        bb = QHBoxLayout(self.batch_bar)
        bb.setContentsMargins(0, 6, 0, 0)
        bb.setSpacing(8)
        self.select_all_check = QCheckBox("全选")
        self.select_all_check.toggled.connect(self._on_select_all)
        bb.addWidget(self.select_all_check)
        self.batch_count = QLabel("已选 0 项")
        self.batch_count.setStyleSheet("color: palette(dark); font-size: 12px;")
        bb.addWidget(self.batch_count)
        bb.addStretch(1)
        self.batch_shelf_btn = QPushButton("加入书架")
        self.batch_shelf_btn.setFixedHeight(26)
        self.batch_shelf_btn.clicked.connect(self._on_batch_add_shelf)
        bb.addWidget(self.batch_shelf_btn)
        self.batch_dl_btn = QPushButton("加入下载")
        self.batch_dl_btn.setFixedHeight(26)
        self.batch_dl_btn.clicked.connect(self._on_batch_download)
        bb.addWidget(self.batch_dl_btn)
        self.batch_clear_btn = QPushButton("取消选择")
        self.batch_clear_btn.setFixedHeight(26)
        self.batch_clear_btn.clicked.connect(self._clear_selection)
        bb.addWidget(self.batch_clear_btn)
        layout.addWidget(self.batch_bar)

    def fill_keyword(self, keyword: str) -> None:
        """外部预填关键词并搜索。"""
        self.keyword_input.setText(keyword)
        self._on_search()

    def _on_search(self) -> None:
        keyword = self.keyword_input.text().strip()
        if not keyword:
            return
        self._filter_source = ""
        self._saved_unfiltered_shown = None
        self.filter_bar_widget.setVisible(False)
        self.status_label.setText("搜索中...")
        self._clear_grid()
        self._results = []
        # 重置渲染列表：前一次合并搜索设过 _results_display，换源/换关键词再搜时
        # 不重置会继续渲染旧内容（_current_display 优先读它）→ 一直显示多源旧结果。
        self._results_display = None
        self._shown_count = 0
        self._cover_decrypt_submitted = set()
        self._cover_backfill_submitted = set()  # 新一轮会话可重新回填封面
        self._cover_backfill_tasks = []  # 清空旧回填任务引用（防回到旧结果）
        self._selected = {}
        self.batch_bar.setVisible(False)
        self.select_all_check.setChecked(False)

        # 选择目标源
        selected_type = self.type_combo.currentData()
        if self._all_selected:
            sources = self._manager.enabled_sources()
        elif self._selected_sources:
            sources = [s for s in self._manager.enabled_sources()
                       if s.source_id in self._selected_sources]
        else:
            self.status_label.setText("未选择任何源")
            return
        if selected_type:
            sources = [s for s in sources if s.content_type == selected_type]

        if not sources:
            self.status_label.setText("没有可搜索的源")
            return

        # 每源一个状态 chip + 一个后台任务（并发由 ThreadPool 调度）
        self._build_status_bar(sources)
        self.status_bar.setVisible(True)
        self._pending_count = len(sources)
        self._search_tasks = []
        self._streamed = set()
        self._search_epoch += 1
        epoch = self._search_epoch
        for source in sources:
            task = _SearchTask(self._search, source, keyword, epoch=epoch)
            task.signals.finished.connect(self._on_source_done)
            task.signals.page.connect(self._on_source_page)  # 边抓边显示
            self._search_tasks.append(task)  # 持引用防 GC
            QThreadPool.globalInstance().start(task)

    # ------------------------------------------------------------------ #
    # 每源状态条
    # ------------------------------------------------------------------ #
    def _build_status_bar(self, sources) -> None:
        """清空并重建每源状态 chip 行。"""
        while self.status_bar_layout.count():
            item = self.status_bar_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._status_chips = {}
        for source in sources:
            chip = QLabel(f"🔄 {source.source_name}")
            chip.setStyleSheet(
                "font-size: 11px; padding: 3px 10px; border-radius: 10px;"
                "background: palette(midlight); color: palette(text);"
            )
            chip.setToolTip(f"{source.source_name}：搜索中")
            self.status_bar_layout.addWidget(chip)
            self._status_chips[source.source_id] = chip
        self.status_bar_layout.addStretch(1)

    def _set_source_status(self, source, state: str, err: str = "") -> None:
        """更新单源状态 chip：🔄进行中 ✅完成 ❌失败。"""
        chip = self._status_chips.get(source.source_id)
        if chip is None:
            return
        if state == "running":
            chip.setText(f"🔄 {source.source_name}")
            chip.setStyleSheet(
                "font-size: 11px; padding: 3px 10px; border-radius: 10px;"
                "background: palette(midlight); color: palette(text);"
            )
            chip.setToolTip(f"{source.source_name}：搜索中")
        elif state == "done":
            chip.setText(f"✅ {source.source_name}")
            chip.setStyleSheet(
                "font-size: 11px; padding: 3px 10px; border-radius: 10px;"
                "background: palette(base); color: palette(text);"
            )
            chip.setToolTip(f"{source.source_name}：完成")
        else:
            chip.setText(f"❌ {source.source_name}")
            chip.setStyleSheet(
                "font-size: 11px; padding: 3px 10px; border-radius: 10px;"
                "background: rgba(255,107,107,0.15); color: #D32F2F;"
            )
            chip.setToolTip(f"{source.source_name}：失败\n{err or '未知错误'}")

    def _on_source_page(self, source, new_results, epoch) -> None:
        """边抓边显示：单源某页结果就绪，立即追加渲染（第 1 页秒出）。

        合并模式下不渲染（等全部完成统一合并），但结果照常累计进 _results。
        epoch 不匹配说明这次搜索已被新的搜索替代，结果丢弃（防闪旧内容）。
        """
        if epoch != self._search_epoch or not new_results:
            return
        self._results.extend(new_results)
        self._streamed.add(source.source_id)
        if not self.merge_check.isChecked():
            self._append_results(new_results)
        # 加密站（18mh 类）：边抓边显示首批就触发封面解密，不等全部页 done——
        # 否则源搜索慢（多页）时封面整场是加密 URL 加载不出。逐页去重提交。
        self._submit_cover_decrypt(source, new_results)
        # cover_backfill 源（搜索列表纯文本无封面）：逐页后台抓详情封面回填。
        self._submit_cover_backfill(source, new_results)
        self._update_batch_status()

    def _on_source_done(self, source, results, err, epoch) -> None:
        """单源搜索完成：更新状态 chip + 追加结果。

        已通过 page 事件边抓边渲染的源（_streamed），finished 的全量结果
        不重复追加；否则（on_page 缺省/回调异常）一次性补全。
        epoch 不匹配（已被新搜索替代）→ 整单丢弃。
        """
        if epoch != self._search_epoch:
            return
        if err:
            if self._is_challenge_blocked(err):
                # 站点反爬挑战拦截：给出明确提示（搜索是一次性，无自动重试）
                self._set_source_status(source, "failed", "被站点反爬拦截（Cloudflare 验证）")
                self.status_label.setText(
                    f"{source.source_name} 被站点反爬拦截（Cloudflare 验证），"
                    "请稍后再试或换源搜索。"
                )
            else:
                self._set_source_status(source, "failed", str(err))
        else:
            self._set_source_status(source, "done")
            if source.source_id not in self._streamed:
                self._results.extend(results or [])
                if self.merge_check.isChecked():
                    # 合并模式：只累积结果，全部完成后再统一合并渲染
                    pass
                else:
                    self._append_results(results or [])
            # 加密站（18mh 类）：搜索结果封面后补解密——列表秒开，封面后台恢复。
            # 边抓边显示的页已逐批提交（_on_source_page → _submit_cover_decrypt），
            # done 这里只补提交未解密过的剩余结果（_cover_decrypt_submitted 去重）。
            self._submit_cover_decrypt(source, results or [])
            # cover_backfill 源：done 补提交（_cover_backfill_submitted 去重，
            # 只补未回填过的剩余结果——on_page 缺省/未流式时一次性补全）。
            self._submit_cover_backfill(source, results or [])
        self._pending_count -= 1
        if self._pending_count <= 0:
            self._on_all_done()

    @staticmethod
    def _is_challenge_blocked(err_text) -> bool:
        """错误文本是否命中反爬挑战特征（Cloudflare 验证 / WAF 403 等）。

        搜索结果加载失败是「一次性」无自动重试循环，命中时只在状态处
        给出明确提示（不涉及重试调度）。普通网络超时/404 不含这些特征。
        """
        if not err_text:
            return False
        low = str(err_text).lower()
        return any(marker in low for marker in CHALLENGE_MARKERS)

    @staticmethod
    def _needs_cover_decrypt(source) -> bool:
        """该源搜索结果封面是否需 AES 解密（decryption.targets.image，18mh 类）。"""
        try:
            return bool(
                source.raw.get("decryption", {}).get("targets", {}).get("image")
            )
        except Exception:  # noqa: BLE001
            return False

    def _on_covers_decrypted(self, source, covers, epoch) -> None:
        """封面解密完成：回写 SearchResult.cover + 刷新对应卡片。

        covers = {result.url: data_uri}。合并模式下卡片 work 是合并代表
        （url 与原始结果一致），按 url 匹配刷新。
        epoch 不匹配（已被新搜索替代）→ 旧结果直接丢弃，避免回填到
        新搜索的 _results/卡片（同 url 串封面）。
        """
        if epoch != self._search_epoch or not covers:
            return
        for r in self._results:
            uri = covers.get(r.url)
            if uri:
                r.cover = uri
        import shiboken6

        for card in self.grid_container.findChildren(WorkCard):
            try:
                if not shiboken6.isValid(card):
                    continue
                uri = covers.get(getattr(card.work, "url", ""))
                if uri:
                    card.set_cover_data(uri)
            except Exception:  # noqa: BLE001
                continue

    def _submit_cover_decrypt(self, source, results) -> None:
        """为未提交过封面解密的结果启动后台解密（加密站 decryption.targets.image）。

        边抓边显示逐页到达与 done 全量到达都走这里，用 _cover_decrypt_submitted
        按 url 去重：同一批结果只提交一次，避免逐页触发 + done 全量触发重复
        解密同一批封面。非加密源或空结果直接跳过。
        """
        if not results or not self._needs_cover_decrypt(source):
            return
        pending = [
            r for r in results
            if getattr(r, "url", "") and r.url not in self._cover_decrypt_submitted
        ]
        if not pending:
            return
        for r in pending:
            self._cover_decrypt_submitted.add(r.url)
        task = _SearchCoverDecryptTask(
            self._search, source, pending, epoch=self._search_epoch
        )
        task.signals.finished.connect(self._on_covers_decrypted)
        self._cover_tasks.append(task)  # 持引用防 GC
        QThreadPool.globalInstance().start(task)

    # ------------------------------------------------------------------ #
    # 详情封面回填（cover_backfill 源：搜索列表纯文本无封面，后台按详情页补回）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _needs_cover_backfill(source) -> bool:
        """该源搜索列表是否纯文本无封面（需详情页回填封面）。

        优先看 endpoints.search.item.cover_backfill；搜索未显式配时
        兼容 discovery.works_list_item.cover_backfill——同一站点列表页
        纯文本无封面，搜索列表通常同样无封面，两处同启。
        """
        try:
            search_item = (source.raw.get("endpoints") or {}).get("search", {}).get("item") or {}
            if search_item.get("cover_backfill"):
                return True
            disc = (source.raw.get("endpoints") or {}).get("discovery") or {}
            return bool((disc.get("works_list_item") or {}).get("cover_backfill"))
        except Exception:  # noqa: BLE001
            return False

    def _submit_cover_backfill(self, source, results) -> None:
        """为 cover 为空的结果后台抓详情页封面（cover_backfill 源）。

        边抓边显示逐页到达与 done 全量到达都走这里，用 _cover_backfill_submitted
        按 url 去重：同一批结果只提交一次。仅提交 cover 为空且 url 非空的结果
        （搜索结果自带封面的源不额外抓详情）。未注入 content（非 GUI 入口 /
        无 Content 可用）直接跳过。失败静默。
        """
        if not results or not self._needs_cover_backfill(source):
            return
        if self._content is None:
            return
        pending = [
            r for r in results
            if getattr(r, "url", "") and not getattr(r, "cover", "")
            and r.url not in self._cover_backfill_submitted
        ]
        if not pending:
            return
        for r in pending:
            self._cover_backfill_submitted.add(r.url)
        task = _SearchCoverBackfillTask(
            self._content, source, pending, epoch=self._search_epoch
        )
        task.signals.finished.connect(self._on_covers_backfilled)
        self._cover_backfill_tasks.append(task)  # 持引用防 GC（与解密任务分开，互不干扰）
        self._backfill_pool.start(task)

    def _on_covers_backfilled(self, source, covers, epoch) -> None:
        """详情封面回填完成：回写 SearchResult.cover + 经 CoverLoader 刷新对应卡片。

        epoch 不匹配（已被新搜索替代）→ 丢弃；空结果跳过。合并模式下
        卡片 work 是合并代表（url 与原始结果一致），按 url 匹配刷新。
        """
        if epoch != self._search_epoch or not covers:
            return
        for r in self._results:
            url = getattr(r, "url", "")
            if url in covers and covers[url]:
                r.cover = covers[url]
        # 经 CoverLoader 异步加载并刷新卡片（全局限流，与初始加载一致）
        from gui.components.cover_loader import CoverLoader

        import shiboken6

        for card in self.grid_container.findChildren(WorkCard):
            try:
                if not shiboken6.isValid(card):
                    continue
                cover_url = covers.get(getattr(card.work, "url", ""))
                if not cover_url:
                    continue
                CoverLoader.instance().load(
                    cover_url,
                    lambda pix, c=card: c.set_cover_pixmap(pix),
                    source_id=source.source_id,
                    source=source,
                )
            except Exception:  # noqa: BLE001
                continue

    def _on_merge_toggled(self, checked: bool) -> None:
        """合并相似开关切换：对当前结果重新合并渲染（结果已加载时）。"""
        if self._results:
            self._rebuild_results_with_merge()

    def _rebuild_results_with_merge(self) -> None:
        """按当前合并开关状态重建结果网格。"""
        if self._search is None:
            return
        if self.merge_check.isChecked():
            # 合并相似：合并后只显示代表卡片（variants 挂在卡片详情）
            from framework.search import Search

            merged = Search.merge_similar(self._results)
            self._results_display = merged
        else:
            self._results_display = list(self._results)
        self._shown_count = 0
        ratio = self._scroll_ratio()
        self._clear_grid()
        # 只渲染首屏（能填满一个视口的批次数），剩余滚动懒加载——
        # 不像以前全量渲染剩余卡片（几百张同帧建卡导致首屏/下滑卡顿）。
        self._append_displayed_batch()
        # 合并开关切换时内容条数可能骤变：按比例恢复，避免网格重建后
        # 贴回顶部/跳到底（合并前滚动位置的相对视觉保持不变）。
        self._restore_scroll_ratio(ratio)

    def _append_displayed_batch(self) -> None:
        """按 _results_display 渲染首屏（能填满一个视口的批次数）。

        剩余结果留待滚动到 80%（_on_scroll）逐批渲染，不提前全量建卡。
        """
        display = getattr(self, "_results_display", None)
        if display is None:
            return
        cols = self._columns()
        new_shown = min(len(display), self._first_screen_limit())
        while self._shown_count < new_shown:
            r = display[self._shown_count]
            self._append_card(r, cols)
            self._shown_count += 1
        self._apply_column_stretch(cols)
        self._update_batch_status()
        self._maybe_preload_results()

    def _on_all_done(self) -> None:
        """全部源搜索结束。

        非合并模式：边抓边显示已渲染首屏，直接更新状态（不重建网格，
        避免搜索完成瞬间清空重插导致闪屏）；合并模式：结果一直在累积
        未渲染，统一合并后只渲染首屏。

        剩余结果不在这里渲染——严格按滚动到 80%（_on_scroll）逐批加载
        （每批 _page_size 条），避免搜索完成瞬间全量建卡导致首屏/下滑
        卡顿，用户只看到已在视口内的卡片。仅当首屏还没填满（边抓边显示
        异常 on_page 缺省 / 结果不足一屏）时兜底补齐首屏，绝不提前渲染
        视口外结果。
        """
        if not self._results:
            self.status_label.setText("搜不到这个哦，换个词试试？")
            return
        if self.merge_check.isChecked():
            self._rebuild_results_with_merge()
        elif self._shown_count < self._first_screen_limit():
            # 兜底补齐首屏：渲染到填满一个视口为止（结果不足一屏则全渲染）
            self._append_results(self._results)
        self._update_batch_status()

    def _update_batch_status(self) -> None:
        """更新状态文本：已显示 X / 共 Y 条。"""
        total = len(self._filtered_display())
        if self._filter_source:
            self.status_label.setText(f"共 {total} 条结果（仅看此源）")
        elif self._shown_count >= total:
            self.status_label.setText(f"共 {total} 条结果")
        else:
            self.status_label.setText(f"已显示 {self._shown_count} / {total} 条，滚动加载更多...")

    def _append_results(self, items) -> None:
        """把一批结果卡片追加到网格尾部（按当前列数排），首屏懒加载。

        结果累积到 _results（_on_source_page），这里只渲染到首屏
        （填满一个视口 + 最后多留一行）；不自动补足视口——避免边抓边
        显示时一次性插入几百张卡片导致闪屏/跳动（与发现页懒加载一致）。
        其余结果留待滚动到 80%（_on_scroll）再逐批渲染。
        """
        if not items:
            return
        display = self._filtered_display()
        cols = self._columns()
        # 首屏渲染量：填满视口（而非固定 _page_size 条）——否则结果多时
        # 只渲染一页、布局未刷新前滚动条不出现，用户看不到更多。按视口
        # 高估算行数（卡片高约 292px，4 列）再留一行缓冲，让首屏渲染后
        # 滚动条必然出现；仍保留上限防一次性建太多卡片闪屏。
        new_shown = min(len(display), self._first_screen_limit())
        while self._shown_count < new_shown:
            r = display[self._shown_count]
            self._append_card(r, cols)
            self._shown_count += 1
        self._apply_column_stretch(cols)
        self._update_batch_status()
        self._maybe_preload_results()

    def _first_screen_limit(self) -> int:
        """能填满一个视口的首屏渲染量（最后多留一行，滚动条必然出现）。

        结果再多也只建这么多张卡片：首屏之后严格按滚动到 80%（_on_scroll）
        逐批渲染，避免搜索完成/边抓边显示瞬间一次性建几百张卡片闪屏/卡顿。
        """
        cols = max(1, self._columns())
        rows = max(1, self.scroll.viewport().height() // 292 + 1)
        return max(self._page_size, rows * cols)

    def _current_display(self):
        """当前渲染源：合并后为 _results_display，否则 _results。"""
        display = getattr(self, "_results_display", None)
        if display is None:
            display = self._results
        return display

    def _filtered_display(self):
        """按来源筛选过滤后的当前渲染列表（无筛选时返回原列表）。"""
        display = self._current_display()
        if not self._filter_source:
            return display
        return [r for r in display if r.source_id == self._filter_source]

    def _scroll_ratio(self) -> float:
        """滚动位置占滚动条比例（0~1）。maximum<=0 → 0。"""
        vbar = self.scroll.verticalScrollBar()
        return (vbar.value() / vbar.maximum()) if vbar.maximum() > 0 else 0.0

    def _restore_scroll_ratio(self, ratio: float) -> None:
        """下个事件循环按比例恢复滚动位置（重建/换词后布局未刷新，需延后）。

        抑制 on_scroll：恢复动作本身不触发下一批懒加载（否则恢复位置
        恰在 80% 以上会同步递归连铺，回到「全量滚动卡顿」老坑）。
        """
        ratio = max(0.0, min(1.0, ratio))

        def _apply() -> None:
            try:
                import shiboken6
                if not shiboken6.isValid(self):
                    return  # 页面已销毁，忽略
            except Exception:  # noqa: BLE001
                pass
            vbar = self.scroll.verticalScrollBar()
            if vbar.maximum() <= 0:
                return
            self._restoring_scroll = True
            try:
                vbar.setValue(int(round(ratio * vbar.maximum())))
            finally:
                self._restoring_scroll = False

        QTimer.singleShot(0, _apply)

    def _load_more_results(self) -> None:
        """滚动加载下一批结果（一次滚动只铺一批，铺完即停）。

        滚动到 80% 触发（_on_scroll）。一次同步铺 _page_size 张（12）；
        立即看看视口下方是否还有明显的空白缓冲（_maybe_preload_results
        只补足首屏），但**绝不**一次性/持续铺完全部结果——那会让网格里
        堆积几百上千张卡片，滚动绘制本身就卡（历史教训：上一版 flush 链
        一次触发后 16ms 一批铺到全部，滚动中途即严重卡顿）。
        用户继续滚动到下一批 80% 再触发下一批，网格卡片数保持贴近
        「已读 + 缓冲」量级，滚动始终轻量。

        滚动锚定（懒加载不跳屏）：追加前记录位置；追加后若用户已在底部
        （value==旧 max）则钉到新的底部继续看新内容，否则保持原位置，
        避免「内容补齐后视觉被顶上去/跳回来」。
        """
        if self._more_pending:
            return  # 上一次触发的分批尚未完成，本次滚动到的 80% 交给其后
        display = self._filtered_display()
        if self._shown_count >= len(display):
            return
        cols = self._columns()
        vbar = self.scroll.verticalScrollBar()
        prev_value = vbar.value()
        was_at_bottom = vbar.maximum() > 0 and prev_value >= vbar.maximum() - 8
        new_shown = min(len(display), self._shown_count + self._page_size)
        while self._shown_count < new_shown:
            r = display[self._shown_count]
            self._append_card(r, cols)
            self._shown_count += 1
        self._apply_column_stretch(cols)
        self._update_batch_status()
        self._maybe_preload_results()
        # 恢复动作排在预加载/布局之后：单帧内 scrollbar 范围已含全部新加入的
        # 卡片，锚定用新范围计算，底部钉底/中间保持才不偏
        QTimer.singleShot(
            0,
            lambda: self._finish_more_anchor(was_at_bottom, prev_value),
        )

    def _finish_more_anchor(self, was_at_bottom: bool, prev_value: int) -> None:
        """追加批次后的滚动恢复：底部钉底续接，否则保持原位置。"""
        try:
            import shiboken6
            if not shiboken6.isValid(self):
                return  # 页面已销毁，忽略
        except Exception:  # noqa: BLE001
            pass
        vbar = self.scroll.verticalScrollBar()
        self._restoring_scroll = True
        try:
            if was_at_bottom and vbar.maximum() > 0:
                vbar.setValue(vbar.maximum())
            elif vbar.value() != prev_value:
                vbar.setValue(prev_value)
        finally:
            self._restoring_scroll = False

    def _maybe_preload_results(self) -> None:
        """视口未填满 → 同步补足首屏（绝不改动滚动条位置）。

        首屏一批渲染完视口没填满（结果较少 / 布局未刷新时）继续补渲染，
        让首屏尽快填满、滚动条出现；补到首屏上限即停，剩下交给滚动 80%
        （_on_scroll）。这里**只同步补卡、不触发滚动锚定**：用户没有滚动时
        绝不主动 setValue（流式追加期间上下跳动的根因之一）。绝不调用
        _load_more_results（那条路径带锚定且属于用户滚动语义）。
        """
        display = self._filtered_display()
        if self._shown_count >= len(display):
            return
        limit = self._first_screen_limit()
        if self._shown_count >= limit:
            return  # 首屏已填满，等滚动触发
        cols = self._columns()
        new_shown = min(len(display), limit)
        while self._shown_count < new_shown:
            r = display[self._shown_count]
            self._append_card(r, cols)
            self._shown_count += 1
        self._apply_column_stretch(cols)
        self._update_batch_status()

    def _columns(self) -> int:
        """搜索结果固定 4 列。

        动态列数下滚动加载会错乱：加载更多时纵向滚动条出现使视口宽度
        变化 ±15px，若恰在整除边界则列数跳变，新旧卡片列数不一致导致
        排版错乱（第一页正常、超过后错乱）。固定 4 列保证首屏/滚动/
        合并/筛选所有渲染路径列数统一。4×190px≈760px，900px 最小窗口
        也能放下。
        """
        return 4

    def _apply_column_stretch(self, cols: int) -> None:
        """每列等宽，卡片均匀分布。

        列数不变时跳过：setColumnStretch 会触发整网格重排，分批懒加载时
        每批都调一次等于每批全量重排（滚动卡顿来源之一）。列数固定 4，
        实际只在首屏/重建后应用一次。
        """
        if cols == self._last_columns:
            return
        self._last_columns = cols
        self.apply_column_stretch(self.grid_layout, cols)

    def _append_card(self, r, cols) -> None:
        """建一张结果卡片并放进网格（按 cols 列排列）。

        只负责「建卡 + 放网格 + 计数」；cols 必须由调用方在循环前
        用 _columns() 算好传入（助手内不再重算，防滚动条宽度变化导致
        与外部列数不一致、卡片错乱）。
        """
        row, col = divmod(self._work_count, cols)
        card = self._make_card(r)
        self.grid_layout.addWidget(card, row, col)
        self._work_count += 1

    def _show_results(self) -> None:
        """按当前筛选重建结果网格（来源角标筛选用）。"""
        ratio = self._scroll_ratio()
        self._clear_grid()
        display = self._filtered_display()
        cols = self._columns()
        # 筛选时全量渲染（结果通常较少）；无筛选时只渲染已加载批
        shown = display if self._filter_source else display[:self._shown_count]
        for r in shown:
            self._append_card(r, cols)
        self._shown_count = len(shown)
        self._apply_column_stretch(cols)
        self._update_batch_status()
        # 筛选切换同样按比例恢复，不跳回顶部
        self._restore_scroll_ratio(ratio)

    def _emit_open(self, result) -> None:
        """点搜索结果卡片 → 打开 reader 播放/阅读。"""
        if not getattr(result, "url", "") or not getattr(result, "source_id", ""):
            return
        try:
            src = self._manager.get(result.source_id)
        except Exception:
            return
        self.open_requested.emit(result.source_id, result.url, src.content_type)

    # ------------------------------------------------------------------ #
    # 勾选批量（ui-search.md #8）
    # ------------------------------------------------------------------ #
    def _make_card(self, r):
        """创建勾选模式卡片并连接信号。

        封面随卡片创建立即加载（CoverLoader 全局限流异步，不阻塞 UI；
        与发现页一致——发现页滚动丝滑即源于此，不做滚动 mapTo 全扫）。
        """
        card = WorkCard(r, selectable=True)
        card.clicked.connect(lambda _, rr=r: self._emit_open(rr))
        card.checked.connect(self._on_card_checked)
        card.set_source_filterable(True)
        card.source_clicked.connect(self._set_filter)
        if r.url in self._selected:
            card.set_checked(True)
        return card

    def _on_card_checked(self, work, checked: bool) -> None:
        """单卡勾选状态变化：更新选中集合 + 批量栏。"""
        url = getattr(work, "url", "")
        if not url:
            return
        if checked:
            self._selected[url] = work
        else:
            self._selected.pop(url, None)
        self._refresh_batch_bar()

    def _refresh_batch_bar(self) -> None:
        """更新批量栏：勾选数 + 显隐。"""
        n = len(self._selected)
        self.batch_count.setText(f"已选 {n} 项")
        # 全选 checkbox 同步（避免信号循环）
        self.select_all_check.blockSignals(True)
        display = self._filtered_display()
        total = len(display)
        self.select_all_check.setChecked(total > 0 and n == total)
        self.select_all_check.blockSignals(False)
        self.batch_bar.setVisible(n > 0)

    def _on_select_all(self, checked: bool) -> None:
        """全选/取消全选当前已显示的结果。"""
        if checked:
            display = self._filtered_display()
            for r in display:
                if r.url:
                    self._selected[r.url] = r
            # 同步所有卡片勾选态
            for card in self.grid_container.findChildren(WorkCard):
                if getattr(card.work, "url", "") in self._selected:
                    card.blockSignals(True)
                    card.set_checked(True)
                    card.blockSignals(False)
        else:
            self._clear_selection()
            return
        self._refresh_batch_bar()

    def _clear_selection(self) -> None:
        """清空全部勾选。"""
        self._selected = {}
        for card in self.grid_container.findChildren(WorkCard):
            card.blockSignals(True)
            card.set_checked(False)
            card.blockSignals(False)
        self._refresh_batch_bar()

    def _on_batch_add_shelf(self) -> None:
        """批量加入书架。"""
        items = list(self._selected.values())
        if not items:
            return
        self.add_to_shelf_requested.emit(items)
        self._clear_selection()

    def _on_batch_download(self) -> None:
        """批量加入下载。"""
        items = list(self._selected.values())
        if not items:
            return
        self.batch_download_requested.emit(items)
        self._clear_selection()

    def _clear_grid(self) -> None:
        while self.grid_layout.count():
            child = self.grid_layout.takeAt(0)
            w = child.widget()
            if w is not None:
                # 立即解除父子关系（不再渲染/被 findChildren 找到），deleteLater 再释放内存。
                # 只用 deleteLater 时旧卡片在事件循环处理前短暂残留，换源搜索会"闪出旧内容"。
                w.setParent(None)
                w.deleteLater()
        self._work_count = 0
        self._more_pending = False  # 换词/重建网格：中止旧滚动分批链
        self._last_columns = 0  # 重建后需重新应用一次列拉伸

    def _set_filter(self, source_id: str) -> None:
        """来源角标筛选。"""
        if source_id != self._filter_source:
            if not self._filter_source:
                # 首次进入筛选：保存未筛选时的渲染进度，清除筛选后恢复用
                self._saved_unfiltered_shown = self._shown_count
            self._filter_source = source_id
            src = self._manager.get(self._filter_source)
            self.filter_label.setText(f"仅看 {src.source_name}")
        else:
            # 再次点击同源 → 取消筛选，恢复未筛选时的渲染进度
            self._filter_source = ""
            self.filter_label.setText("")
            if self._saved_unfiltered_shown is not None:
                self._shown_count = self._saved_unfiltered_shown
                self._saved_unfiltered_shown = None
        self.filter_bar_widget.setVisible(bool(self._filter_source))
        self._show_results()

    def _clear_filter(self) -> None:
        self._filter_source = ""
        self.filter_label.setText("")
        self.filter_bar_widget.setVisible(False)
        if self._saved_unfiltered_shown is not None:
            self._shown_count = self._saved_unfiltered_shown
            self._saved_unfiltered_shown = None
        self._show_results()

    def _on_scroll(self, value: int) -> None:
        """滚动到 80% → 渲染下一批结果（懒加载，与发现页一致）。

        提前到 80% 而非贴底：滚动到底前下一批已在渲染，视觉无停顿；
        又不一次性把全部结果建卡（防闪屏/封面加载不过来）。

        合并同一事件循环内的连续滚动事件：一次滚动/惯性滑动会连发多个
        valueChanged，逐个同步铺批会让主线程一帧内建几十张卡（卡顿），
        并排队多个 _finish_more_anchor 互相 setValue（内容上下跳动）。
        这里只置位 _more_pending 并延到下一事件循环铺一批，铺批期间到达
        的滚动事件直接合并丢弃。
        """
        vbar = self.scroll.verticalScrollBar()
        if self._restoring_scroll:
            return  # 懒加载锚定/重建恢复滚动位置中，不触发下一批加载
        if vbar.maximum() > 0 and value >= vbar.maximum() * 0.8:
            if self._more_pending:
                return  # 本事件循环已排定一批，连续滚动事件合并
            self._more_pending = True
            QTimer.singleShot(0, self._flush_more)

    def _flush_more(self) -> None:
        """执行被合并的滚动铺批（清除合并标记后同步铺一批）。"""
        self._more_pending = False
        self._load_more_results()

    def refresh(self) -> None:
        """重建源选择菜单（源选择变更后，禁用源不再列出）。"""
        self._rebuild_sources_menu()

    def _rebuild_sources_menu(self) -> None:
        """按当前启用的源重建 src_btn 弹出菜单，保留已勾选状态。

        源列表放在 QScrollArea（限高 SRC_MENU_SCROLL_LIMIT）的 QWidgetAction
        里：源多时菜单内滚动（源一屏放不下不再铺满全屏），源少时菜单随内容
        自适应（含"全部"行 + 源行，不含滚动条）。QMenu 内置滚动只在拆分菜单
        超屏幕时出现，限高需由内嵌滚动区承担，实测 setMaximumHeight 只会截断
        不滚动。点源行（内嵌控件）天然不收起菜单，"全部"仍是 QAction 由
        _NonClosingMenu 保持不收起；点外部 / Escape 收起不变。
        """
        self._src_menu.clear()
        self._src_rows = {}

        enabled = self._manager.enabled_sources()
        enabled_ids = {s.source_id for s in enabled}
        self._selected_sources &= enabled_ids

        all_action = self._src_menu.addAction("全部")
        all_action.setCheckable(True)
        all_action.blockSignals(True)
        all_action.setChecked(self._all_selected)
        all_action.blockSignals(False)
        all_action.setData("all")
        all_action.toggled.connect(self._on_all_source_toggled)
        self._all_action = all_action
        self._src_menu.addSeparator()

        # 源清单：QScrollArea 内嵌（限高滚动），每行一个可勾选按钮
        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)
        for s in enabled:
            checked = self._all_selected or s.source_id in self._selected_sources
            row = QPushButton(("✓ " if checked else "") + (s.source_name or s.source_id))
            row.setCheckable(True)
            row.setChecked(checked)
            row.setCursor(Qt.PointingHandCursor)
            row.setToolTip(s.source_id)
            row.setStyleSheet(
                "QPushButton { text-align: left; border: 1px solid transparent;"
                " border-radius: 6px; padding: 7px 10px;"
                " color: palette(text); background: palette(base); }"
                "QPushButton:hover { background: palette(midlight);"
                " color: palette(text); }"
                "QPushButton:checked { background: palette(highlight);"
                " color: palette(highlightedText); font-weight: bold;"
                " border: 1px solid palette(highlight); }"
            )
            row.clicked.connect(
                lambda checked_, src=s: self._on_source_toggled(src, checked_)
            )
            lay.addWidget(row)
            self._src_rows[s.source_id] = row

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(container)
        scroll.setMinimumWidth(220)
        scroll.setMaximumWidth(320)
        scroll.setMaximumHeight(SRC_MENU_SCROLL_LIMIT)
        scroll.setFrameShape(QFrame.NoFrame)  # 融入菜单外观，不显边框
        wa = QWidgetAction(self._src_menu)
        wa.setDefaultWidget(scroll)
        self._src_menu.addAction(wa)

        self._update_src_button_label()

    def _update_src_button_label(self) -> None:
        """按当前选择就地更新源按钮文字（不重建菜单）。"""
        if self._all_selected:
            self.src_btn.setText("源：全部")
        else:
            n = len(self._selected_sources)
            self.src_btn.setText(f"源：{n}个" if n else "源：无")

    def _sync_all_action(self) -> None:
        """就地同步「全部」QAction 勾选态（blockSignals 防递归，不重建菜单）。"""
        act = getattr(self, "_all_action", None)
        if act is None:
            return
        act.blockSignals(True)
        act.setChecked(self._all_selected)
        act.blockSignals(False)

    def _sync_source_rows(self) -> None:
        """就地同步源行按钮勾选态（blockSignals 防递归，不重建菜单/不重置滚动）。"""
        for sid, row in self._src_rows.items():
            row.blockSignals(True)
            row.setChecked(self._all_selected or sid in self._selected_sources)
            row.blockSignals(False)

    def _clear_results_for_source_change(self) -> None:
        """源选择变更：作废旧搜索、清空旧结果，等待用户点「搜索」。

        - 递增 _search_epoch：旧任务回调（page/finished/封面）因 epoch 过期被丢弃；
        - 清网格与渲染状态（_results / _results_display / _shown_count / _streamed）；
        - 清状态 chip、批量栏、来源筛选；
        - 状态提示改为「已更换源，请点击搜索」，**不发起新搜索**。
        """
        self._search_epoch += 1
        self._clear_grid()
        self._results = []
        self._results_display = None
        self._shown_count = 0
        self._streamed = set()
        self._selected = {}
        self._pending_count = 0
        self._filter_source = ""
        self._saved_unfiltered_shown = None
        self.filter_bar_widget.setVisible(False)
        self.batch_bar.setVisible(False)
        self.select_all_check.blockSignals(True)
        self.select_all_check.setChecked(False)
        self.select_all_check.blockSignals(False)
        while self.status_bar_layout.count():
            item = self.status_bar_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._status_chips = {}
        self.status_bar.setVisible(False)
        self.status_label.setText("已更换源，请点击搜索")

    def _on_all_source_toggled(self, checked: bool) -> None:
        """点「全部」：checked=True 全选，checked=False 全部取消。

        就地更新菜单（不重建 → 不重置菜单滚动位置），且不自动搜索：
        仅更新选择状态/按钮文字并清空旧结果，等用户点「搜索」。
        """
        if checked:
            self._all_selected = True
            self._selected_sources.clear()
        else:
            self._all_selected = False
            self._selected_sources.clear()
        self._sync_all_action()
        self._sync_source_rows()
        self._update_src_button_label()
        self._clear_results_for_source_change()

    def _on_source_toggled(self, source, checked: bool) -> None:
        """勾选/取消单个源（就地更新，不重建菜单、不自动搜索）。

        所有源勾满 → 恢复全选态；全部取消 → 按钮显示「源：无」。
        仅更新选择状态与按钮文字，并清空旧结果；必须点「搜索」才发起搜索。
        """
        enabled = self._manager.enabled_sources()
        enabled_ids = {s.source_id for s in enabled}
        if self._all_selected:
            # 从「全部」进入部分选择：先铺满全部源，再按本次点击增删
            self._selected_sources = set(enabled_ids)
        if checked:
            self._selected_sources.add(source.source_id)
        else:
            self._selected_sources.discard(source.source_id)
        if self._selected_sources >= enabled_ids:
            self._all_selected = True
            self._selected_sources.clear()
        else:
            self._all_selected = False
        self._sync_all_action()
        self._update_src_button_label()
        self._clear_results_for_source_change()
