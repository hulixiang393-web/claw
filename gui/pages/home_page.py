"""首页 / 仪表盘（HomePage）。

对应 ui-home.md 全部功能点：
- 顶部栏（logo + 三态吉祥物 + 主题切换 + 设置入口）
- 统计卡片区（小说/漫画/视频/启用/失效红点）
- 最近搜索（20条，点击重搜）
- 迷你任务进度（EventBus 订阅）
- 空状态（无源）
- 批量更新检测（依赖 bulk_fetch，暂占位「功能暂未实现」）
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from framework.events import EventBus
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
        self.title.setStyleSheet("font-size: 14px; font-weight: bold; color: palette(text);")
        header.addWidget(self.title)
        header.addStretch(1)
        layout.addLayout(header)

        self.list_label = QLabel("")
        self.list_label.setWordWrap(True)
        self.list_label.setStyleSheet(
            "color: palette(text2); font-size: 12px; line-height: 1.6;"
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
                "color: palette(success); font-weight: bold;"
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

    def __init__(
        self,
        source_manager: SourceManager,
        event_bus: EventBus,
        theme_manager: ThemeManager,
        settings: SettingsManager,
        history: SearchHistory,
        bulk_fetch=None,
        parent=None,
    ):
        super().__init__(parent)
        self._manager = source_manager
        self._bus = event_bus
        self._theme_manager = theme_manager
        self._settings = settings
        self._history = history

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(16)

        # 顶部栏
        self.mascot = MascotWidget()
        self.topbar = TopBar(self.mascot, theme_manager)
        self.topbar.settings_requested.connect(self.settings_requested.emit)
        layout.addWidget(self.topbar)

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

        self.refresh()

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """从数据源刷新本页。"""
        self.stats_row.reload()
        self.broken_card.update_sources(self._manager.list_broken())
        self.recent.reload()
        self._update_empty_state()

    def _update_empty_state(self) -> None:
        has_sources = len(self._manager.all()) > 0
        self.empty_state.setVisible(not has_sources)
        self.stats_row.setVisible(has_sources)
        self.broken_card.setVisible(has_sources and bool(self._manager.list_broken()))
        self.recent.setVisible(has_sources)
        self.mini_progress.setVisible(has_sources)
        self.batch_update.setVisible(has_sources)

    def _on_recent_clicked(self, keyword: str) -> None:
        """点击最近搜索词 → 切换 Tab 到搜索并预填（App 层接）。"""
        self.search_requested.emit(keyword)

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
