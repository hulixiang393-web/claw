"""应用入口（App）。

- 主窗口 + 导航栏（首页 | 发现 | 搜索 | 阅读 | 下载 | 书架 | 源管理 | 设置）
- 主题加载与实时切换
- 全局快捷键（Ctrl+1~8 切 Tab 等）
- 未实现界面在导航栏标注「·开发中」
- 首页内部导航（⚙设置 / 去添加源 / 最近搜索）联动主窗口 Tab
"""

from __future__ import annotations

import sys
import threading
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
from framework.http import HttpClient, NetworkDefaults
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


def network_defaults_from_settings(settings) -> NetworkDefaults:
    """从 settings（app_config network.*）构造 NetworkDefaults。

    新增键：impersonate（curl_cffi TLS/JA3 伪装档位，null=关闭）、user_agents
    （UA 轮换列表，空/缺失=关闭）。默认关闭，不改变现有源的请求行为。
    """
    return NetworkDefaults(
        timeout=float(settings.get("network", "default_timeout", 10)),
        retries=int(settings.get("network", "default_retries", 3)),
        interval_ms=int(settings.get("network", "default_request_interval", 0)),
        proxy=settings.get("network", "proxy") or None,
        user_agent=settings.get("network", "default_user_agent", "") or None,
        impersonate=settings.get("network", "impersonate") or None,
        user_agents=settings.get("network", "user_agents") or None,
    )


def _app_base_dir() -> Path:
    """应用根目录：PyInstaller 打包后为 exe 所在目录（sources/data/docs 随 exe 旁），
    开发运行时为项目根（gui/ 的上一级）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _pick_favorite_folder(parent, store) -> str:
    """弹「收藏到分类」对话框，返回所选收藏夹名。

    阅读器 / 详情抽屉新增收藏共用此入口：
    - 选「全部」或取消 → 返回 ""（未归类，书架「全部」视图显示）
    - 选已有收藏夹 → 返回该夹名
    - 输入新名字 → 自动建夹后返回该名字
    """
    from PySide6.QtWidgets import (
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QLabel,
        QVBoxLayout,
    )

    dlg = QDialog(parent)
    dlg.setWindowTitle("收藏到分类")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    layout.addWidget(QLabel("收藏到哪个收藏夹？"))

    combo = QComboBox()
    combo.setEditable(True)
    combo.addItem("全部")
    combo.addItems(store.list_folders() if store is not None else [])
    combo.setCurrentIndex(0)
    layout.addWidget(combo)

    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Ok).setText("收藏")
    buttons.button(QDialogButtonBox.Cancel).setText("取消")
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    if dlg.exec() != QDialog.Accepted:
        return ""
    name = combo.currentText().strip()
    if not name or name == "全部":
        return ""  # 「全部」/ 未填写 → 未归类
    if store is not None and name not in store.list_folders():
        store.create_folder(name)  # 输入新名字 → 自动建夹
    return name


class _CentralArea(QWidget):
    """中央区容器：paintEvent 直接绘制背景图（最底层）。

    Qt 的 QSS `background-image` 对普通 QWidget 不生效（即使 WA_StyledBackground
    也只画 background-color），所以背景图必须用 paintEvent 画——这是可靠方案：
    先画背景图铺满，再让 QTabWidget 等子控件（透明/半透明）浮在其上。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bg_pixmap = None  # 由 App 层注入（合成后的背景图）
        self._bg_scaled = None  # 按窗口尺寸预缩放的绘制用缓存（避免每次重绘都缩放）

    def set_bg_pixmap(self, pixmap) -> None:
        """注入背景图（QPixmap 或 None 关闭）。"""
        self._bg_pixmap = pixmap
        self._bg_scaled = None  # 尺寸未变时重绘仍用旧缓存
        self.update()  # 触发重绘

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._bg_scaled = None  # 窗口尺寸变了，下次重绘重新缩放

    def _ensure_bg_scaled(self) -> None:
        """把背景图预缩放到当前窗口尺寸（KeepAspectRatioByExpanding + 居中裁剪）。

        只在尺寸变化时缩放一次（SmoothTransformation），paintEvent 直接
        drawPixmap 同尺寸（无运行时缩放），大幅降低 Tab 切换/窗口重绘开销。
        """
        if self._bg_pixmap is None or self._bg_pixmap.isNull():
            return
        if self._bg_scaled is not None and self._bg_scaled.size() == self.size():
            return
        size = self.size()
        if size.width() <= 0 or size.height() <= 0:
            return
        from PySide6.QtCore import Qt

        scaled = self._bg_pixmap.scaled(
            size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
        )
        # 居中裁剪到窗口尺寸（超出部分去掉），避免 drawPixmap 用负坐标
        sx = max(0, (scaled.width() - size.width()) // 2)
        sy = max(0, (scaled.height() - size.height()) // 2)
        self._bg_scaled = scaled.copy(
            sx, sy, min(size.width(), scaled.width()), min(size.height(), scaled.height())
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._bg_pixmap is not None and not self._bg_pixmap.isNull():
            self._ensure_bg_scaled()
            if self._bg_scaled is not None and not self._bg_scaled.isNull():
                painter = QPainter(self)
                painter.drawPixmap(0, 0, self._bg_scaled)
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
        base_dir = _app_base_dir()
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

        self.reading_progress = ReadingProgress(
            base_dir / "data" / "reading_progress.json",
            shelf_cb=self._favorite_has,  # 收藏的书续读永久保留，未收藏 24h 清理
        )
        self.reading_progress.prune(shelf_cb=self._favorite_has)  # 启动清理（收藏保留）

        # 爬取执行链（网络默认值从 settings 接线：impersonate/user_agents 默认关闭）
        self.http = HttpClient(defaults=network_defaults_from_settings(self.settings))
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
            cookie_manager=self.cookie_manager,
        )
        self.download_queue = DownloadQueue(
            content=self.content,
            http=self.http,
            settings=self.settings,
            source_manager=self.source_manager,
            event_bus=self.event_bus,
            history_path=base_dir / "data" / "downloads.json",
        )
        # 下载完成/新增 → 刷新书架（新下载的书出现在本地组）；收藏变化在 _on_favorite 刷新
        self.event_bus.subscribe(self._on_download_event)
        # Redis 持久化缓存注入：书架池（封面/详情/正文/漫画页）+ 搜索&发现池（搜索/列表）。
        # 数据目录随 data/ 走 settings（与首页索引同目录）。
        from framework.cache_service import get_shelf_cache, get_search_cache

        shelf_cache = get_shelf_cache(str(base_dir / "data" / "cache"))
        search_cache = get_search_cache(str(base_dir / "data" / "cache"))
        self.discovery.cache = search_cache
        self.content._cache = shelf_cache
        self.search.cache = search_cache
        self.http.cache = None  # 上层显式查/写，HttpClient 仅持句柄供复用（不透明拦截）
        # 封面缓存预算接线（cover_cache_size_mb）+ 书架封面持久化
        from gui.components.cover_loader import CoverLoader

        CoverLoader.instance().configure(
            self.settings.get("ui", "cover_cache_size_mb", 256),
            shelf_cache=shelf_cache,
        )
        # 正文图/封面下载收敛到框架 HttpClient：共享实例带 CF cookie / impersonate 等能力
        CoverLoader.instance().use_http(self.http)
        # 登记全部源：列表/搜索封面回填只传 source_id，需据此补源级 direct
        # （爱丽丝等直连源封面经系统代理会失败/变慢 → 封面空白）
        for _src in self.source_manager.all():
            CoverLoader.instance().register_source(_src)

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
        # 切到书架 Tab 时刷新：阅读/下载后回来，续读记忆（读到第X章/看到第X集）实时更新
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # 主题
        self.theme_manager.on_theme_changed(self._apply_theme_qss)
        self._apply_theme_qss(self.theme_manager.current_key())

        # 全局快捷键
        self._install_shortcuts()

        # 启动自动诊断：后台对所有启用源跑一次 selfcheck，健康灯一开始就准确
        self._schedule_startup_diag()

        # 首次启动源选择引导（可跳过；未做过源选择时窗口显示后弹出）
        self._schedule_source_selection()

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
            search=self.search,
        )
        # 阅读器「收藏」→ 写书架收藏库（与发现详情抽屉同一入口 _on_favorite）
        self.reader.favorite_requested.connect(self._on_favorite)
        # 阅读器「⬇ 下载」→ 拉详情后弹章节范围对话框（默认当前章→末章）
        self.reader.download_requested.connect(self._download_from_reader)
        # 收藏判断回调：LibraryStore.has(url)（书架库未构建时先构建）
        self.reader.set_favorite_checker(self._favorite_has)
        # 阅读器全屏：隐藏/恢复 Tab 栏（沉浸阅读，退出全屏还原）
        self.reader.fullscreen_changed.connect(self._on_reader_fullscreen)
        # 启动即应用阅读区独立背景/字号（ui-reader #12）
        self.reader.apply_reading_style(
            self.settings.get("ui", "reading_bg", "") or "",
            int(self.settings.get("ui", "reading_font_size", 0) or 0),
        )
        # 阅读器「背景」按钮循环 → 写回设置（记忆用户护眼背景色，下次重启沿用）
        self.reader.reading_bg_changed.connect(self._on_reading_bg_changed)
        # App 退出：先落盘当前阅读进度（续读记忆跨重启保留），再释放 VLC。
        from PySide6.QtWidgets import QApplication

        QApplication.instance().aboutToQuit.connect(self.reader.flush_progress)
        QApplication.instance().aboutToQuit.connect(self.reader.shutdown_video)
        from framework.vlc_player import shutdown_vlc

        QApplication.instance().aboutToQuit.connect(shutdown_vlc)
        # 启动即预热 VLC 实例（import vlc + 建 Instance 约 1-2s，是首播主要耗时）。
        # 放后台线程不阻塞启动；首播时实例已就绪 → 秒开。
        from framework.vlc_player import warmup_vlc

        threading.Thread(target=warmup_vlc, daemon=True, name="vlc-warmup").start()
        return self.reader

    def _on_reader_fullscreen(self, fs: bool) -> None:
        """阅读器全屏时隐藏 Tab 栏（沉浸），退出全屏恢复。"""
        self.tabs.tabBar().setVisible(not fs)

    def _on_reading_bg_changed(self, bg: str) -> None:
        """阅读器「背景」循环后写回设置（记忆护眼背景色，下次打开/重启沿用）。"""
        self.settings.set("ui", "reading_bg", bg)
        self.settings.save()

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

            base_dir = _app_base_dir()
            self.library_store = LibraryStore(base_dir / "data" / "library.json")
            return self.library_store
        except Exception:  # noqa: BLE001
            return None

    def _build_discover(self) -> DiscoverPage:
        from framework.cache_service import get_session_cache
        page = DiscoverPage(
            source_manager=self.source_manager,
            discovery=self.discovery,
            content=self.content,
            bulk_fetch=self.bulk_fetch,
            event_bus=self.event_bus,
            theme_manager=self.theme_manager,
            session_cache=get_session_cache(),
        )
        page.read_requested.connect(self._open_reader)
        page.download_requested.connect(self._open_download_dialog)
        page.favorite_requested.connect(self._on_favorite)
        self.discover_page = page
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
            # 新增收藏：弹「收藏到分类」选收藏夹（全部/取消 → 未归类）
            folder = _pick_favorite_folder(self, store)
            store.add(
                getattr(detail, "source_id", ""),
                url,
                getattr(detail, "title", "") or url,
                content_type=getattr(detail, "content_type", ""),
                cover=getattr(detail, "cover", ""),
                author=getattr(detail, "author", "") or "",
                tags=list(getattr(detail, "tags", None) or ()) if detail else [],
                folder=folder,
            )
        # 刷新书架（若已构建）
        page = getattr(self, "library_page", None)
        if page is not None:
            page.refresh()
        # 刷新阅读器收藏按钮状态（收藏/取消后 ★/☆ 同步）
        if getattr(self, "reader", None) is not None:
            self.reader.refresh_favorite_state()

    # ------------------------------------------------------------------ #
    def _on_download_event(self, event) -> None:
        """下载事件（后台线程回调）：完成/失败后刷新书架，让新下载的书出现在本地组。"""
        try:
            etype = getattr(event, "type", "")
            if etype in ("DOWNLOAD_COMPLETED", "DOWNLOAD_FAILED", "DOWNLOAD_STARTED"):
                page = getattr(self, "library_page", None)
                if page is not None and hasattr(page, "refresh"):
                    page.refresh()
        except Exception:  # noqa: BLE001 —— 书架刷新失败不影响下载
            pass

    def _on_tab_changed(self, idx: int) -> None:
        """Tab 切换：离开阅读 Tab 先落盘进度；切到书架时重建一次（续读实时更新）。"""
        # 离开阅读 Tab → 落盘当前作品进度（切走不丢续读位置）
        prev = getattr(self, "_last_tab_idx", None)
        reader_idx = self._tab_index.get("reader")
        if prev == reader_idx and idx != reader_idx and self.reader is not None:
            self.reader.flush_progress()
        self._last_tab_idx = idx
        page = self.tabs.widget(idx)
        if page is getattr(self, "library_page", None) and hasattr(page, "refresh"):
            page.refresh()

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
                content_type=getattr(r, "content_type", "") or "",
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
            def __init__(self, content, source, url, item):
                super().__init__()
                self.signals = _ShelfDlSignals()
                self._content = content
                self._source = source
                self._url = url
                self._item = item

            def run(self) -> None:
                detail = None
                try:
                    detail = self._content.fetch_detail(self._source, self._url)
                except Exception:  # noqa: BLE001
                    detail = None
                try:
                    self.signals.done.emit((self._item, self._source, self._url, detail))
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
            task = _FetchDetailTask(self.content, source, getattr(r, "url", ""), r)
            task.signals.done.connect(self._on_batch_dl_detail)
            self._dl_tasks.append(task)
            QThreadPool.globalInstance().start(task)

    def _on_batch_dl_detail(self, payload) -> None:
        """批量下载：一条详情就绪，累计；全部就绪后入队。"""
        item, source, url, detail = payload
        self._dl_results.append((item, source, url, detail))
        self._dl_pending -= 1
        if self._dl_pending > 0:
            return
        # 全部就绪：对每个有章节的结果入队（阅读器下载弹范围选框，批量下载取前 N 集）
        ok = 0
        for r, source, url, detail in self._dl_results:
            if detail is None or not getattr(detail, "chapters", None):
                continue
            # 阅读器下载：弹章节范围对话框（默认当前章→末章）
            if getattr(r, "range_dialog", False):
                picked = self._reader_download_selection(
                    detail, getattr(r, "current_idx", 1) or 1
                )
                if picked is None:
                    continue  # 用户取消
                selected, quality = picked
                self.download_queue.add_task(detail, selected=selected, quality=quality)
                ok += 1
                continue
            ep_count = getattr(r, "ep_count", 0) or 0
            if ep_count > 0 and len(detail.chapters) > ep_count:
                from types import SimpleNamespace

                detail = SimpleNamespace(
                    **{k: v for k, v in vars(detail).items() if k != "chapters"},
                    chapters=detail.chapters[:ep_count],
                )
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
            content=self.content,
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
        from pathlib import Path

        # 统一解析路径，与书架扫描（shelf_service output_dir .resolve()）生成的
        # 续读 key 完全一致，否则相对目录配置下续读匹配不上。
        path = str(Path(path).resolve())
        self.reader.open_epub(path)
        self.tabs.setCurrentIndex(self._tab_index["reader"])

    def _build_library(self):
        """书架：本地 epub + 收藏聚合。"""
        from gui.pages.library_page import LibraryPage

        base_dir = _app_base_dir()
        # 复用收藏库（阅读器可能已先构建），避免重复读文件
        self.library_store = self._ensure_library_store()
        output_dir = self.settings.get("download", "output_dir", "downloads")
        shelf_export_dir = self.settings.get("library", "shelf_export_dir", "library")
        self.library_page = LibraryPage(
            output_dir=output_dir,
            library_store=self.library_store,
            reading_progress=self.reading_progress,
            shelf_export_dir=shelf_export_dir,
            cover_backfiller=self._backfill_favorite_covers,
        )
        # 点本地 epub → 内置阅读器打开（续读）
        self.library_page.open_epub_requested.connect(self._open_epub)
        # 点收藏在线书 → 在线阅读器打开
        self.library_page.open_online_requested.connect(self._open_online_from_shelf)
        # 收藏在线书右键「下载到本地」→ 加入下载队列
        self.library_page.download_requested.connect(self._download_from_shelf)
        # 点本地视频书 → 弹集选择播本地 mp4（离线）
        self.library_page.play_local_video_requested.connect(self._play_local_video)
        return self.library_page

    def _backfill_favorite_covers(self, recs) -> None:
        """书架渲染后：对封面无效的收藏记录后台补详情封面（失败静默，不阻塞书架）。

        fetch_cover 复用 page: 详情缓存——详情页已在缓存/重启前抓过则零网络；
        仅真无缓存时才发一次详情 GET（串行后台执行，不并发压站）。补齐后写回
        收藏库，下次渲染不再重复补。

        「无效封面」判定：空 / 等于源 base_url（历史 bug 曾写回 base_url 垃圾值）
        / 命中源 cover 配置的占位标记——判定只是内存比对，无需网络；只对无效的
        发起 fetch_cover。
        """
        if not recs:
            return
        store = self._ensure_library_store()
        content = getattr(self, "content", None)
        source_manager = getattr(self, "source_manager", None)
        if store is None or content is None or source_manager is None:
            return
        from PySide6.QtCore import QThreadPool, QRunnable

        class _CoverBackfillTask(QRunnable):
            def __init__(self, content, source_manager, store, recs):
                super().__init__()
                self._content = content
                self._source_manager = source_manager
                self._store = store
                self._recs = recs

            def _cover_invalid(self, cover: str, source) -> bool:
                """封面可否直接当作有效（不触发网络重补）。"""
                if not cover:
                    return True
                if source is None:
                    return False
                base = (getattr(source, "base_url", "") or "").rstrip("/")
                if cover.rstrip("/") == base:
                    return True
                try:
                    detail_cfg = source.get_detail_config() or {}
                    cover_sel = (detail_cfg.get("fields") or {}).get("cover")
                    return bool(cover_sel) and self._content._is_placeholder_cover(
                        cover, cover_sel
                    )
                except Exception:  # noqa: BLE001
                    return False

            def run(self) -> None:
                for r in self._recs:
                    try:
                        source = self._source_manager.get(r.get("source_id", ""))
                        if source is None:
                            continue
                        if not self._cover_invalid(r.get("cover", ""), source):
                            continue  # 已有有效封面，不重复补
                        cover = self._content.fetch_cover(
                            source, r.get("url", "")
                        )
                        if not cover or self._cover_invalid(cover, source):
                            continue  # 仍然取不到有效封面 → 保持原样待下轮
                        self._store.set_cover(r.get("url", ""), cover)
                    except Exception:  # noqa: BLE001 —— 单条失败静默，继续补其它
                        continue

        # 持有任务引用防 GC（书架在 App 生命周期内常驻）
        self._cover_backfill_tasks = getattr(self, "_cover_backfill_tasks", None) or []
        self._cover_backfill_tasks.append(_CoverBackfillTask(
            content, source_manager, store, recs
        ))
        QThreadPool.globalInstance().start(self._cover_backfill_tasks[-1])

    def _play_local_video(self, rec: dict) -> None:
        """播本地视频：记住上次看到哪集（下次默认定位该集）；单集直接播，多集弹选择。"""
        paths = list(rec.get("episode_paths") or [])
        if not paths:
            return
        # 记忆 key：本地视频书 key=目录路径（与 shelf_service 一致，已归一为绝对路径）
        key = rec.get("key") or rec.get("path") or ""
        prev_title = ""
        if key and self.reading_progress is not None:
            recp = self.reading_progress.resume(key)
            prev_title = (recp or {}).get("chapter_title", "")
        if not prev_title:
            # 本地无进度 → 回退线上收藏进度（同书线上看到哪集，本地也定位到那集）
            url = rec.get("url") or ""
            if url and self.reading_progress is not None:
                recp = self.reading_progress.resume(url)
                prev_title = (recp or {}).get("chapter_title", "")

        target = paths[0]
        if len(paths) > 1:
            from pathlib import Path

            names = [Path(p).name for p in paths]

            # 文件名模糊匹配：线上集标题（如"第5集"）未必等于文件名（如"第5集.mp4"）
            cur = 0
            if prev_title:
                for i, n in enumerate(names):
                    if prev_title in n or n in prev_title:
                        cur = i
                        break
            # 自定义弹窗替代 QInputDialog：QInputDialog.getItem 会按最长文本
            # 自动撑得太宽（mp4 文件名通常很长），设最大宽度统一弹窗尺寸。
            from PySide6.QtWidgets import (
                QComboBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout,
            )

            dlg = QDialog(self)
            dlg.setWindowTitle("选择集数")
            dlg.setMaximumWidth(400)
            layout = QVBoxLayout(dlg)
            layout.setContentsMargins(16, 14, 16, 14)
            layout.setSpacing(10)
            layout.addWidget(QLabel("选择要播放的集："))
            combo = QComboBox()
            combo.setEditable(False)
            combo.addItems(names)
            combo.setCurrentIndex(cur)
            layout.addWidget(combo)
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.button(QDialogButtonBox.Ok).setText("播放")
            buttons.accepted.connect(dlg.accept)
            buttons.rejected.connect(dlg.reject)
            layout.addWidget(buttons)
            if dlg.exec() != QDialog.Accepted:
                return
            target = paths[combo.currentIndex()]
        # 记录"看到第 N 集"（外部播放器无法回传秒数，至少记住集数续播）
        if key and self.reading_progress is not None:
            try:
                from pathlib import Path as _P

                self.reading_progress.save(
                    "", key, "video", target, _P(target).name or ""
                )
            except Exception:  # noqa: BLE001 —— 记忆失败不影响播放
                pass
        from framework.external_player import open_with_player

        open_with_player(target)

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

    def _download_from_shelf(self, payload) -> None:
        """书架收藏在线书右键「下载到本地」→ 后台拉详情 → 加入下载队列。

        播放页下载可携带第 4 项 ep_count（>0 时只下载前 N 集）。
        复用 _on_batch_download 的单条任务机制（拉详情拿章节列表后 add_task）。
        """
        if not isinstance(payload, (tuple, list)) or len(payload) < 2:
            return
        source_id, url = payload[0], payload[1]
        if not url:
            return
        from types import SimpleNamespace

        item = SimpleNamespace(source_id=source_id, url=url,
                               ep_count=payload[3] if len(payload) > 3 else 0)
        self._on_batch_download([item])

    def _download_from_reader(self, payload) -> None:
        """阅读器「⬇ 下载」→ 拉详情 → 弹章节范围对话框（默认当前章→末章）→ 入队。

        payload=(source_id, url, content_type, current_idx)，current_idx 为 1 基。
        """
        if not isinstance(payload, (tuple, list)) or len(payload) < 2:
            return
        source_id, url = payload[0], payload[1]
        content_type = payload[2] if len(payload) > 2 else ""
        current_idx = int(payload[3]) if len(payload) > 3 and payload[3] else 1
        if not url:
            return
        from types import SimpleNamespace
        item = SimpleNamespace(
            source_id=source_id, url=url, content_type=content_type,
            range_dialog=True, current_idx=max(1, current_idx),
        )
        self._on_batch_download([item])

    def _reader_download_selection(self, detail, current_idx):
        """弹阅读器章节范围对话框；返回 (selected_list, quality) 或 None（取消）。"""
        from gui.components.download_range_dialog import (
            DownloadRangeDialog, build_selection,
        )

        chapters = list(getattr(detail, "chapters", None) or [])
        total = len(chapters)
        if total <= 1:
            return [True] * total, ""
        dialog = DownloadRangeDialog(
            detail.title, total, content_type=getattr(detail, "content_type", ""),
            parent=self, default_start=current_idx,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return None
        return build_selection(total, dialog.selection()), dialog.quality()

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
        # 源选择 → 打开同一个引导对话框（重新勾选启用源）
        self.settings_page.source_select_requested.connect(
            self._open_source_selection_dialog
        )
        return self.settings_page

    def _build_source(self):
        """源管理页：源列表 + 诊断 + 编辑入口。"""
        from gui.pages.source_page import SourcePage

        base_dir = _app_base_dir()
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

        base_dir = _app_base_dir()
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
                _app_base_dir() / "sources"
            )
        except Exception:
            pass
        # 源配置（含 direct）可能变化 → 刷新 CoverLoader 的源注册表
        try:
            from gui.components.cover_loader import CoverLoader

            for _src in self.source_manager.all():
                CoverLoader.instance().register_source(_src)
        except Exception:  # noqa: BLE001
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
        """背景图：按主题 bg 色 + 用户图片 + 透明度 → 合成后注入中央区。

        背景图用 paintEvent 绘制（QWidget 的 QSS background-image 不生效）。
        缓存命中的加载与缓存未命中时的整套合成（解码 + 平滑缩放 + 叠加 +
        PNG 落盘）都不阻塞 GUI 线程：前者本就毫秒级，后者交给后台任务。

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
        opacity = self._bg_opacity()
        tokens = self.theme_manager.current_tokens()
        bg_color = tokens.get("bg", "#FFF6F9")
        # 缓存 key
        cache_dir = _app_base_dir() / "data" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / "bg_composed.png"
        key_file = cache_dir / "bg_composed.key"
        cache_key = f"{bg_path}|{opacity}|{bg_color}"
        # 缓存命中：直接快速加载，不阻塞
        cache_hit = False
        try:
            if key_file.is_file() and key_file.read_text(encoding="utf-8").strip() == cache_key:
                if cache_path.is_file() and cache_path.stat().st_size > 0:
                    cache_hit = True
        except OSError:
            pass
        if cache_hit:
            self._load_bg_pixmap(cache_path)
        else:
            # 缓存未命中：大图解码 + 平滑缩放 + 叠加 + 写盘全部放后台线程
            self._bg_compose_async(bg_file, opacity, bg_color, cache_path, key_file, cache_key)
        return self._bg_overlay_qss()

    def _bg_opacity(self) -> float:
        """背景图不透明度（0.0~1.0）。

        注意不能用 `... or 0.6`：用户显式设为 0.0 时 0.0 是假值，
        会被 or 篡改成 0.6 导致「调透明度到 0 不生效」。
        """
        raw = self.settings.get("ui", "background_opacity", 0.6)
        try:
            return float(0.6 if raw is None else raw)
        except (TypeError, ValueError):
            return 0.6

    def _load_bg_pixmap(self, cache_path) -> None:
        """从缓存 PNG 加载 QPixmap 并注入中央区（GUI 线程）。"""
        try:
            from PySide6.QtGui import QPixmap

            pm = QPixmap(str(cache_path))
            if pm.isNull():
                self._clear_bg_pixmap()
            else:
                self._central_area.set_bg_pixmap(pm)
        except Exception:  # noqa: BLE001
            self._clear_bg_pixmap()

    def _bg_compose_async(self, bg_file, opacity, bg_color,
                          cache_path, key_file, cache_key) -> None:
        """后台合成背景图（QThreadPool worker + 信号回主线程注入）。

        序列号防重叠：连续触发（快速换图/调透明度）只采纳最新一次结果，
        过期任务静默退出且不写盘，避免旧参数覆盖新参数对应缓存。
        """
        from PySide6.QtCore import QThreadPool, QRunnable, QObject, Signal

        self._bg_seq = getattr(self, "_bg_seq", 0) + 1
        seq = self._bg_seq
        w = max(800, self.width() or 1024)
        h = max(600, self.height() or 768)

        class _BgSignals(QObject):
            done = Signal(int, object)  # (seq, cache_path or None)

        class _BgComposeTask(QRunnable):
            def __init__(self, signals, seq):
                super().__init__()
                self.signals = signals
                self._seq = seq
                self._bg_file = str(bg_file)
                self._opacity = opacity
                self._bg_color = bg_color
                self._cache_path = str(cache_path)
                self._key_file = str(key_file)
                self._cache_key = cache_key
                self._w = w
                self._h = h

            def run(self) -> None:
                try:
                    from PySide6.QtGui import QColor, QImage, QPainter as _QP

                    bg_q = QImage(self._w, self._h, QImage.Format_RGB32)
                    bg_q.fill(QColor(self._bg_color))
                    fg_q = QImage(self._bg_file)
                    if fg_q.isNull():
                        self.signals.done.emit(self._seq, None)
                        return
                    fg_q = fg_q.scaled(
                        self._w, self._h,
                        Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
                    )
                    painter = _QP(bg_q)
                    painter.setOpacity(self._opacity)
                    painter.drawImage(0, 0, fg_q)
                    painter.end()
                    # 原子落盘：先写临时文件再 rename，避免读到半写 PNG
                    tmp_png = self._cache_path + ".tmp"
                    bg_q.save(tmp_png, "PNG")
                    import os
                    os.replace(tmp_png, self._cache_path)
                    tmp_key = self._key_file + ".tmp"
                    with open(tmp_key, "w", encoding="utf-8") as fh:
                        fh.write(self._cache_key)
                    os.replace(tmp_key, self._key_file)
                    self.signals.done.emit(self._seq, self._cache_path)
                except Exception:  # noqa: BLE001
                    try:
                        self.signals.done.emit(self._seq, None)
                    except RuntimeError:
                        pass

        seq = self._bg_seq
        signals = _BgSignals()
        signals.done.connect(self._on_bg_composed)
        self._bg_compose_tasks = getattr(self, "_bg_compose_tasks", []) + [_BgComposeTask(signals, seq)]
        QThreadPool.globalInstance().start(self._bg_compose_tasks[-1])

    def _on_bg_composed(self, seq, path) -> None:
        """后台合成完成 → 仅采纳最新一次结果并注入。"""
        self._bg_compose_tasks = []
        if seq != getattr(self, "_bg_seq", 0):
            return  # 过期结果
        if path:
            from pathlib import Path as _P
            self._load_bg_pixmap(_P(path))
        else:
            self._clear_bg_pixmap()

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
QFrame#workCard, QFrame#statsCard,
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
        # 封面缓存预算（保留已注入的书架持久化缓存，避免覆盖为 None）
        from gui.components.cover_loader import CoverLoader

        from framework.cache_service import get_shelf_cache

        CoverLoader.instance().configure(
            self.settings.get("ui", "cover_cache_size_mb", 256),
            shelf_cache=get_shelf_cache(str(_app_base_dir() / "data" / "cache")),
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

    # ------------------------------------------------------------------ #
    # 源选择引导（首次启动 + 设置界面「源选择」共用）
    # ------------------------------------------------------------------ #
    def _schedule_source_selection(self) -> None:
        """首次启动源选择引导：未做过源选择时，等窗口显示后弹出（可跳过）。

        确认 / 跳过都会写入「已做过源选择」标记，下次启动不再弹。
        """
        if self.settings.get("sources_runtime", "sources_selected", False):
            return
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, lambda: self._open_source_selection_dialog(first_run=True))

    def _open_source_selection_dialog(self, first_run: bool = False) -> None:
        """打开源选择引导对话框，应用勾选结果并刷新相关页面。

        first_run=True：跳过或确定都视为「已做过源选择」，写入标记；
        确定后把勾选结果应用到源启用状态（持久化到源 JSON）。
        """
        from PySide6.QtWidgets import QDialog

        from gui.components.source_select_dialog import SourceSelectDialog

        dlg = SourceSelectDialog(self.source_manager, first_run=first_run, parent=self)
        accepted = dlg.exec() == QDialog.Accepted
        if accepted:
            self.source_manager.apply_enabled_selection(dlg.selected_ids())
        if first_run:
            self.settings.set("sources_runtime", "sources_selected", True)
            self.settings.save()
        if accepted:
            self._on_sources_reselected()

    def _on_sources_reselected(self) -> None:
        """源选择应用后：刷新源管理页 + 发现页 + 搜索页（禁用源不再列出）。"""
        page = getattr(self, "source_page", None)
        if page is not None:
            page.refresh()
        for key in ("discover", "search"):
            idx = self._tab_index.get(key)
            if idx is None:
                continue
            page = self.tabs.widget(idx)
            if page is not None and hasattr(page, "refresh"):
                page.refresh()


def main() -> int:
    from framework.logging_setup import setup_logging
    from framework.settings_manager import SettingsManager

    # 日志初始化（诊断区 verbose_logging / log_dir）
    base_dir = _app_base_dir()
    setup_logging(SettingsManager(base_dir / "app_config.json"))

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
