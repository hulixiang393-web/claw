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
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from framework.search import Search, SearchResult
from framework.source_manager import SourceManager

from gui.components import WorkCard
from .base_page import BasePage

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
    finished = Signal(object, object)  # (source, {result.url: data_uri})


class _SearchCoverDecryptTask(QRunnable):
    """后台批量解密封面（加密站搜索结果，18mh 类）。

    搜索返回时封面是加密 URL 直接加载不出图，后台复用 discovery 的
    AES 解密（decrypt_search_covers）后，GUI 把 data URI 回填刷新。
    非加密源不启动此任务（decrypt_search_covers 返回 {}）。
    """

    def __init__(self, search_obj, source, results):
        super().__init__()
        self.signals = _SearchCoverDecryptSignals()
        self._search = search_obj
        self._source = source
        self._results = results

    def run(self) -> None:
        covers = {}
        try:
            covers = self._search.decrypt_search_covers(self._source, self._results)
        except Exception:  # noqa: BLE001
            covers = {}
        try:
            self.signals.finished.emit(self._source, covers)
        except RuntimeError:
            pass


class SearchPage(BasePage):
    search_clicked = Signal(str)  # 搜索触发（首页接）
    open_requested = Signal(str, str, str)  # (source_id, url, content_type) 打开作品
    # 批量操作（ui-search.md #8）：加入书架 / 加入下载
    add_to_shelf_requested = Signal(object)   # list[SearchResult]
    batch_download_requested = Signal(object)  # list[SearchResult]

    def __init__(self, source_manager: SourceManager, search: Search, parent=None):
        super().__init__(parent)
        self._manager = source_manager
        self._search = search
        self._results = []
        self._filter_source = ""
        self._status_chips: dict = {}  # source_id → (QLabel, QLabel状态) 或组合控件
        self._pending_count = 0  # 未完成搜索的源数
        self._work_count = 0  # 当前网格卡片计数（追加/重建共用）
        self._shown_count = 0  # 已渲染到 _results 的条数（分批懒加载用）
        self._page_size = 20  # 每批渲染条数（搜索结果分批，防一次几百张卡片卡顿）
        self._preload_depth = 2  # 预加载缓冲批次：首屏 1 批 + 再预加载 2 批填满视口（防一次建太多卡片）
        self._selected: dict = {}  # 勾选批量：url → SearchResult
        self._select_mode = False  # 是否进入勾选模式
        self._cover_tasks = []  # 封面解密后台任务引用（防 GC）
        self._cover_decrypt_submitted: set = set()  # 已提交封面解密的结果 url（防逐页/done 重复触发）
        self._streamed: set = set()  # 已边抓边渲染的源（finished 不重复追加）
        self._search_epoch = 0  # 搜索会话标记：换源/换关键词自增，过期任务结果丢弃

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
        self.src_combo = QComboBox()
        self.src_combo.addItem("全部源", "")
        for s in source_manager.enabled_sources():
            self.src_combo.addItem(s.source_name, s)
        self.merge_check = QCheckBox("合并相似")
        self.merge_check.setToolTip("按书名+作者模糊匹配，合并同书多源版本（默认关）")
        self.merge_check.setChecked(False)
        self.merge_check.toggled.connect(self._on_merge_toggled)
        self.search_btn = QPushButton("搜索")
        self.search_btn.clicked.connect(self._on_search)
        top.addWidget(self.keyword_input, stretch=1)
        top.addWidget(self.type_combo)
        top.addWidget(self.src_combo)
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
        self.filter_bar_widget.setVisible(False)
        self.status_label.setText("搜索中...")
        self._clear_grid()
        self._results = []
        self._shown_count = 0
        self._cover_decrypt_submitted = set()
        self._selected = {}
        self.batch_bar.setVisible(False)
        self.select_all_check.setChecked(False)

        # 选择目标源
        selected_type = self.type_combo.currentData()
        selected_src = self.src_combo.currentData()
        if selected_src:
            sources = [selected_src]
        else:
            sources = self._manager.enabled_sources()
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

    def _on_covers_decrypted(self, source, covers) -> None:
        """封面解密完成：回写 SearchResult.cover + 刷新对应卡片。

        covers = {result.url: data_uri}。合并模式下卡片 work 是合并代表
        （url 与原始结果一致），按 url 匹配刷新。
        """
        if not covers:
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
        task = _SearchCoverDecryptTask(self._search, source, pending)
        task.signals.finished.connect(self._on_covers_decrypted)
        self._cover_tasks.append(task)  # 持引用防 GC
        QThreadPool.globalInstance().start(task)

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
        self._clear_grid()
        # 分批渲染：先渲染第一批
        self._append_displayed_batch()

    def _append_displayed_batch(self) -> None:
        """按 _results_display 渲染下一批（合并后走此路径）。

        每次只渲染一批（_page_size 条），不自动补足视口——防止搜索完
        一次性插入大量卡片闪屏。首屏一屏内容 + 滚动到 80% 逐批加载
        （_on_scroll），与发现页懒加载一致。
        """
        display = getattr(self, "_results_display", None)
        if display is None:
            return
        cols = self._columns()
        new_shown = min(len(display), self._shown_count + self._page_size)
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
        未渲染，统一合并后渲染首屏，剩余滚动懒加载。
        """
        if not self._results:
            self.status_label.setText("搜不到这个哦，换个词试试？")
            return
        if self.merge_check.isChecked():
            self._rebuild_results_with_merge()
        elif self._shown_count == 0:
            # 兜底：边抓边显示异常（on_page 缺省）→ 直接渲染首屏
            self._append_results(self._results)
        self._update_batch_status()

    def _update_batch_status(self) -> None:
        """更新状态文本：已显示 X / 共 Y 条。"""
        total = len(self._results)
        if self._filter_source:
            # 筛选时显示筛选后总数（_results 未过滤，需单独算）
            filtered = sum(1 for r in self._results if r.source_id == self._filter_source)
            self.status_label.setText(f"共 {filtered} 条结果（仅看此源）")
        elif self._shown_count >= total:
            self.status_label.setText(f"共 {total} 条结果")
        else:
            self.status_label.setText(f"已显示 {self._shown_count} / {total} 条，滚动加载更多...")

    def _append_results(self, items) -> None:
        """把一批结果卡片追加到网格尾部（按当前列数排），首屏懒加载。

        结果累积到 _results（_on_source_page），这里只渲染到首屏
        （_page_size 条）；不自动补足视口——避免边抓边显示时一次性
        插入几百张卡片导致闪屏/跳动（与发现页懒加载一致）。其余结果
        留待滚动到 80%（_on_scroll）再逐批渲染。
        """
        if not items:
            return
        display = self._current_display()
        cols = self._columns()
        # 首屏只渲染到 _page_size 条上限；后续页到达只累积 _results，
        # 超过上限不再追着渲染（防边抓边显示一次性插入大量卡片闪屏）
        new_shown = min(len(display), self._page_size)
        while self._shown_count < new_shown:
            r = display[self._shown_count]
            self._append_card(r, cols)
            self._shown_count += 1
        self._apply_column_stretch(cols)
        self._update_batch_status()
        self._maybe_preload_results()

    def _current_display(self):
        """当前渲染源：合并后为 _results_display，否则 _results。"""
        display = getattr(self, "_results_display", None)
        if display is None:
            display = self._results
        return display

    def _load_more_results(self) -> None:
        """滚动加载下一批结果。"""
        display = self._current_display()
        if self._shown_count >= len(display):
            return
        cols = self._columns()
        new_shown = min(len(display), self._shown_count + self._page_size)
        while self._shown_count < new_shown:
            r = display[self._shown_count]
            self._append_card(r, cols)
            self._shown_count += 1
        self._apply_column_stretch(cols)
        self._update_batch_status()
        self._maybe_preload_results()

    def _maybe_preload_results(self) -> None:
        """视口未填满 → 继续渲染下一批（有限预加载深度，与发现页 _maybe_preload 同思路）。

        首屏一批渲染完视口没填满（结果较少时）继续补渲染，让首屏尽快填满、
        滚动流畅；只预加载有限批次（_preload_depth 缓冲），到顶后停止，剩下
        交给滚动 80%（_on_scroll）。不自动无限补足——防止搜索完成/边抓边显示
        瞬间一次性建大量卡片闪屏（历史 bug，见 _append_results 注释）。

        QTimer.singleShot(0) 延到布局完成后再查视口：addWidget 后 scrollbar
        范围要等下一轮布局才更新，立即判断会误以为未填满、一次性预加载过多
        卡片闪屏。布局稳定后按真实视口填满度决定是否补一批。
        """
        display = self._current_display()
        if self._shown_count >= len(display):
            return
        if self._shown_count >= self._page_size * (1 + self._preload_depth):
            return  # 预加载深度到顶（首屏 1 批 + 缓冲 _preload_depth 批），等滚动
        if self.scroll.verticalScrollBar().maximum() >= self.scroll.height():
            return  # 视口已填满，等滚动触发
        QTimer.singleShot(0, self._load_more_results)

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
        """每列等宽，卡片均匀分布。"""
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
        self._clear_grid()
        display = self._current_display()
        items = display
        if self._filter_source:
            items = [r for r in items if r.source_id == self._filter_source]
        cols = self._columns()
        # 筛选时全量渲染（结果通常较少）；无筛选时只渲染已加载批
        shown = items if self._filter_source else display[:self._shown_count]
        for r in shown:
            self._append_card(r, cols)
        self._apply_column_stretch(cols)
        self._update_batch_status()

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
        """创建勾选模式卡片并连接信号。"""
        card = WorkCard(r, selectable=True)
        card.clicked.connect(lambda _, rr=r: self._emit_open(rr))
        card.checked.connect(self._on_card_checked)
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
        display = self._current_display()
        total = len(display)
        self.select_all_check.setChecked(total > 0 and n == total)
        self.select_all_check.blockSignals(False)
        self.batch_bar.setVisible(n > 0)

    def _on_select_all(self, checked: bool) -> None:
        """全选/取消全选当前已显示的结果。"""
        if checked:
            display = self._current_display()
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

    def _set_filter(self, source_id: str) -> None:
        """来源角标筛选。"""
        self._filter_source = source_id if source_id != self._filter_source else ""
        if self._filter_source:
            src = self._manager.get(self._filter_source)
            self.filter_label.setText(f"仅看 {src.source_name}")
        self.filter_bar_widget.setVisible(bool(self._filter_source))
        self._show_results()

    def _clear_filter(self) -> None:
        self._filter_source = ""
        self.filter_label.setText("")
        self.filter_bar_widget.setVisible(False)
        self._show_results()

    def _on_scroll(self, value: int) -> None:
        """滚动到 80% → 渲染下一批结果（懒加载，与发现页一致）。

        提前到 80% 而非贴底：滚动到底前下一批已在渲染，视觉无停顿；
        又不一次性把全部结果建卡（防闪屏/封面加载不过来）。
        """
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() > 0 and value >= vbar.maximum() * 0.8:
            self._load_more_results()

    def refresh(self) -> None:
        """重建源范围下拉（源选择变更后，禁用源不再列出）。"""
        self._rebuild_src_combo()

    def _rebuild_src_combo(self) -> None:
        """按当前启用的源重建 src_combo，尽量保持原选中源。"""
        current = self.src_combo.currentData()
        self.src_combo.blockSignals(True)
        self.src_combo.clear()
        self.src_combo.addItem("全部源", "")
        for s in self._manager.enabled_sources():
            self.src_combo.addItem(s.source_name, s)
        idx = self.src_combo.findData(current) if current is not None else 0
        self.src_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.src_combo.blockSignals(False)
