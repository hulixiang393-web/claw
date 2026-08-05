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
from framework.download_queue import DownloadQueue

from gui.pages.home_page import HomePage
from gui.pages.discover_page import DiscoverPage
from gui.pages.reader_page import ReaderPage
from gui.pages.search_page import SearchPage
from gui.pages.download_page import DownloadPage

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
IMPLEMENTED_TABS = {
    "home", "discover", "reader", "search",
    "download", "library", "source", "settings",
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("多源爬虫框架")
        self.resize(1100, 760)
        # 锁死最小尺寸：防止详情抽屉/内容变化时窗口自动缩放或闪屏
        self.setMinimumSize(900, 640)

        # 基础设施
        base_dir = Path(__file__).resolve().parent.parent
        self.settings = SettingsManager(base_dir / "app_config.json")
        self.event_bus = EventBus()
        self.theme_manager = ThemeManager(self.settings)
        self.source_manager = SourceManager(
            sources_dir=base_dir / "sources",
            health_file=base_dir / "data" / "health.json",
            runtime_settings=self.settings,
        )
        # 登录 Cookie 管理：源管理界面登录 → 存 cookie → 请求自动附带
        from framework.cookie_manager import CookieManager

        self.cookie_manager = CookieManager(base_dir / "data")
        self.source_manager.set_cookie_provider(self.cookie_manager.to_cookie_header)
        self.search_history = SearchHistory(base_dir / "data" / "search_history.json")
        # 阅读进度记忆（当天续读，24h 未入书架则清理）
        self.reading_progress = None
        from framework.reading_progress import ReadingProgress

        self.reading_progress = ReadingProgress(base_dir / "data" / "reading_progress.json")
        self.reading_progress.prune(shelf_cb=None)  # 启动时清理过期记忆

        # 爬取执行链（网络默认值从 settings 接线）
        from framework.http import NetworkDefaults

        self.http = HttpClient(defaults=NetworkDefaults(
            timeout=float(self.settings.get("network", "default_timeout", 10)),
            retries=int(self.settings.get("network", "default_retries", 3)),
            interval_ms=int(self.settings.get("network", "default_request_interval", 0)),
            proxy=self.settings.get("network", "proxy") or None,
            user_agent=self.settings.get("network", "default_user_agent", "") or None,
        ))
        self.parser = Parser()
        self.checker = StructureChecker(
            self.http, self.parser,
            default_strategy=self.settings.get("sources_runtime", "selfcheck_strategy", "soft"),
        )
        self.discovery = Discovery(
            self.http, self.parser, self.checker, health_reporter=self.source_manager
        )
        self.decrypter = Decrypter(self.http)
        self.content = Content(
            self.http, self.parser, self.checker, self.decrypter,
            health_reporter=self.source_manager,
        )
        self.bulk_fetch = BulkFetch(
            self.discovery,
            event_bus=self.event_bus,
            index_dir=base_dir / "data",
        )
        self.search = Search(
            self.http, self.parser, self.discovery,
            concurrent=int(self.settings.get("network", "concurrent_search_sources", 4)),
        )
        self.download_queue = DownloadQueue(
            content=self.content,
            http=self.http,
            settings=self.settings,
            source_manager=self.source_manager,
            event_bus=self.event_bus,
        )
        # 封面缓存预算接线（cover_cache_size_mb）
        from gui.components.cover_loader import CoverLoader

        CoverLoader.instance().configure(
            self.settings.get("ui", "cover_cache_size_mb", 256)
        )

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
            elif key == "download":
                page = self._build_download()
                tab_label = label
            elif key == "library":
                page = self._build_library()
                tab_label = label
            elif key == "source":
                page = self._build_source()
                tab_label = label
            elif key == "settings":
                page = self._build_settings()
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
            reading_progress=self.reading_progress,
            font_scale=float(self.settings.get("ui", "font_scale", 1.0)),
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
        page.download_requested.connect(self._open_download_dialog)
        page.favorite_requested.connect(self._on_favorite)
        return page

    def _on_favorite(self, detail) -> None:
        """收藏/取消收藏：写书架收藏库，并切换按钮状态。"""
        store = getattr(self, "library_store", None)
        if store is None:
            return
        url = getattr(detail, "url", "") or ""
        if not url:
            return
        if store.has(url):
            store.remove(url)
        else:
            store.add(
                getattr(detail, "source_id", ""),
                url,
                getattr(detail, "title", "") or url,
                content_type=getattr(detail, "content_type", ""),
                cover=getattr(detail, "cover", ""),
            )
        # 刷新书架（若已构建）
        page = getattr(self, "library_page", None)
        if page is not None:
            page.refresh()

    def _build_search(self) -> SearchPage:
        page = SearchPage(
            source_manager=self.source_manager,
            search=self.search,
        )
        page.open_requested.connect(self._open_from_search)
        return page

    def _open_from_search(self, source_id: str, url: str, content_type: str) -> None:
        """搜索页点结果卡片 → 打开 reader 播放/阅读。"""
        if self.reader is None:
            return
        self.reader.open(source_id, url, content_type)
        self.tabs.setCurrentIndex(self._tab_index["reader"])

    def _build_download(self) -> DownloadPage:
        from gui.pages.download_page import DownloadPage

        self.download_page = DownloadPage(
            queue=self.download_queue,
            event_bus=self.event_bus,
            settings=self.settings,
        )
        # 下载完成「打开阅读」→ 内置 epub 阅读器
        self.download_page.open_epub_requested.connect(self._open_epub)
        return self.download_page

    def _open_epub(self, path: str) -> None:
        """下载页「打开阅读」→ 用内置 epub 阅读器打开。"""
        if self.reader is None or not path:
            return
        self.reader.open_epub(path)
        self.tabs.setCurrentIndex(self._tab_index["reader"])

    def _build_library(self):
        """书架：本地 epub + 收藏聚合。"""
        from framework.library_store import LibraryStore
        from gui.pages.library_page import LibraryPage

        base_dir = Path(__file__).resolve().parent.parent
        self.library_store = LibraryStore(base_dir / "data" / "library.json")
        output_dir = self.settings.get("download", "output_dir", "downloads")
        shelf_export_dir = self.settings.get("library", "shelf_export_dir", "library")
        self.library_page = LibraryPage(
            output_dir=output_dir,
            library_store=self.library_store,
            reading_progress=self.reading_progress,
            shelf_export_dir=shelf_export_dir,
        )
        # 点本地 epub → 内置阅读器打开（续读）
        self.library_page.open_epub_requested.connect(self._open_epub)
        # 点收藏在线书 → 在线阅读器打开
        self.library_page.open_online_requested.connect(self._open_online_from_shelf)
        return self.library_page

    def _open_online_from_shelf(self, payload) -> None:
        """书架点收藏在线书 → 打开在线阅读器。payload=(source_id, url, content_type)。"""
        if self.reader is None or not isinstance(payload, (tuple, list)) or len(payload) < 3:
            return
        source_id, url, content_type = payload
        self.reader.open(source_id, url, content_type)

    def _build_settings(self):
        """设置页：分区 Tab 覆盖 app_config 全量字段。"""
        from gui.pages.settings_page import SettingsPage

        self.settings_page = SettingsPage(
            settings=self.settings,
            theme_manager=self.theme_manager,
        )
        # 主题切换 → 全局 QSS 刷新（其他页面 on_theme_changed）
        self.settings_page.theme_changed.connect(self._apply_theme_qss)
        # 应用 → 重跑主题 + 字体缩放 + 背景图（实时生效）
        self.settings_page.settings_applied.connect(self._on_settings_applied)
        return self.settings_page
        self.tabs.setCurrentIndex(self._tab_index["reader"])

    def _build_source(self):
        """源管理页：源列表 + 诊断 + 编辑入口。"""
        from gui.pages.source_page import SourcePage

        base_dir = Path(__file__).resolve().parent.parent
        self.source_page = SourcePage(
            source_manager=self.source_manager,
            checker=self.checker,
            sources_dir=base_dir / "sources",
            cookie_manager=self.cookie_manager,
        )
        # 编辑某源 → 打开编辑器对话框
        self.source_page.edit_requested.connect(self._open_source_editor)
        return self.source_page

    def _open_source_editor(self, source_id: str) -> None:
        """打开源编辑器对话框。source_id 为空 → 新建模式。"""
        from gui.components.source_editor import SourceEditor

        base_dir = Path(__file__).resolve().parent.parent
        config = None
        if source_id:
            try:
                config = self.source_manager.get(source_id)
            except Exception:
                config = None
        editor = SourceEditor(
            source_config=config,
            sources_dir=base_dir / "sources",
            parent=self,
        )
        editor.source_saved.connect(self._on_source_saved)
        editor.exec()

    def _on_source_saved(self, source_id: str) -> None:
        """编辑器保存后：重载源 + 刷新列表。"""
        try:
            self.source_manager.load_dir(
                Path(__file__).resolve().parent.parent / "sources"
            )
        except Exception:
            pass
        if hasattr(self, "source_page"):
            self.source_page.refresh()

    def _open_download_dialog(self, detail) -> None:
        """发现详情抽屉「下载」→ 弹章节范围对话框 → 入队。"""
        from gui.components.download_range_dialog import (
            DownloadRangeDialog,
            build_selection,
        )

        if detail is None:
            return
        chapters = list(getattr(detail, "chapters", None) or [])
        total = len(chapters)
        dialog = DownloadRangeDialog(
            detail.title, total, content_type=detail.content_type, parent=self
        )
        if dialog.exec() == dialog.DialogCode.Accepted:
            selection = build_selection(total, dialog.selection())
            self.download_queue.add_task(
                detail, selected=selection, quality=dialog.quality()
            )
            # 跳转到下载页查看进度
            self.tabs.setCurrentWidget(self.download_page)

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
        """应用主题 QSS 到全局 + 背景图合成 + 设置 Palette（兜底防黑色） + 广播事件。"""
        from PySide6.QtGui import QColor, QPalette

        qss = self.theme_manager.generate_qss(theme)

        # 背景图合成（若设置了 background_image）
        bg_qss = self._bg_image_qss_block()
        if bg_qss:
            qss += "\n" + bg_qss

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

    def _bg_image_qss_block(self) -> str:
        """背景图合成：按主题 bg 色 + 用户图片 + 透明度 → 输出缓存 PNG，追加 QSS。

        返回追加 QSS 字符串（空=不启用背景图）。
        缓存路径 data/cache/bg_composed.png；key = (路径, opacity, bg_color)。
        """
        bg_path = self.settings.get("ui", "background_image", "") or ""
        if not bg_path:
            return ""
        from pathlib import Path as _P

        bg_file = _P(bg_path)
        if not bg_file.is_file():
            return ""
        opacity = float(self.settings.get("ui", "background_opacity", 0.6) or 0.6)
        tokens = self.theme_manager.current_tokens()
        bg_color = tokens.get("bg", "#FFF6F9")
        # 缓存 key
        cache_dir = Path(__file__).resolve().parent.parent / "data" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / "bg_composed.png"
        key_file = cache_dir / "bg_composed.key"
        cache_key = f"{bg_path}|{opacity}|{bg_color}"
        # 复用已合成
        try:
            if key_file.is_file() and key_file.read_text(encoding="utf-8").strip() == cache_key:
                if cache_path.is_file() and cache_path.stat().st_size > 0:
                    return self._bg_qss(cache_path)
        except OSError:
            pass
        # 合成：用户图 × opacity 叠加到 bg 纯色（按当前窗口尺寸）
        try:
            from PySide6.QtGui import QColor, QImage, QPainter

            w = max(800, self.width() or 1024)
            h = max(600, self.height() or 768)
            bg_q = QImage(w, h, QImage.Format_RGB32)
            bg_q.fill(QColor(bg_color))
            fg_q = QImage(bg_file)
            if fg_q.isNull():
                return ""
            fg_q = fg_q.scaled(
                w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
            )
            painter = QPainter(bg_q)
            painter.setOpacity(opacity)
            painter.drawImage(0, 0, fg_q)
            painter.end()
            bg_q.save(str(cache_path), "PNG")
            key_file.write_text(cache_key, encoding="utf-8")
        except Exception:  # noqa: BLE001
            return ""
        return self._bg_qss(cache_path)

    @staticmethod
    def _bg_qss(cache_path: Path) -> str:
        """生成背景图 QSS（url 必须正斜杠）。"""
        url = str(cache_path).replace("\\", "/")
        return (
            "QMainWindow, QDialog {\n"
            f"    background-image: url('{url}');\n"
            "    background-position: center;\n"
            "    background-repeat: no-repeat;\n"
            "}\n"
        )

    def _on_settings_applied(self) -> None:
        """设置页点「应用」→ 重跑主题 QSS（含背景图）+ 字体缩放 + 网络默认值 + 封面缓存。"""
        # 重跑主题（含背景图合成 QSS + 字体缩放）
        self._apply_theme_qss(self.theme_manager.current_key())
        # 字体缩放 → 阅读器
        font_scale = float(self.settings.get("ui", "font_scale", 1.0))
        if hasattr(self, "reader") and self.reader is not None:
            self.reader.apply_font_scale(font_scale)
        # 网络默认值 → 已读的 http.defaults 跟不上（构造时快照），但超时等走 per-source
        # 封面缓存预算
        from gui.components.cover_loader import CoverLoader

        CoverLoader.instance().configure(
            self.settings.get("ui", "cover_cache_size_mb", 256)
        )

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
    from framework.logging_setup import setup_logging
    from framework.settings_manager import SettingsManager

    # 日志初始化（诊断区 verbose_logging / log_dir）
    base_dir = Path(__file__).resolve().parent.parent
    setup_logging(SettingsManager(base_dir / "app_config.json"))

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
