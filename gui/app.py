"""应用入口（App）。

- 主窗口 + 导航栏（首页 | 发现 | 搜索 | 阅读 | 下载 | 书架 | 源管理 | 设置）
- 主题加载与实时切换
- 全局快捷键（Ctrl+1~8 切 Tab 等）
- 未实现界面在导航栏标注「·开发中」
- 首页内部导航（⚙设置 / 去添加源 / 最近搜索）联动主窗口 Tab
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from framework.events import EventBus, Event, EVENT_THEME_CHANGED
from framework.settings_manager import SettingsManager
from framework.source_manager import SourceManager
from framework.search_history import SearchHistory
from framework.theme_manager import ThemeManager
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.discovery import Discovery
from framework.content import Content
from framework.decrypter import Decrypter
from framework.bulk_fetch import BulkFetch
from framework.search import Search

from gui.pages.home_page import HomePage
from gui.pages.discover_page import DiscoverPage
from gui.pages.reader_page import ReaderPage
from gui.pages.search_page import SearchPage

# 导航栏顺序（对应 ui-index.md）
TABS = [
    ("首页", "home"),
    ("发现", "discover"),
    ("搜索", "search"),
    ("阅读", "reader"),
    ("下载", "download"),
    ("书架", "library"),
    ("源管理", "source"),
    ("设置", "settings"),
]

# 已实现界面
IMPLEMENTED_TABS = {"home", "discover", "reader", "search"}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("多源爬虫框架")
        self.resize(1100, 760)

        # 基础设施
        base_dir = Path(__file__).resolve().parent.parent
        self.settings = SettingsManager(base_dir / "app_config.json")
        self.event_bus = EventBus()
        self.theme_manager = ThemeManager(self.settings)
        self.source_manager = SourceManager(
            sources_dir=base_dir / "sources",
            health_file=base_dir / "data" / "health.json",
        )
        self.search_history = SearchHistory(base_dir / "data" / "search_history.json")

        # 爬取执行链
        self.http = HttpClient()
        self.parser = Parser()
        self.checker = StructureChecker(self.http, self.parser)
        self.discovery = Discovery(self.http, self.parser, self.checker)
        self.decrypter = Decrypter(self.http)
        self.content = Content(self.http, self.parser, self.checker, self.decrypter)
        self.bulk_fetch = BulkFetch(
            self.discovery,
            event_bus=self.event_bus,
            index_dir=base_dir / "data",
        )
        self.search = Search(self.http, self.parser, self.discovery)

        # Tab 索引映射
        self._tab_index = {key: i for i, (_, key) in enumerate(TABS)}

        # 中心区
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs)

        self._build_pages()

        # 主题
        self.theme_manager.on_theme_changed(self._apply_theme_qss)
        self._apply_theme_qss(self.theme_manager.current_key())

        # 全局快捷键
        self._install_shortcuts()

    # ------------------------------------------------------------------ #
    def _build_pages(self) -> None:
        """构建导航栏各 Tab。未实现界面在标签后标注「·开发中」。"""
        self.reader = None  # 占位，_build_reader 时创建
        for label, key in TABS:
            if key == "home":
                page = self._build_home()
                tab_label = label
            elif key == "discover":
                page = self._build_discover()
                tab_label = label
            elif key == "reader":
                page = self._build_reader()
                tab_label = label
            elif key == "search":
                page = self._build_search()
                tab_label = label
            elif key in IMPLEMENTED_TABS:
                page = self._build_home()  # 其他已实现暂用首页占位
                tab_label = label
            else:
                page = self._build_placeholder(label)
                tab_label = f"{label}·开发中"
            self.tabs.addTab(page, tab_label)

    def _build_reader(self) -> ReaderPage:
        from gui.pages.reader_page import ReaderPage

        self.reader = ReaderPage(
            source_manager=self.source_manager,
            content=self.content,
        )
        return self.reader

    def _build_discover(self) -> DiscoverPage:
        page = DiscoverPage(
            source_manager=self.source_manager,
            discovery=self.discovery,
            content=self.content,
            bulk_fetch=self.bulk_fetch,
            event_bus=self.event_bus,
            theme_manager=self.theme_manager,
        )
        page.read_requested.connect(self._open_reader)
        return page

    def _build_search(self) -> SearchPage:
        return SearchPage(
            source_manager=self.source_manager,
            search=self.search,
        )

    def _open_reader(self, detail) -> None:
        """从发现详情抽屉「开始阅读」→ 打开阅读器。"""
        if detail is None or self.reader is None:
            self.tabs.setCurrentIndex(self._tab_index["reader"])
            return
        self.reader.open(
            detail.source_id,
            detail.url,
            detail.content_type,
            detail.chapters[0].url if detail.chapters else "",
        )
        self.tabs.setCurrentIndex(self._tab_index["reader"])

    def _build_home(self) -> HomePage:
        page = HomePage(
            source_manager=self.source_manager,
            event_bus=self.event_bus,
            theme_manager=self.theme_manager,
            settings=self.settings,
            history=self.search_history,
        )
        # 首页内部导航联动主窗口 Tab
        page.add_source_requested.connect(
            lambda: self.tabs.setCurrentIndex(self._tab_index["source"])
        )
        page.settings_requested.connect(
            lambda: self.tabs.setCurrentIndex(self._tab_index["settings"])
        )
        page.search_requested.connect(self._jump_to_search)
        return page

    def _jump_to_search(self, keyword: str) -> None:
        """跳转搜索 Tab（搜索页实现后接线预填关键词）。"""
        self.tabs.setCurrentIndex(self._tab_index["search"])
        # TODO: 搜索页实现后调用 SearchPage.fill_keyword(keyword)

    def _build_placeholder(self, label: str) -> QWidget:
        """未实现界面的占位页。"""
        w = QWidget()
        v = QVBoxLayout(w)
        v.addStretch(1)
        txt = QLabel(f"{label} · 功能暂未实现")
        txt.setAlignment(Qt.AlignCenter)
        txt.setStyleSheet("color: #7D6B7A; font-size: 16px;")
        v.addWidget(txt)
        hint = QLabel("该界面将在后续迭代实现")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color: #B5A8BA; font-size: 12px;")
        v.addWidget(hint)
        v.addStretch(1)
        return w

    # ------------------------------------------------------------------ #
    def _apply_theme_qss(self, theme: str) -> None:
        """应用主题 QSS 到全局 + 设置 Palette（兜底防黑色） + 广播事件。"""
        from PySide6.QtGui import QColor, QPalette

        qss = self.theme_manager.generate_qss(theme)
        app = QApplication.instance()
        app.setStyleSheet(qss)

        # 设置全局 Palette：即使某控件未被 QSS 覆盖，也用主题主色而非黑色
        tokens = self.theme_manager.current_tokens()
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(tokens["bg"]))
        palette.setColor(QPalette.WindowText, QColor(tokens["text"]))
        palette.setColor(QPalette.Base, QColor(tokens["bg2"]))
        palette.setColor(QPalette.AlternateBase, QColor(tokens["bg3"]))
        palette.setColor(QPalette.Text, QColor(tokens["text"]))
        palette.setColor(QPalette.Button, QColor(tokens["bg2"]))
        palette.setColor(QPalette.ButtonText, QColor(tokens["text"]))
        palette.setColor(QPalette.Highlight, QColor(tokens["accent"]))
        palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
        app.setPalette(palette)

        self.event_bus.emit(Event(EVENT_THEME_CHANGED, {"theme": theme}))
        # 通知各页
        for i in range(self.tabs.count()):
            page = self.tabs.widget(i)
            if hasattr(page, "on_theme_changed"):
                page.on_theme_changed(theme)

    # ------------------------------------------------------------------ #
    def _install_shortcuts(self) -> None:
        """全局快捷键（Ctrl+1~8 切 Tab / Ctrl+Shift+S 设置 等）。"""
        from PySide6.QtGui import QKeySequence, QShortcut

        for idx, (_, key) in enumerate(TABS):
            if idx < 8:
                sc = QShortcut(QKeySequence(f"Ctrl+{idx + 1}"), self)
                sc.activated.connect(lambda i=idx: self.tabs.setCurrentIndex(i))
        # Ctrl+Shift+S 设置
        settings_tab = self._tab_index["settings"]
        sc = QShortcut(QKeySequence("Ctrl+Shift+S"), self)
        sc.activated.connect(lambda: self.tabs.setCurrentIndex(settings_tab))


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
