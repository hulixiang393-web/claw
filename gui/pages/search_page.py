"""搜索界面（SearchPage）。

对应 ui-search.md：
- 关键词输入 + 类型筛选 + 源范围 + 搜索
- 结果卡片流（复用 WorkCard）
- 来源角标可筛选
- 跨源搜索（后台线程）
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QThreadPool, QRunnable, QObject
from PySide6.QtWidgets import (
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

GRID_COLUMNS = 5


class _SearchSignals(QObject):
    finished = Signal(object, object)  # (results, err)


class _SearchTask(QRunnable):
    """后台跨源搜索。"""

    def __init__(self, search_obj, sources, keyword):
        super().__init__()
        self.signals = _SearchSignals()
        self._search = search_obj
        self._sources = sources
        self._keyword = keyword

    def run(self) -> None:
        results, err = [], None
        try:
            results = self._search.search_type(self._sources, self._keyword)
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(results, err)
        except RuntimeError:
            pass


class SearchPage(BasePage):
    search_clicked = Signal(str)  # 搜索触发（首页接）

    def __init__(self, source_manager: SourceManager, search: Search, parent=None):
        super().__init__(parent)
        self._manager = source_manager
        self._search = search
        self._results = []
        self._filter_source = ""

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
        for s in source_manager.all():
            self.src_combo.addItem(s.source_name, s)
        self.search_btn = QPushButton("搜索")
        self.search_btn.clicked.connect(self._on_search)
        top.addWidget(self.keyword_input, stretch=1)
        top.addWidget(self.type_combo)
        top.addWidget(self.src_combo)
        top.addWidget(self.search_btn)
        layout.addLayout(top)

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

        # 选择目标源
        sources = []
        selected_type = self.type_combo.currentData()
        selected_src = self.src_combo.currentData()
        if selected_src:
            sources = [selected_src]
        else:
            sources = self._manager.all()
            if selected_type:
                sources = [s for s in sources if s.content_type == selected_type]

        # 后台搜索
        task = _SearchTask(self._search, sources, keyword)
        task.signals.finished.connect(self._on_results_loaded)
        QThreadPool.globalInstance().start(task)

    def _on_results_loaded(self, results, err) -> None:
        if err:
            self.status_label.setText(f"搜索失败：{err}")
            return
        self._results = results
        self._show_results()
        if not results:
            self.status_label.setText("搜不到这个哦，换个词试试？")
        else:
            self.status_label.setText(f"共 {len(results)} 条结果")

    def _show_results(self) -> None:
        self._clear_grid()
        items = self._results
        if self._filter_source:
            items = [r for r in items if r.source_id == self._filter_source]
        for r in items:
            card = WorkCard(r)
            self.grid_layout.addWidget(card)

    def _clear_grid(self) -> None:
        while self.grid_layout.count():
            child = self.grid_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

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
        vbar = self.scroll.verticalScrollBar()
        if value >= vbar.maximum() - 200:
            pass  # 搜索结果不分懒加载（一次返回全部）

    def refresh(self) -> None:
        pass
