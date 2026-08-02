"""发现界面（DiscoverPage）。

对应 ui-discover.md：
- 顶部源选择器：只列配置了 endpoints.discovery 的源（分组 + 健康灯）
- 分类行：源配置了分类项才显示
- 作品列表：卡片流 + 懒加载滚动（滚动到底加载下一页）
- 详情抽屉占位 / 全量抓取占位（第二次实现）
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from framework.discovery import Discovery, Work
from framework.events import EventBus
from framework.source_manager import SourceManager
from framework.theme_manager import ThemeManager

from gui.components import WorkCard
from .base_page import BasePage


class DiscoverPage(BasePage):
    def __init__(
        self,
        source_manager: SourceManager,
        discovery: Discovery,
        event_bus: EventBus,
        theme_manager: ThemeManager,
        parent=None,
    ):
        super().__init__(parent)
        self._manager = source_manager
        self._discovery = discovery
        self._bus = event_bus
        self._theme_manager = theme_manager
        self._current_source = None
        self._current_page = 0
        self._loading = False
        self._has_more = True
        self._current_cat_url = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # ---- 顶部：源选择器 + 抓取全部 ----
        top = QHBoxLayout()
        self.source_combo = QComboBox()
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        top.addWidget(self.source_combo, stretch=1)

        self.bulk_btn = QPushButton("抓取全部 · 开发中")
        self.bulk_btn.setFixedWidth(130)
        self.bulk_btn.clicked.connect(self._on_bulk_fetch)
        top.addWidget(self.bulk_btn)
        layout.addLayout(top)

        # ---- 分类行 ----
        self.cat_bar = QHBoxLayout()
        layout.addLayout(self.cat_bar)

        # ---- 作品列表（懒加载滚动）----
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.scroll, stretch=1)

        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(0)
        self.scroll.setWidget(self.list_container)

        # 滚动到底触发
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

        # 底部状态
        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color: palette(dark); padding: 8px;")
        layout.addWidget(self.status_label)

        self._reload_sources()

    # ------------------------------------------------------------------ #
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
        self._load_categories()
        self._reset_works()

    # ------------------------------------------------------------------ #
    def _load_categories(self) -> None:
        """加载当前源的分类。未配置分类 → 直接进作品列表。"""
        # 清旧分类按钮
        while self.cat_bar.count():
            child = self.cat_bar.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        if self._current_source is None:
            return
        try:
            cats = self._discovery.list_categories(self._current_source)
        except Exception as exc:
            self.status_label.setText(f"分类加载失败：{exc}")
            return

        if not cats:
            # 无分类 → 用作品列表入口（search.base_url 优先，其次 discovery.list_url）
            search = self._current_source.get_search_config()
            disc = self._current_source.get_discovery_config()
            list_url = (
                search.get("base_url")
                or disc.get("list_url")
                or self._current_source.base_url
            )
            self._current_cat_url = list_url
            return

        # 显示分类按钮 + "全部"
        search = self._current_source.get_search_config()
        disc = self._current_source.get_discovery_config()
        all_url = search.get("base_url") or disc.get("list_url") or self._current_source.base_url
        all_btn = QPushButton("全部")
        all_btn.setCheckable(True)
        all_btn.setChecked(True)
        all_btn.clicked.connect(lambda: self._on_category(None, all_btn))
        self.cat_bar.addWidget(all_btn)
        for cat in cats:
            btn = QPushButton(cat.title)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _, c=cat, b=btn: self._on_category(c, b))
            self.cat_bar.addWidget(btn)
        self.cat_bar.addStretch(1)
        # 默认选中"全部" → 用作品列表入口
        self._current_cat_url = all_url

    def _on_category(self, cat, btn) -> None:
        # 单选互斥
        for i in range(self.cat_bar.count()):
            w = self.cat_bar.itemAt(i).widget()
            if isinstance(w, QPushButton) and w is not btn:
                w.setChecked(False)
        btn.setChecked(True)
        disc = self._current_source.get_discovery_config()
        search = self._current_source.get_search_config()
        if cat is None:
            # 全部 → 作品列表入口
            self._current_cat_url = (
                search.get("base_url")
                or disc.get("list_url")
                or self._current_source.base_url
            )
        else:
            self._current_cat_url = cat.url
        self._reset_works()

    # ------------------------------------------------------------------ #
    def _reset_works(self) -> None:
        """清空列表，重新从第 1 页加载。"""
        self._clear_works()
        self._current_page = 0
        self._has_more = True
        self._load_next_page()

    def _clear_works(self) -> None:
        while self.list_layout.count():
            child = self.list_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def _load_next_page(self) -> None:
        if self._loading or not self._has_more or self._current_source is None:
            return
        if self._current_cat_url is None:
            return
        self._loading = True
        self.status_label.setText("加载中...")
        self._current_page += 1
        try:
            works = self._discovery.list_works(
                self._current_source, self._current_cat_url, self._current_page
            )
        except Exception as exc:
            self.status_label.setText(f"加载失败：{exc}")
            self._has_more = False
            self._loading = False
            return

        if not works:
            self._has_more = False
            self.status_label.setText("到底啦～")
        else:
            self._append_works(works)
            self.status_label.setText(f"已加载 {self._current_page} 页")
        self._loading = False

    def _append_works(self, works) -> None:
        for w in works:
            card = WorkCard(w)
            card.clicked.connect(self._on_work_clicked)
            self.list_layout.addWidget(card)

    def _on_work_clicked(self, work: Work) -> None:
        # 详情抽屉：第二次实现，先提示
        self.status_label.setText(f"[详情待实现] {work.title}")

    def _on_bulk_fetch(self) -> None:
        self.status_label.setText("全量抓取功能开发中，后续完善。")

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
