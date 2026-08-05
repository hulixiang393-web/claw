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
from PySide6.QtGui import QPainter
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


class _CentralArea(QWidget):
    """中央区容器：paintEvent 直接绘制背景图（最底层）。

    Qt 的 QSS `background-image` 对普通 QWidget 不生效（即使 WA_StyledBackground
    也只画 background-color），所以背景图必须用 paintEvent 画——这是可靠方案：
    先画背景图铺满，再让 QTabWidget 等子控件（透明/半透明）浮在其上。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bg_pixmap = None  # 由 App 层注入（合成后的背景图）

    def set_bg_pixmap(self, pixmap) -> None:
        """注入背景图（QPixmap 或 None 关闭）。"""
        self._bg_pixmap = pixmap
        self.update()  # 触发重绘

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._bg_pixmap is not None and not self._bg_pixmap.isNull():
            painter = QPainter(self)
            # 背景图铺满整个区域（KeepAspectRatioByExpanding + 居中裁剪）
            painter.drawPixmap(self.rect(), self._bg_pixmap)
            painter.end()
        super().paintEvent(event)


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

        # 中心区（paintEvent 直接绘制背景图，最可靠）
        central = _CentralArea()
        central.setObjectName("centralArea")
        self._central_area = central
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

        # 启动自动诊断：后台对所有启用源跑一次 selfcheck，健康灯一开始就准确
        self._schedule_startup_diag()

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
        # 阅读器「收藏」→ 写书架收藏库（与发现详情抽屉同一入口 _on_favorite）
        self.reader.favorite_requested.connect(self._on_favorite)
        # 收藏判断回调：LibraryStore.has(url)（书架库未构建时先构建）
        self.reader.set_favorite_checker(self._favorite_has)
        # 启动即应用阅读区独立背景/字号（ui-reader #12）
        self.reader.apply_reading_style(
            self.settings.get("ui", "reading_bg", "") or "",
            int(self.settings.get("ui", "reading_font_size", 0) or 0),
        )
        return self.reader

    def _favorite_has(self, url: str) -> bool:
        """收藏判断：书架库是否已收藏该书。库未构建则先构建。"""
        store = self._ensure_library_store()
        if store is None or not url:
            return False
        try:
            return store.has(url)
        except Exception:  # noqa: BLE001
            return False

    def _ensure_library_store(self):
        """确保书架收藏库已构建（reader 在 library 前构建，需延迟获取）。"""
        store = getattr(self, "library_store", None)
        if store is not None:
            return store
        try:
            from framework.library_store import LibraryStore

            base_dir = Path(__file__).resolve().parent.parent
            self.library_store = LibraryStore(base_dir / "data" / "library.json")
            return self.library_store
        except Exception:  # noqa: BLE001
            return None

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
        store = self._ensure_library_store()
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
        # 刷新阅读器收藏按钮状态（收藏/取消后 ★/☆ 同步）
        if getattr(self, "reader", None) is not None:
            self.reader.refresh_favorite_state()

    # ------------------------------------------------------------------ #
    def _on_batch_add_shelf(self, items) -> None:
        """搜索页勾选批量 → 加入书架（写收藏库）。"""
        store = self._ensure_library_store()
        if store is None or not items:
            return
        added = 0
        for r in items:
            url = getattr(r, "url", "") or ""
            if not url or store.has(url):
                continue
            store.add(
                getattr(r, "source_id", ""),
                url,
                getattr(r, "title", "") or url,
                content_type="",
                cover=getattr(r, "cover", ""),
                author=getattr(r, "author", ""),
            )
            added += 1
        # 刷新书架（若已构建）
        page = getattr(self, "library_page", None)
        if page is not None:
            page.refresh()
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.information(
            self, "加入书架", f"已收藏 {added} 部作品到书架。"
        )

    def _on_batch_download(self, items) -> None:
        """搜索页勾选批量 → 加入下载队列。

        为每条结果后台拉详情（拿章节列表），全部就绪后入队。
        """
        if not items:
            return
        from PySide6.QtCore import QThreadPool, QRunnable, QObject, Signal

        class _ShelfDlSignals(QObject):
            done = Signal(object)  # (list[(result, detail_or_None)])

        class _FetchDetailTask(QRunnable):
            def __init__(self, content, source, url):
                super().__init__()
                self.signals = _ShelfDlSignals()
                self._content = content
                self._source = source
                self._url = url

            def run(self) -> None:
                detail = None
                try:
                    detail = self._content.fetch_detail(self._source, self._url)
                except Exception:  # noqa: BLE001
                    detail = None
                try:
                    self.signals.done.emit((self._source, self._url, detail))
                except RuntimeError:
                    pass

        self._dl_tasks = []
        self._dl_results = []
        self._dl_pending = len(items)
        for r in items:
            try:
                source = self.source_manager.get(getattr(r, "source_id", ""))
            except Exception:
                source = None
            if source is None:
                self._dl_pending -= 1
                continue
            task = _FetchDetailTask(self.content, source, getattr(r, "url", ""))
            task.signals.done.connect(self._on_batch_dl_detail)
            self._dl_tasks.append(task)
            QThreadPool.globalInstance().start(task)

    def _on_batch_dl_detail(self, payload) -> None:
        """批量下载：一条详情就绪，累计；全部就绪后入队。"""
        source, url, detail = payload
        self._dl_results.append((source, url, detail))
        self._dl_pending -= 1
        if self._dl_pending > 0:
            return
        # 全部就绪：对每个有章节的结果入队
        ok = 0
        for source, url, detail in self._dl_results:
            if detail is None or not getattr(detail, "chapters", None):
                continue
            self.download_queue.add_task(detail)
            ok += 1
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.information(
            self, "加入下载", f"已加入下载队列 {ok} 部作品。"
        )
        # 跳转下载页查看进度
        if hasattr(self, "download_page"):
            self.tabs.setCurrentWidget(self.download_page)

    def _build_search(self) -> SearchPage:
        page = SearchPage(
            source_manager=self.source_manager,
            search=self.search,
        )
        page.open_requested.connect(self._open_from_search)
        page.add_to_shelf_requested.connect(self._on_batch_add_shelf)
        page.batch_download_requested.connect(self._on_batch_download)
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
        from gui.pages.library_page import LibraryPage

        base_dir = Path(__file__).resolve().parent.parent
        # 复用收藏库（阅读器可能已先构建），避免重复读文件
        self.library_store = self._ensure_library_store()
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
        """书架点收藏在线书 → 打开在线阅读器。payload=(source_id, url, content_type)。

        content_type 为空时从源配置兜底推断（兼容早期收藏未存类型的记录）。
        """
        if self.reader is None or not isinstance(payload, (tuple, list)) or len(payload) < 3:
            return
        source_id, url, content_type = payload
        if not content_type:
            try:
                content_type = self.source_manager.get(source_id).content_type
            except Exception:  # noqa: BLE001
                content_type = ""
        self.reader.open(source_id, url, content_type)
        # 跳转阅读 Tab
        self.tabs.setCurrentIndex(self._tab_index["reader"])

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
        from framework.preview import Preview

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
            preview=Preview(self.http, self.parser),
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
            bulk_fetch=self.bulk_fetch,
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
        """跳转搜索 Tab + 预填关键词并自动搜索（ui-home #3 最近搜索一键重搜）。"""
        self.tabs.setCurrentIndex(self._tab_index["search"])
        page = self.tabs.widget(self._tab_index["search"])
        if page is not None and hasattr(page, "fill_keyword"):
            page.fill_keyword(keyword)

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
        """背景图合成：按主题 bg 色 + 用户图片 + 透明度 → 合成 QPixmap 注入中央区。

        背景图用 paintEvent 绘制（QWidget 的 QSS background-image 不生效），
        本方法负责：合成 PNG（缓存）→ 加载 QPixmap → 注入 self._central_area。

        返回追加 QSS 字符串（半透明覆盖块；空=不启用背景图）。
        """
        bg_path = self.settings.get("ui", "background_image", "") or ""
        if not bg_path:
            self._clear_bg_pixmap()
            return ""
        from pathlib import Path as _P

        bg_file = _P(bg_path)
        if not bg_file.is_file():
            self._clear_bg_pixmap()
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
        cache_hit = False
        # 复用已合成
        try:
            if key_file.is_file() and key_file.read_text(encoding="utf-8").strip() == cache_key:
                if cache_path.is_file() and cache_path.stat().st_size > 0:
                    cache_hit = True
        except OSError:
            pass
        if not cache_hit:
            # 合成：用户图 × opacity 叠加到 bg 纯色（按当前窗口尺寸）
            try:
                from PySide6.QtGui import QColor, QImage, QPainter as _QP

                w = max(800, self.width() or 1024)
                h = max(600, self.height() or 768)
                bg_q = QImage(w, h, QImage.Format_RGB32)
                bg_q.fill(QColor(bg_color))
                fg_q = QImage(bg_file)
                if fg_q.isNull():
                    self._clear_bg_pixmap()
                    return ""
                fg_q = fg_q.scaled(
                    w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
                )
                painter = _QP(bg_q)
                painter.setOpacity(opacity)
                painter.drawImage(0, 0, fg_q)
                painter.end()
                bg_q.save(str(cache_path), "PNG")
                key_file.write_text(cache_key, encoding="utf-8")
            except Exception:  # noqa: BLE001
                self._clear_bg_pixmap()
                return ""
        # 加载 QPixmap 并注入中央区（paintEvent 绘制）
        try:
            from PySide6.QtGui import QPixmap

            pm = QPixmap(str(cache_path))
            if pm.isNull():
                self._clear_bg_pixmap()
            else:
                self._central_area.set_bg_pixmap(pm)
        except Exception:  # noqa: BLE001
            self._clear_bg_pixmap()
        return self._bg_overlay_qss()

    def _clear_bg_pixmap(self) -> None:
        """清除中央区背景图（无背景图或加载失败时）。"""
        if hasattr(self, "_central_area"):
            try:
                self._central_area.set_bg_pixmap(None)
            except Exception:  # noqa: BLE001
                pass

    def _bg_overlay_qss(self) -> str:
        """背景图模式下的半透明覆盖块（全窗口透出）。

        背景图设在 QMainWindow 上，但默认 QSS 里 QTabWidget::pane / QTabBar /
        通用 QFrame / QListWidget 等用不透明主题色盖住整块内容区 → 背景图看不见。
        这里把大面积容器全部改透明（含通用 QFrame），仅保留常用卡片的半透明
        底色 + 可读性保障，让背景图全窗口透出。
        """
        tokens = self.theme_manager.current_tokens()
        bg = tokens.get("bg", "#FFFFFF")
        bg2 = tokens.get("bg2", "#FFFFFF")
        bg3 = tokens.get("bg3", "#F0F0F0")
        hover_alpha = "rgba(0,0,0,0.05)"

        def _rgba(hex_color: str, alpha: float) -> str:
            """把 #RRGGBB 转 rgba(r,g,b,a)。失败回退白色半透明。"""
            try:
                hex_color = hex_color.lstrip("#")
                r = int(hex_color[0:2], 16)
                g = int(hex_color[2:4], 16)
                b = int(hex_color[4:6], 16)
                return f"rgba({r},{g},{b},{alpha})"
            except (ValueError, IndexError):
                return f"rgba(255,255,255,{alpha})"

        # 背景色透明度调到很低：卡片 ~0.4，次要容器 ~0.3，正文面板 ~0.25，
        # 让背景图全窗口透出（用户明确要求）
        card = _rgba(bg2, 0.42)       # 卡片底（低不透明度，背景图透出）
        card_soft = _rgba(bg2, 0.30)  # 次要容器（列表/下拉等）
        panel = _rgba(bg, 0.25)       # 阅读器/正文等大块面板
        hover = _rgba(bg3, 0.55)
        pane_tint = _rgba(bg, 0.12)   # pane 极浅底色（几乎透明，略增强对比）
        return f"""
/* ---------- 背景图模式：背景色透明度调到很低，背景图全窗口透出 ---------- */
/* 0) 中央区背景图已设到 QWidget#centralArea；保证它不被覆盖 */
QWidget#centralArea {{
    background: transparent;
}}
/* 1) Tab 面板透明（露出 centralArea 背景图）；仅保留极浅底色增强对比 */
QTabWidget::pane {{
    background-color: {pane_tint};
}}
QTabBar {{
    background-color: transparent;
}}
QTabBar::tab:hover:!selected {{
    background-color: {hover_alpha};
}}
/* 2) 通用 QFrame / 滚动区 / 列表：透明或极浅，背景图透出 */
QFrame {{
    background: transparent;
}}
QScrollArea, QScrollArea > QWidget > QWidget {{
    background: transparent;
    border: none;
}}
QListWidget {{
    background-color: transparent;
    border: none;
}}
QListWidget::item:hover {{
    background-color: {hover_alpha};
}}
QListWidget::item:selected {{
    background-color: {_rgba(tokens.get("accent", "#FF8FAB"), 0.85)};
}}
/* 3) 常用卡片：低不透明度（保留层次，背景图透出） */
QFrame#workCard, QFrame#statsCard, QFrame#detailDrawer,
QFrame#catBar, QFrame#sourceRow, QFrame#shelfCard, QFrame#brokenCard {{
    background-color: {card};
}}
QFrame#workCard:hover, QFrame#shelfCard:hover {{
    background-color: {hover};
}}
/* 4) 输入控件 / 次要容器：低不透明度 */
QLineEdit, QSpinBox, QComboBox {{
    background-color: {card_soft};
}}
QComboBox QAbstractItemView {{
    background-color: {card};
}}
/* 4) 阅读器正文 / 信息条：半透明面板，保证长文可读 */
QLabel#readerBody, QTextEdit {{
    background-color: {panel};
    border-radius: 12px;
    padding: 8px 12px;
}}
/* 5) 纯文本标签：彻底透明 */
QLabel#statsValue, QLabel#statsLabel, QLabel#brokenBadge {{
    background: transparent;
}}
"""

    def _on_settings_applied(self) -> None:
        """设置页点「应用」→ 重跑主题 QSS（含背景图）+ 字体缩放 + 网络默认值 + 封面缓存。"""
        # 重跑主题（含背景图合成 QSS + 字体缩放）
        self._apply_theme_qss(self.theme_manager.current_key())
        # 字体缩放 → 阅读器
        font_scale = float(self.settings.get("ui", "font_scale", 1.0))
        if hasattr(self, "reader") and self.reader is not None:
            self.reader.apply_font_scale(font_scale)
            self.reader.apply_reading_style(
                self.settings.get("ui", "reading_bg", "") or "",
                int(self.settings.get("ui", "reading_font_size", 0) or 0),
            )
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

    # ------------------------------------------------------------------ #
    def _schedule_startup_diag(self) -> None:
        """启动自动诊断：等窗口显示后，后台对所有启用源跑一次 selfcheck。

        对应 ui-source.md #4「启动自动诊断」。静默不阻塞启动；
        诊断结果经 SourceManager.update_health 持久化，源管理页健康灯自动刷新。
        """
        from PySide6.QtCore import QTimer, QThreadPool, QRunnable, QObject, Signal

        class _DiagSignals(QObject):
            finished = Signal(str, bool, str)  # (source_id, ok, err)

        class _DiagTask(QRunnable):
            def __init__(self, checker, source, url):
                super().__init__()
                self.signals = _DiagSignals()
                self._checker = checker
                self._source = source
                self._url = url

            def run(self) -> None:
                ok, err = False, ""
                try:
                    ok = self._checker.check(self._source, self._url)
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                try:
                    self.signals.finished.emit(self._source.source_id, ok, err)
                except RuntimeError:
                    pass

        def _diag_url(source) -> str:
            """自检目标 URL：优先 discovery 列表入口，否则 base_url / homepage。"""
            from urllib.parse import urljoin

            raw = source.raw
            endpoints = raw.get("endpoints") or {}
            discovery = endpoints.get("discovery") or {}
            rel = (
                discovery.get("works_list_url")
                or discovery.get("list_url")
                or discovery.get("list_categories_url")
            )
            base = source.base_url or source.homepage
            if not base:
                return ""
            if rel:
                return rel if rel.startswith("http") else urljoin(base, rel)
            return base

        def _run_one(source) -> None:
            url = _diag_url(source)
            if not url:
                self._on_startup_diag_done(source.source_id, False, "缺少可自检的 URL")
                return
            task = _DiagTask(self.checker, source, url)
            task.signals.finished.connect(self._on_startup_diag_done)
            QThreadPool.globalInstance().start(task)

        def _start() -> None:
            sources = [s for s in self.source_manager.all() if s.enabled]
            for s in sources:
                _run_one(s)

        # 延迟到窗口显示后再跑，不阻塞启动
        QTimer.singleShot(0, _start)

    def _on_startup_diag_done(self, source_id: str, ok: bool, err: str) -> None:
        """启动诊断完成：更新健康状态 + 刷新源管理页健康灯。"""
        from framework.source_manager import HEALTH_OK, HEALTH_WARN, HEALTH_BROKEN

        if ok:
            state = HEALTH_OK
        elif err:
            state = HEALTH_BROKEN
        else:
            state = HEALTH_WARN
        try:
            self.source_manager.update_health(source_id, state, err if err else "")
        except Exception:  # noqa: BLE001
            return
        # 刷新源管理页（若已构建）健康灯
        page = getattr(self, "source_page", None)
        if page is not None:
            for row in getattr(page, "_rows", []) or []:
                if row.source().source_id == source_id:
                    health = self.source_manager.get_health(source_id)
                    row.set_health(health)
                    row.set_error(err if not ok else "")
                    break


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
