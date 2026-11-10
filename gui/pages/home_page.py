"""首页 / 仪表盘（HomePage）。

对应 PROJECT_GUIDE.md §6.2 首页功能点：
- 顶部栏（logo + 三态吉祥物 + 主题切换 + 设置入口）
- 统计卡片区（小说/漫画/视频/启用/失效红点）
- 最近搜索（20条，点击重搜）
- 迷你任务进度（EventBus 订阅）
- 空状态（无源）
- 批量更新检测（依赖 bulk_fetch，暂占位「功能暂未实现」）
"""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, QSize, Signal, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
    QListWidget,
    QListWidgetItem,
)

from framework.events import EventBus, EVENT_SOURCE_VISIBILITY_CHANGED
from framework.settings_manager import SettingsManager
from framework.source_manager import SourceManager
from framework.search_history import SearchHistory
from framework.theme_manager import ThemeManager

from gui.components import (
    MascotWidget,
    StatsRow,
    RecentSearches,
    MiniProgress,
    EmptyState,
)
from .base_page import BasePage


_PLAYBACK_ROW_HEIGHT = 34  # 单行高度：够放下 26px 的「删除」按钮 + 上下留白


def _playback_view_height(rows: int) -> int:
    """最近播放列表的期望高度：随条数增长，最少 3 行、最多 9 行。

    行高与 _PLAYBACK_ROW_HEIGHT 对齐，保证每行（标题 + 删除按钮）完整可见；
    条数少时不白占版面，条数多时不会把单行压扁。
    """
    visible = min(max(rows, 3), 9)
    return visible * _PLAYBACK_ROW_HEIGHT + 10


class _PlaybackRow(QWidget):
    """Clickable history row with an independent delete action."""

    clicked = Signal(object)

    def __init__(self, record: dict, parent=None):
        super().__init__(parent)
        self._record = record
        self._full_text = ""
        # 行高自己钉死：QListWidget 用 item.sizeHint 定行高，但行内控件
        # 在被 setItemWidget 接管时可能只拿到很矮的初始高度，会把按钮切掉。
        self.setFixedHeight(_PLAYBACK_ROW_HEIGHT)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 4, 2)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignVCenter)
        self.label = QLabel("")
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # Ignored：标题再长也不撑宽行宽（否则「删除」按钮会被挤出可视区），
        # 超出部分交给 elidedText 打省略号。
        self.label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.label.setMinimumWidth(0)
        layout.addWidget(self.label, stretch=1)
        self.delete_btn = QPushButton("删除")
        self.delete_btn.setFixedWidth(50)
        self.delete_btn.setFixedHeight(26)
        layout.addWidget(self.delete_btn, alignment=Qt.AlignVCenter)

    def set_label(self, text: str) -> None:
        """完整标题存起来，按当前可用宽度做省略号裁剪（悬浮可看全文）。"""
        self._full_text = text
        self.label.setToolTip(text)
        self._sync_label()

    def text(self) -> str:
        return self._full_text

    def _sync_label(self) -> None:
        avail = self.label.width()
        if avail <= 0:
            self.label.setText(self._full_text)
            return
        self.label.setText(
            self.label.fontMetrics().elidedText(
                self._full_text, Qt.TextElideMode.ElideRight, avail
            )
        )

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._sync_label()

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._record)
        super().mousePressEvent(event)


class _NoItemTextDelegate(QStyledItemDelegate):
    """列表项不画自己的文本：这一行的文本只由 _PlaybackRow 的 QLabel 负责。

    QListWidgetItem 的 text() 仍然保留（键盘导航、无障碍、测试要用），但绘制
    权只给行控件。否则同一行会画两遍同样的文本：不透明主题色下行控件/QLabel
    的 bg2 底色把 item 文本盖住，看不出问题；一旦启用背景图，
    `app._bg_overlay_qss()` 把 `QFrame`（QLabel 的基类）改成
    `background: transparent`，item 文本就整条透出来，和行控件文本错开几个
    像素叠在一起 —— 这就是「首页历史记录有重影、去掉背景图就没有」的根因。

    只清 option.text，不动背景/选中态：`QListWidget::item:selected` 的高亮
    仍由代理按 style option 绘制。
    """

    def initStyleOption(self, option, index):  # noqa: N802
        super().initStyleOption(option, index)
        option.text = ""


class _PlaybackList(QListWidget):
    """最近播放列表：行内控件尺寸跟随列表排版。

    QListWidget 通过 setItemWidget 放进去的控件只用一次几何计算，右侧会
    留出多余空白、窗口改变后也不跟手；这里在每次重排后把行控件对齐到
    该行的可视矩形，保证标题可铺满整行、「删除」按钮贴右。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # item 文本不参与绘制（见 _NoItemTextDelegate），避免与行控件文本叠加。
        self.setItemDelegate(_NoItemTextDelegate(self))

    def release_item_widgets(self) -> None:
        """清空列表前显式拆掉行控件。

        `QListWidget.clear()` 只删 item；`setItemWidget` 挂上去的控件不在 model
        里，会作为 viewport 的子控件留在原地。背景图模式下这些残留控件以及它们
        的文本是透明的 —— 留在屏上就是另一份可见文本（重影的第二个来源）。
        """
        for row in range(self.count()):
            widget = self.itemWidget(self.item(row))
            if widget is None:
                continue
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()

    def fit_item_widgets(self) -> None:
        for row in range(self.count()):
            item = self.item(row)
            widget = self.itemWidget(item)
            if widget is None:
                continue
            rect = self.visualRect(self.indexFromItem(item))
            if rect.width() > 0 and rect.height() > 0 and widget.geometry() != rect:
                widget.setGeometry(rect)

    def updateGeometries(self):  # noqa: N802
        super().updateGeometries()
        self.fit_item_widgets()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.fit_item_widgets()


def _open_playback_record(home, record) -> None:
    """Open a history record; kept as a function for lightweight test doubles."""
    from framework.reading_progress import history_active

    progress = getattr(home, "_reading_progress", None)
    if progress is not None:
        record = progress.all().get((record or {}).get("book_url"))
    if not record or not history_active(record):
        return
    if not (getattr(home, "_privacy", None)
            and home._privacy.playback_history_hidden(record)):
        home.playback_requested.emit(record)


class _BrokenSourcesCard(QWidget):
    """不可用源列表卡片：列出不可用的源及其原因（无不可用时隐藏）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("brokenCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self.title = QLabel("⚠ 不可用源")
        self.title.setStyleSheet("font-size: 14px; font-weight: bold; color: #000000;")
        header.addWidget(self.title)
        header.addStretch(1)
        layout.addLayout(header)

        self.list_label = QLabel("")
        self.list_label.setWordWrap(True)
        self.list_label.setStyleSheet(
            "color: #000000; font-size: 12px; line-height: 1.6;"
        )
        layout.addWidget(self.list_label)

    def update_sources(self, broken: list) -> None:
        """刷新不可用源列表。empty → 隐藏卡片。"""
        if not broken:
            self.setVisible(False)
            return
        lines = []
        for b in broken:
            name = b.get("name") or b.get("source_id", "")
            err = b.get("error", "")
            lines.append(f"• {name}：{err}")
        self.list_label.setText("\n".join(lines))
        self.setVisible(True)


class TopBar(QWidget):
    """顶部栏：logo + 吉祥物 + 主题切换 + 设置入口。"""

    settings_requested = Signal()  # → App 层切到「设置」Tab

    def __init__(self, mascot: MascotWidget, theme_manager: ThemeManager, parent=None):
        super().__init__(parent)
        self._theme_manager = theme_manager
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        logo = QLabel("🌸 多源爬虫")
        logo.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(logo)

        self.mascot = mascot
        layout.addWidget(mascot)

        layout.addStretch(1)

        self.theme_btn = QPushButton("🌙 主题")
        self.theme_btn.setFixedWidth(80)
        self.theme_btn.clicked.connect(self._cycle_theme)
        layout.addWidget(self.theme_btn)

        self.settings_btn = QPushButton("⚙ 设置")
        self.settings_btn.setFixedWidth(80)
        self.settings_btn.clicked.connect(self.settings_requested.emit)
        layout.addWidget(self.settings_btn)

    def _cycle_theme(self) -> None:
        """主题循环切换 sakura → mint → midnight → sakura。"""
        keys = ["sakura", "mint", "midnight"]
        cur = self._theme_manager.current_key()
        nxt = keys[(keys.index(cur) + 1) % len(keys)] if cur in keys else "sakura"
        # 切换后由 App 层应用 QSS（通过 on_theme_changed 回调）
        self._theme_manager.switch_to(nxt)


class BatchUpdateCard(QWidget):
    """批量更新检测：对每个有索引的源，对比「最近更新」找新作品。"""

    def __init__(self, bulk_fetch, source_manager, parent=None):
        super().__init__(parent)
        self._bulk_fetch = bulk_fetch
        self._manager = source_manager
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        title = QLabel("批量更新检测")
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)

        self.hint = QLabel("检查各源是否有新作品（需先「抓取全部」建索引）")
        self.hint.setObjectName("statsLabel")
        layout.addWidget(self.hint, stretch=1)

        self.btn = QPushButton("检查更新")
        self.btn.setFixedWidth(130)
        self.btn.clicked.connect(self._on_check)
        layout.addWidget(self.btn)

    def _on_check(self) -> None:
        """后台对全部源跑 check_updates，完成后汇总。"""
        from PySide6.QtCore import QThreadPool, QRunnable, QObject, Signal

        class _CheckSignals(QObject):
            finished = Signal(object)  # 全部结果 list[dict]

        class _CheckTask(QRunnable):
            def __init__(self, bulk_fetch, source):
                super().__init__()
                self.signals = _CheckSignals()
                self._bf = bulk_fetch
                self._src = source

            def run(self) -> None:
                results = []
                sources = [self._src]
                for s in sources:
                    try:
                        results.append(self._bf.check_updates(s))
                    except Exception as exc:  # noqa: BLE001
                        results.append({
                            "source_id": s.source_id,
                            "source_name": s.source_name,
                            "new_works": [],
                            "error": str(exc),
                        })
                try:
                    self.signals.finished.emit(results)
                except RuntimeError:
                    pass

        # 每个源一个任务（并发由 ThreadPool 调度）
        self.btn.setEnabled(False)
        self.hint.setText("检查中...")
        sources = [s for s in self._manager.all() if s.enabled]
        if not sources:
            self.hint.setText("没有启用的源")
            self.btn.setEnabled(True)
            return

        self._tasks = []
        self._batch_results = []
        self._batch_pending = len(sources)
        for s in sources:
            task = _CheckTask(self._bulk_fetch, s)
            task.signals.finished.connect(self._on_source_checked)
            self._tasks.append(task)
            QThreadPool.globalInstance().start(task)

    def _on_source_checked(self, results) -> None:
        """单源完成：累计，全部完成汇总展示。"""
        self._batch_results.extend(results)
        self._batch_pending -= 1
        if self._batch_pending > 0:
            return
        self.btn.setEnabled(True)
        total_new = 0
        parts = []
        for r in self._batch_results:
            new = r.get("new_works") or []
            total_new += len(new)
            if r.get("error") and not new:
                parts.append(f"{r.get('source_name')}: {r['error']}")
            elif new:
                parts.append(f"{r.get('source_name')}: {len(new)}部新")
        if total_new:
            self.hint.setText(
                f"发现 {total_new} 部新作品：{'，'.join(p for p in parts if '部新' in p)}"
            )
            self.hint.setStyleSheet(
                "color: #000000; font-weight: bold;"
            )
        else:
            self.hint.setText("各源暂无新作品")
            self.hint.setStyleSheet("")


class HomePage(BasePage):
    """首页 / 仪表盘。"""

    # 对外信号（App 层接）：点击最近搜索 → 搜索；点添加源 → 源管理；点设置 → 设置Tab
    search_requested = Signal(str)
    add_source_requested = Signal()
    settings_requested = Signal()
    playback_requested = Signal(object)
    retry_runtime_requested = Signal()

    def __init__(
        self,
        source_manager: SourceManager,
        event_bus: EventBus,
        theme_manager: ThemeManager,
        settings: SettingsManager,
        history: SearchHistory,
        bulk_fetch=None,
        parent=None,
        reading_progress=None,
        privacy=None,
    ):
        super().__init__(parent)
        # Keep dashboard copy readable when a dark global theme is selected.
        self.setStyleSheet("QWidget { color: #000000; }")
        self._manager = source_manager
        self._bus = event_bus
        self._theme_manager = theme_manager
        self._settings = settings
        self._history = history
        self._reading_progress = reading_progress
        self._privacy = privacy

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 整页放进滚动区：窗口变矮时页面滚动，而不是把各卡片压扁/裁掉
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        layout.addWidget(self._scroll)

        body = QWidget()
        self._scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(16)

        # 顶部栏
        self.mascot = MascotWidget()
        self.topbar = TopBar(self.mascot, theme_manager)
        self.topbar.settings_requested.connect(self.settings_requested.emit)
        layout.addWidget(self.topbar)

        status_row = QHBoxLayout()
        self.runtime_status = QLabel("")
        self.runtime_status.setObjectName("statsLabel")
        self.runtime_status.setVisible(False)
        status_row.addWidget(self.runtime_status, stretch=1)
        self.runtime_retry = QPushButton("重试")
        self.runtime_retry.setVisible(False)
        self.runtime_retry.clicked.connect(self.retry_runtime_requested.emit)
        status_row.addWidget(self.runtime_retry)
        layout.addLayout(status_row)

        # 统计卡片
        self.stats_row = StatsRow(source_manager)
        layout.addWidget(self.stats_row)

        # 不可用源列表（无不可用时隐藏）
        self.broken_card = _BrokenSourcesCard()
        layout.addWidget(self.broken_card)

        # 最近搜索
        self.recent = RecentSearches(history)
        layout.addWidget(self.recent)
        self.recent.search_clicked.connect(self._on_recent_clicked)
        self.recent.changed.connect(self.refresh)

        playback_header = QHBoxLayout()
        self.playback_title = QLabel("最近播放（0）")
        playback_header.addWidget(self.playback_title)
        playback_header.addStretch(1)
        self.clear_playback_btn = QPushButton("清空")
        self.clear_playback_btn.setFixedHeight(26)
        self.clear_playback_btn.clicked.connect(self._clear_playback_history)
        playback_header.addWidget(self.clear_playback_btn)
        layout.addLayout(playback_header)

        # 最近播放：高度随条数自适应（3~9 行），有富余空间时继续向下伸展，
        # 单行固定 _PLAYBACK_ROW_HEIGHT，避免标题与「删除」按钮被挤成一条缝。
        self.playback_list = _PlaybackList()
        self.playback_list.setUniformItemSizes(True)
        self.playback_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.playback_list.setMinimumHeight(_playback_view_height(0))
        self.playback_list.setMaximumHeight(380)
        self.playback_list.itemClicked.connect(self._on_playback_clicked)
        layout.addWidget(self.playback_list, stretch=1)

        self.playback_empty_hint = QLabel("暂无播放记录")
        self.playback_empty_hint.setStyleSheet(
            "color: #000000; font-size: 12px; padding: 6px 2px;"
        )
        layout.addWidget(self.playback_empty_hint)

        # 迷你任务进度
        self.mini_progress = MiniProgress(event_bus)
        layout.addWidget(self.mini_progress)

        # 批量更新检测（对比索引找新作品）
        self.batch_update = BatchUpdateCard(bulk_fetch, source_manager)
        layout.addWidget(self.batch_update)

        # 空状态（覆盖层，默认隐藏）
        self.empty_state = EmptyState()
        self.empty_state.add_source_clicked.connect(self._on_add_source)
        self.empty_state.setVisible(False)
        layout.addWidget(self.empty_state, stretch=1)

        # 订阅 EventBus → 吉祥物
        if event_bus is not None:
            event_bus.subscribe(self.mascot.on_event)
            event_bus.subscribe(self._on_visibility_changed)

        self._history_timer = QTimer(self)
        self._history_timer.setInterval(1000)
        self._history_timer.timeout.connect(self._refresh_visible_history)
        self._history_timer.start()
        self.refresh()

    def _refresh_visible_history(self):
        if self.isVisible():
            self._refresh_history()

    def _on_visibility_changed(self, event) -> None:
        if getattr(event, "type", "") == EVENT_SOURCE_VISIBILITY_CHANGED:
            self.refresh()

    # ------------------------------------------------------------------ #
    def set_runtime_services(self, source_manager, bulk_fetch) -> None:
        self._manager = source_manager
        self.stats_row._manager = source_manager
        self.batch_update._manager = source_manager
        self._bulk_fetch = bulk_fetch
        self.batch_update._bulk_fetch = bulk_fetch
        self.refresh()

    def set_runtime_status(self, message: str = "") -> None:
        visible = bool(message)
        self.runtime_status.setText(message)
        self.runtime_status.setVisible(visible)
        self.runtime_retry.setVisible(visible)

    def refresh(self) -> None:
        """从数据源刷新本页。"""
        self.stats_row.reload()
        self.broken_card.update_sources(self._manager.list_broken())
        self._refresh_history()
        self._update_empty_state()

    def _refresh_history(self):
        records = self._reading_progress.recent_videos(self._privacy) if self._reading_progress else []
        snapshot = (self._history.recent(), records)
        if getattr(self, "_history_snapshot", None) == snapshot:
            return
        self._history_snapshot = snapshot
        self.recent.reload()
        # 先拆旧行控件再清 item：否则残留的行控件（透明背景下）会和新行叠在一起。
        self.playback_list.release_item_widgets()
        self.playback_list.clear()
        self.playback_title.setText(f"最近播放（{len(records)}）")
        self.playback_list.setMinimumHeight(_playback_view_height(len(records)))
        self.playback_list.setVisible(bool(records))
        self.playback_empty_hint.setVisible(not records)
        for record in records:
            seconds = (record.get("location") or {}).get("seconds")
            stamp = "仅记录集数"
            if isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds >= 0:
                seconds = int(seconds)
                stamp = f"{seconds // 60:02d}:{seconds % 60:02d}"
            label = f"{record.get('title') or record.get('book_url', '')} · {record.get('chapter_title', '')} · {stamp}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, record)
            item.setSizeHint(QSize(0, _PLAYBACK_ROW_HEIGHT))
            self.playback_list.addItem(item)
            # Keep QListWidgetItem text/data for keyboard and existing callers;
            # the delete action is supplied by a lightweight row widget.
            row = _PlaybackRow(record)
            row.set_label(label)
            delete_btn = row.delete_btn
            book_url = record.get("book_url", "")
            delete_btn.clicked.connect(
                lambda _, url=book_url: self._remove_playback_history(url)
            )
            row.clicked.connect(self._on_playback_record)
            self.playback_list.setItemWidget(item, row)
        # 列表隐藏时行矩形还算不出来，showEvent / updateGeometries 里会再对齐一次。
        self.playback_list.fit_item_widgets()

    def _update_empty_state(self) -> None:
        has_sources = len(self._manager.all()) > 0
        self.empty_state.setVisible(not has_sources)
        self.stats_row.setVisible(has_sources)
        self.broken_card.setVisible(has_sources and bool(self._manager.list_broken()))
        self.recent.setVisible(True)
        self.mini_progress.setVisible(has_sources)
        self.batch_update.setVisible(has_sources)

    def _on_recent_clicked(self, keyword: str) -> None:
        """点击最近搜索词 → 切换 Tab 到搜索并预填（App 层接）。"""
        if keyword in self._history.recent():
            self.search_requested.emit(keyword)

    def _on_playback_clicked(self, item):
        _open_playback_record(self, item.data(Qt.UserRole))

    def _on_playback_record(self, record):
        _open_playback_record(self, record)

    def _remove_playback_history(self, book_url: str) -> None:
        if self._reading_progress and self._reading_progress.remove(book_url):
            self._history_snapshot = None
            self._refresh_history()

    def _clear_playback_history(self) -> None:
        if self._reading_progress:
            self._reading_progress.clear_videos()
            self._history_snapshot = None
            self._refresh_history()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def _on_add_source(self) -> None:
        """去添加源 → 切 Tab 到源管理（App 层接）。"""
        self.add_source_requested.emit()

    # ------------------------------------------------------------------ #
    def on_event(self, event) -> None:
        """订阅 EventBus：迷你进度 + 吉祥物。"""
        self.mini_progress._on_event(event)
        self.mascot.on_event(event)

    def on_theme_changed(self, theme: str) -> None:
        self.mascot.update()
