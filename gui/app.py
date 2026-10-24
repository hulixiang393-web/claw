"""应用入口（App）。

- 主窗口 + 导航栏（首页 | 发现 | 搜索 | 阅读 | 下载 | 书架 | 源管理 | 设置）
- 主题加载与实时切换
- 全局快捷键（Ctrl+1~8 切 Tab 等）
- 未实现界面在导航栏标注「·开发中」
- 首页内部导航（⚙设置 / 去添加源 / 最近搜索）联动主窗口 Tab
"""

from __future__ import annotations

import logging
import math
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QSignalBlocker,
    QThreadPool,
    QTimer,
    Qt,
    Signal,
)
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

log = logging.getLogger(__name__)

# 退出时对后台线程池 / VLC 预热线程的有界等待上限（超时容忍，绝不无限阻塞退出）。
_POOL_WAIT_MS = 2000
_VLC_WARMUP_JOIN_S = 2.0


class _AppRunnable(QRunnable):
    """gui/app.py 内后台 QRunnable 基类：收敛重复的构造与回收样板。

    - 统一 QRunnable 初始化（子类只需调用 super().__init__()）；
    - emit_safe：跨线程发信号时若接收者已销毁（RuntimeError）静默丢弃，
      与既有 `except RuntimeError: pass` 语义一致，行为不变。
    """

    def __init__(self) -> None:
        super().__init__()

    @staticmethod
    def emit_safe(signal, *args) -> None:
        """线程安全地 emit：接收者已销毁（RuntimeError）时丢弃，不抛出。"""
        try:
            signal.emit(*args)
        except RuntimeError:
            pass


class _BootstrapSourceManager:
    """运行时就绪前的占位 SourceManager：只提供只读空结果，避免页面构造期崩溃。

    真实服务在 READY 后通过各页 rebind_runtime 注入，本对象只承担占位；
    任何写操作（set_event_bus 等）都是无副作用的空实现。
    """

    def all(self, include_adult=False):
        return []

    def list_broken(self):
        return []

    def count_by_type(self):
        return {}

    def count_enabled(self):
        return 0

    def count_broken(self):
        return 0

    def get(self, source_id):
        return None

    def discoverable_sources(self):
        return []

    def enabled_sources(self):
        return []

    def set_event_bus(self, bus) -> None:
        return None

    def set_adult_visible(self, visible) -> None:
        return None

    def apply_enabled_selection(self, selected_ids) -> None:
        return None

    def get_health(self, source_id):
        from framework.source_manager import SourceHealth

        return SourceHealth()


class _SettingsSnapshot:
    _missing = object()

    def __init__(self, data):
        self._data = data

    def get(self, section, key=_missing, default=None):
        value = self._data.get(section, {})
        if key is self._missing:
            return dict(value) if isinstance(value, dict) else {}
        return value.get(key, default) if isinstance(value, dict) else default

    def get_section(self, section):
        return self.get(section)


@dataclass
class _RuntimeServices:
    source_manager: object
    cookie_manager: object
    shelf_cache: object
    search_cache: object
    shelf_cache_repository: object
    http: object
    parser: object
    checker: object
    discovery: object
    decrypter: object
    content: object
    bulk_fetch: object
    provider_memory: object
    search: object
    download_queue: object


class _StartupSignals(QObject):
    ready = Signal(object, int)
    failed = Signal(str, int)


def _release_services(services) -> None:
    """释放一套未发布的运行时服务（失败/迟到/取消）：关闭连接、回收资源。

    幂等且静默：任何一步失败都不抛出，避免释放路径本身再次影响主流程。
    """
    if services is None:
        return
    for name, method in (("shelf_cache_repository", "close"), ("http", "close")):
        resource = getattr(services, name, None)
        closer = getattr(resource, method, None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                log.debug("释放运行时服务 %s 失败", name, exc_info=True)


class _EventBusQtBridge(QObject):
    """把任意线程 emit 的事件排队投递回 GUI 线程执行。

    `forward` 可能在下载 worker 等后台线程被调用；通过 Qt 信号
    （QueuedConnection，接收者生命周期在主线程）保证 handler 始终在主线程执行。
    close() 后迟发事件直接丢弃，且单个 handler 异常被隔离并记录。
    """

    event = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._closed = False
        self.event.connect(self._deliver, Qt.QueuedConnection)

    def forward(self, handler, event) -> None:
        if self._closed:
            return
        self.event.emit((handler, event))

    def _deliver(self, payload) -> None:
        if self._closed:
            return
        handler, event = payload
        try:
            handler(event)
        except Exception:  # noqa: BLE001 —— 隔离单订阅者异常，不影响其它
            log.exception(
                "事件订阅者处理失败：%s",
                getattr(handler, "__qualname__", type(handler).__name__),
            )

    def close(self) -> None:
        self._closed = True


class _GuiEventBus:
    """页面用的 EventBus 门面：订阅回调统一经桥接在主线程执行。

    页面拿到的就是这个对象，只暴露 subscribe/unsubscribe/emit；所有订阅
    包装后经 _EventBusQtBridge 回主线程，后台线程 emit 不再直接触碰 QWidget。
    """

    def __init__(self, bus, bridge):
        self._bus = bus
        self._bridge = bridge
        self._wrappers = {}

    def subscribe(self, handler) -> None:
        if handler in self._wrappers:
            return
        def wrapper(event, _handler=handler):
            self._bridge.forward(_handler, event)

        self._wrappers[handler] = wrapper
        self._bus.subscribe(wrapper)

    def unsubscribe(self, handler) -> None:
        wrapper = self._wrappers.pop(handler, None)
        if wrapper is not None:
            self._bus.unsubscribe(wrapper)

    def emit(self, event) -> None:
        self._bus.emit(event)

    def close(self) -> None:
        for wrapper in list(self._wrappers.values()):
            self._bus.unsubscribe(wrapper)
        self._wrappers.clear()


class _StartupTask(threading.Thread):
    def __init__(
        self, window, base_dir, settings_snapshot, library_store,
        source_manager, generation, signals, cancel,
    ):
        super().__init__(daemon=True, name="runtime-startup")
        self._window = window
        self._base_dir = base_dir
        self._library_store = library_store
        self._settings = settings_snapshot
        self._source_manager = source_manager
        self._generation = generation
        self._cancel = cancel
        self._signals = signals

    def run(self) -> None:
        try:
            from framework.cache_service import get_shelf_cache, get_search_cache
            from framework.cookie_manager import CookieManager
            from framework.shelf_cache_repository import ShelfCacheRepository
            from framework.shelf_cache_migration import migrate_legacy_data
            from framework.shelf_service import ShelfService
            from framework.ikanpp_provider_memory import ProviderMemory

            base_dir = self._base_dir
            settings = self._settings
            # 源配置在窗口构造阶段已加载；后台只补齐网络/缓存服务，避免重复扫描。
            source_manager = self._source_manager
            cookie_manager = CookieManager(base_dir / "data")
            source_manager.set_cookie_provider(cookie_manager.to_cookie_header)
            shelf_cache = get_shelf_cache(str(base_dir / "data" / "cache"))
            search_cache = get_search_cache(str(base_dir / "data" / "cache"))
            from framework.settings_manager import shelf_cache_settings
            cfg = shelf_cache_settings(settings)
            repository = ShelfCacheRepository(
                base_dir / "data" / "shelf.sqlite3",
                base_dir / "data" / "cache" / "payload",
                max_book_bytes=cfg["max_book_mb"] * 1024 * 1024,
                max_total_bytes=cfg["max_total_mb"] * 1024 * 1024,
                repair_index=False,
            )
            legacy_shelf = ShelfService(
                settings.get("download", "output_dir", "downloads"),
                library_store=self._library_store,
                data_dir=base_dir / "data",
            )
            http = HttpClient(defaults=network_defaults_from_settings(settings))
            parser = Parser()
            checker = StructureChecker(
                http,
                parser,
                default_strategy=settings.get("sources_runtime", "selfcheck_strategy", "soft"),
            )
            discovery = Discovery(http, parser, checker, health_reporter=source_manager)
            decrypter = Decrypter(http)
            content = Content(
                http,
                parser,
                checker,
                decrypter,
                health_reporter=source_manager,
                repository=repository,
            )
            bulk_fetch = BulkFetch(discovery, event_bus=None, index_dir=base_dir / "data")
            provider_memory = ProviderMemory(base_dir / "data" / "ikanpp_provider_memory.json")
            search = Search(
                http,
                parser,
                discovery,
                concurrent=int(settings.get("network", "concurrent_search_sources", 4)),
                cookie_manager=cookie_manager,
                provider_memory=provider_memory,
            )
            download_queue = DownloadQueue(
                content=content,
                http=http,
                settings=settings,
                source_manager=source_manager,
                event_bus=None,
                history_path=base_dir / "data" / "downloads.json",
            )
            content._cache = shelf_cache
            discovery.cache = search_cache
            search.cache = search_cache
            http.cache = None
            services = _RuntimeServices(
                source_manager,
                cookie_manager,
                shelf_cache,
                search_cache,
                repository,
                http,
                parser,
                checker,
                discovery,
                decrypter,
                content,
                bulk_fetch,
                provider_memory,
                search,
                download_queue,
            )
            if self._cancel.is_set():
                _release_services(services)
                return
            # 先发布网络服务，让发现页与启动维护并行。repair_index 会递归扫描
            # 书架载荷目录，不能再挡住源列表和首个分类请求。
            self._signals.ready.emit(services, self._generation)
            if self._cancel.is_set():
                return
            try:
                _run_startup_migration(
                    repository,
                    base_dir / "data" / "reading_progress.json",
                    legacy_shelf,
                    shelf_cache,
                    base_dir,
                )
                repository.repair_index()
                _run_deferred_history_cleanup(self._window)
            except Exception:  # noqa: BLE001 -- 维护失败不影响已发布的网络服务
                log.exception("启动后台维护失败，保留网络服务继续运行")
        except Exception as exc:
            try:
                repository.close()
            except (NameError, AttributeError, OSError):
                log.debug("启动失败后关闭 shelf repository 失败", exc_info=True)
            log.exception("后台初始化基础服务失败")
            if self._cancel.is_set():
                return
            try:
                self._signals.failed.emit(f"{type(exc).__name__}: {exc}", self._generation)
            except RuntimeError:
                pass


def _run_deferred_history_cleanup(window) -> None:
    """启动后台阶段执行搜索历史/阅读进度的延迟清理与落盘。

    仅由 _StartupTask.run（后台线程）调用：保证 bootstrap 首帧构造只读，
    清理/脱敏/写盘不发生在主/UI 线程；清理基于内存当前数据，不覆盖
    后台阶段期间用户的新写入。仓库引用仍由发布成功后主线程挂接。
    """
    history = getattr(window, "search_history", None)
    progress = getattr(window, "reading_progress", None)
    try:
        if history is not None:
            history.run_deferred_cleanup()
        if progress is not None:
            progress.run_deferred_cleanup()
    except Exception:  # noqa: BLE001 —— 清理失败只记录日志，不阻塞服务发布
        log.exception("后台清理历史/进度失败")

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


def _sync_source_visibility(settings, source_manager, event_bus=None) -> None:
    """把内容可见性设置同步到 SourceManager。"""
    if event_bus is not None:
        source_manager.set_event_bus(event_bus)
    source_manager.set_adult_visible(
        bool(settings.get("content", "show_adult_sources", True))
    )


def _make_reading_progress(path: Path, shelf_cb, repository=None):
    from framework.reading_progress import ReadingProgress
    return ReadingProgress(path, shelf_cb=shelf_cb, repository=repository, defer_cleanup=True)


def _legacy_chapters_root(base_dir: Path) -> Path:
    return base_dir / "data" / "chapters"


def _run_startup_migration(repository, progress_path, shelf_service, legacy_cache, base_dir):
    from framework.shelf_cache_migration import migrate_legacy_data
    return migrate_legacy_data(
        repository,
        progress_path,
        shelf_service,
        legacy_cache,
        legacy_chapters_root=_legacy_chapters_root(base_dir),
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

    from framework.ui_perf import op as _op

    with _op("dialog:pick_favorite_folder"):
        accepted = dlg.exec() == QDialog.Accepted
    if not accepted:
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

        base_dir = _app_base_dir()
        self._base_dir = base_dir
        self.settings = SettingsManager(base_dir / "app_config.json")
        self.event_bus = EventBus()
        self._runtime_bridge = _EventBusQtBridge(self)
        self._gui_bus = _GuiEventBus(self.event_bus, self._runtime_bridge)
        self._gui_bus.subscribe(self._on_download_event)
        self.theme_manager = ThemeManager(self.settings)
        from framework.library_store import LibraryStore

        self.library_store = LibraryStore(base_dir / "data" / "library.json")
        self.search_history = SearchHistory(
            base_dir / "data" / "search_history.json", privacy=self.library_store,
            defer_cleanup=True,
        )
        # 源 JSON/健康状态是本地轻量数据，先加载它们让首页首帧直接显示真实统计。
        # 网络客户端、解析器和内容服务仍由后台启动任务创建。
        self.source_manager = SourceManager(
            sources_dir=base_dir / "sources",
            health_file=base_dir / "data" / "health.json",
            runtime_settings=_SettingsSnapshot(self.settings.as_dict()),
        )
        self.source_manager.set_adult_visible(
            bool(self.settings.get("content", "show_adult_sources", True))
        )
        self.cookie_manager = None
        self.shelf_cache = None
        self.search_cache = None
        self.session_cache = None
        self.shelf_cache_repository = None
        self.http = None
        self.parser = None
        self.checker = None
        self.discovery = None
        self.decrypter = None
        self.content = None
        self.bulk_fetch = None
        self.ikanpp_provider_memory = None
        self.search = None
        self.download_queue = None
        self.reading_progress = _make_reading_progress(
            base_dir / "data" / "reading_progress.json", self._favorite_has
        )
        self._runtime_state = "UNINITIALIZED"
        self._runtime_generation = 0
        self._runtime_task = None
        self._runtime_cancel = threading.Event()
        self._runtime_signals = _StartupSignals()
        self._runtime_signals.ready.connect(self._on_runtime_ready)
        self._runtime_signals.failed.connect(self._on_runtime_failed)
        self._pending_navigation = None
        self._closing = False

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
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # 主题
        self.theme_manager.on_theme_changed(self._apply_theme_qss)
        self._initial_theme_pending = True
        QTimer.singleShot(0, self._apply_initial_theme)

        # UI 卡顿探针（默认关闭：环境变量 / diagnostics.ui_perf_logging / data/ui_perf.on）
        try:
            from framework.ui_perf import install as _install_ui_perf

            _install_ui_perf(self.settings, self._base_dir)
        except Exception:  # noqa: BLE001 —— 探针失败不影响启动
            log.debug("ui_perf 初始化失败", exc_info=True)

        # 全局快捷键
        self._install_shortcuts()


    def _apply_initial_theme(self) -> None:
        if not self._initial_theme_pending:
            return
        self._initial_theme_pending = False
        self._apply_theme_qss(self.theme_manager.current_key())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._runtime_state == "UNINITIALIZED":
            QTimer.singleShot(0, self._start_runtime_initialization)

    def _start_runtime_initialization(self) -> None:
        if self._closing or self._runtime_state == "LOADING":
            return
        self._runtime_state = "LOADING"
        self._runtime_cancel.set()
        self._runtime_cancel = threading.Event()
        self._runtime_generation += 1
        generation = self._runtime_generation
        snapshot = _SettingsSnapshot(self.settings.as_dict())
        self._runtime_signals = _StartupSignals()
        self._runtime_signals.ready.connect(self._on_runtime_ready)
        self._runtime_signals.failed.connect(self._on_runtime_failed)
        self._runtime_task = _StartupTask(
            self,
            self._base_dir,
            snapshot,
            self.library_store,
            self.source_manager,
            generation,
            self._runtime_signals,
            self._runtime_cancel,
        )
        self._runtime_task.start()

    def _on_runtime_ready(self, services, generation: int) -> None:
        # 迟到/已关闭的 ready：直接回收这套私有服务，绝不覆盖新 generation 状态
        if self._closing or generation != self._runtime_generation:
            _release_services(services)
            return
        try:
            self._prepare_runtime_services(services)
        except Exception as exc:
            # 发布失败：_prepare_runtime_services 已整体回滚 self 引用，
            # 此处回收私有连接并进入可重试 FAILED。
            try:
                self.reading_progress.attach_repository(None)
            except Exception:  # noqa: BLE001
                log.exception("发布失败后解除阅读进度仓库绑定失败")
            _release_services(services)
            self._on_runtime_failed(f"{type(exc).__name__}: {exc}", generation)
            return
        # 所有绑定完成后才置 READY
        self._runtime_state = "READY"
        for step in (
            self._replace_runtime_placeholders,
            self._schedule_startup_page_priority,
            self._schedule_source_selection,
            self._replace_pending_navigation,
        ):
            try:
                step()
            except Exception:  # noqa: BLE001 —— 后置 UI 动作失败不影响已发布服务
                log.exception("READY 后置动作失败：%s", getattr(step, "__name__", step))

    def _schedule_startup_page_priority(self) -> None:
        """Warm the startup pages in order without blocking the home page."""
        generation = self._runtime_generation
        if getattr(self, "_startup_page_generation", -1) == generation:
            return
        self._startup_page_generation = generation

        def alive() -> bool:
            return not self._closing and self._runtime_state == "READY" and generation == self._runtime_generation

        def build_source() -> None:
            if not alive():
                return
            self._ensure_tab_page(self._tab_index["source"])
            QTimer.singleShot(0, build_library)

        def build_library() -> None:
            if not alive():
                return
            self._ensure_tab_page(self._tab_index["library"])
            QTimer.singleShot(0, build_settings)

        def build_settings() -> None:
            if alive():
                self._ensure_tab_page(self._tab_index["settings"])

        # DiscoverPage construction starts BookStack's first category request;
        # subsequent pages are queued on later UI turns to keep the home frame responsive.
        self._ensure_tab_page(self._tab_index["discover"])
        QTimer.singleShot(0, build_source)

    def _prepare_runtime_services(self, services) -> None:
        """把可失败的绑定/配置做完，最后一次性发布服务引用。

        发布语义为「先备齐全部服务对象 → 单一原子提交点 → 失败整体回滚」：
        1) 备齐阶段先收集本次要发布的全部字段值，不改动 self 上的服务引用；
        2) 提交阶段先快照将被覆盖字段的旧值，任一步失败即恢复全部旧引用与
           进度仓库绑定，避免异常路径残留半套服务。成功后由调用方置 READY。
        """
        services.download_queue._bus = self.event_bus
        services.bulk_fetch._bus = self.event_bus
        from gui.components.cover_loader import CoverLoader

        CoverLoader.instance().configure(
            self.settings.get("ui", "cover_cache_size_mb", 256),
            shelf_cache=services.shelf_cache,
        )
        CoverLoader.instance().use_http(services.http)
        for source in services.source_manager.all():
            CoverLoader.instance().register_source(source)

        # 备齐：先取齐所有字段值（provider_memory 对外名为 ikanpp_provider_memory）
        published = {}
        for name in _RuntimeServices.__dataclass_fields__:
            target = "ikanpp_provider_memory" if name == "provider_memory" else name
            published[target] = getattr(services, name)

        # 单一原子提交点：快照旧值 → 提交 → 任一步失败整体回滚
        previous = {target: getattr(self, target, None) for target in published}
        try:
            for target, value in published.items():
                setattr(self, target, value)
            # 清理/脱敏/写盘已在后台线程完成；这里仅补挂仓库引用（幂等）。
            self.reading_progress.attach_repository(services.shelf_cache_repository)
            _sync_source_visibility(self.settings, services.source_manager, self.event_bus)
            self._rebind_pages(services)
        except BaseException:
            for target, value in previous.items():
                setattr(self, target, value)
            try:
                self.reading_progress.attach_repository(None)
            except Exception:  # noqa: BLE001
                log.exception("发布失败回滚：解除阅读进度仓库绑定失败")
            raise

    def _rebind_pages(self, services) -> None:
        """把已构建页面的占位/旧服务替换为真实运行时服务（未构建的保持惰性）。"""
        page = getattr(self, "home_page", None)
        if page is not None:
            page.set_runtime_services(services.source_manager, services.bulk_fetch)
            page.set_runtime_status("")
        binder = getattr(getattr(self, "discover_page", None), "rebind_runtime", None)
        if callable(binder):
            binder(
                services.source_manager, services.discovery,
                services.content, services.bulk_fetch,
            )
        binder = getattr(getattr(self, "search_page", None), "rebind_runtime", None)
        if callable(binder):
            binder(services.source_manager, services.search, services.content)
        binder = getattr(getattr(self, "source_page", None), "rebind_runtime", None)
        if callable(binder):
            binder(services.source_manager, services.checker, services.cookie_manager)
        binder = getattr(getattr(self, "library_page", None), "rebind_runtime", None)
        if callable(binder):
            binder(
                services.source_manager, services.content,
                services.shelf_cache_repository,
            )

    def _on_runtime_failed(self, error: str, generation: int) -> None:
        if self._closing or generation != self._runtime_generation:
            return
        self._runtime_state = "FAILED"
        page = getattr(self, "home_page", None)
        if page is not None:
            page.set_runtime_status("基础服务初始化失败，可重试")

    def _replace_pending_navigation(self) -> None:
        pending = self._pending_navigation
        self._pending_navigation = None
        if pending is None:
            return
        channel, action = pending
        idx = self._tab_index.get(channel)
        if idx is not None:
            self._ensure_tab_page(idx)
        if callable(action):
            try:
                action()
            except Exception:  # noqa: BLE001
                log.exception("执行待处理导航失败：%s", channel)
        elif idx is not None:
            self.tabs.setCurrentIndex(idx)

    def _replace_runtime_placeholders(self) -> None:
        idx = self.tabs.currentIndex()
        if idx >= 0 and TABS[idx][1] in self._lazy_tab_builders:
            self._ensure_tab_page(idx)

    def closeEvent(self, event) -> None:
        # 退出编排：停止新任务 → 使旧回调失效 → 有界等待 worker/线程池/VLC 预热
        # 收尾 → flush → 关闭 repository 连接。全程有界，不强杀事务线程。
        self._closing = True
        self._runtime_cancel.set()
        self._runtime_generation += 1
        self._runtime_state = "CLOSING"
        try:
            self._gui_bus.close()
        except Exception:  # noqa: BLE001
            log.debug("退出：关闭 GUI 事件总线失败", exc_info=True)
        try:
            self._runtime_bridge.close()
        except Exception:  # noqa: BLE001
            log.debug("退出：关闭运行时事件桥失败", exc_info=True)
        task = self._runtime_task
        is_alive = getattr(task, "is_alive", None)
        join = getattr(task, "join", None)
        if task is not None and callable(is_alive) and is_alive() and callable(join):
            join(5.0)  # 有界等待，不阻塞退出过久
        # 有界等待线程池与 VLC 预热线程收尾（超时容忍，绝不无限阻塞退出）
        self._wait_for_background_tasks()
        try:
            reader = getattr(self, "reader", None)
            if reader is not None:
                reader.flush_progress()
        except Exception:  # noqa: BLE001
            log.debug("退出：落盘阅读进度失败", exc_info=True)
        self._clear_task_references()
        self._close_bound_services()
        super().closeEvent(event)

    def _wait_for_background_tasks(self) -> None:
        """有界等待全局线程池与 VLC 预热线程收尾；超时容忍，不无限阻塞退出。"""
        try:
            done = QThreadPool.globalInstance().waitForDone(_POOL_WAIT_MS)
        except Exception:  # noqa: BLE001
            done = False
            log.debug("退出：等待线程池失败", exc_info=True)
        if not done:
            log.debug("退出：线程池仍有任务未在 %dms 内完成，容忍超时", _POOL_WAIT_MS)
        thread = getattr(self, "_vlc_warmup_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(_VLC_WARMUP_JOIN_S)
            if thread.is_alive():
                log.debug(
                    "退出：VLC 预热线程未在 %.1fs 内结束，容忍超时", _VLC_WARMUP_JOIN_S
                )

    def _clear_task_references(self) -> None:
        """清空 App 持有的后台任务引用（退出释放，避免集合常驻内存）。"""
        for attr in ("_cover_backfill_tasks", "_bg_compose_tasks", "_dl_tasks"):
            tasks = getattr(self, attr, None)
            if tasks:
                tasks.clear()

    def _close_bound_services(self) -> None:
        """关闭已发布的 repository / http 连接（幂等）。"""
        repo = getattr(self, "shelf_cache_repository", None)
        if repo is not None:
            closer = getattr(repo, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    log.debug("退出：关闭 shelf_cache_repository 失败", exc_info=True)
            self.shelf_cache_repository = None
        http = getattr(self, "http", None)
        if http is not None:
            closer = getattr(http, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    log.debug("退出：关闭 http 连接失败", exc_info=True)

    # ------------------------------------------------------------------ #
    def _build_pages(self) -> None:
        """构建导航栏各 Tab。未实现界面在标签后标注「·开发中」。"""
        self.reader = None
        for attr in (
            "discover_page", "search_page", "download_page", "library_page",
            "source_page", "settings_page",
        ):
            setattr(self, attr, None)
        self._lazy_tab_builders = {
            "discover": self._build_discover,
            "search": self._build_search,
            "reader": self._build_reader,
            "download": self._build_download,
            "library": self._build_library,
            "source": self._build_source,
            "settings": self._build_settings,
        }
        for label, key in TABS:
            if key == "home":
                page = self._build_home()
            elif key in self._lazy_tab_builders:
                page = self._build_placeholder(label)
            else:
                page = self._build_placeholder(label)
                label = f"{label}·开发中"
            self.tabs.addTab(page, label)

    def _ensure_tab_page(self, idx: int) -> QWidget:
        key = TABS[idx][1]
        builder = self._lazy_tab_builders.get(key)
        if builder is None:
            return self.tabs.widget(idx)
        # 所有依赖运行时服务的页面都等 READY 后再构造，避免用户在后台初始化
        # 期间点入页面时拿到 None 服务；设置页是纯本地页面，可立即打开。
        if key != "settings" and self._runtime_state != "READY":
            return self.tabs.widget(idx)
        selected_idx = self.tabs.currentIndex()

        page = builder()

        self._lazy_tab_builders.pop(key, None)
        old = self.tabs.widget(idx)
        label = self.tabs.tabText(idx)
        blocker = QSignalBlocker(self.tabs)
        self.tabs.removeTab(idx)
        if old is not None:
            old.deleteLater()
        self.tabs.insertTab(idx, page, label)
        self.tabs.setCurrentIndex(selected_idx)
        del blocker
        return page

    def _build_reader(self) -> ReaderPage:
        from gui.pages.reader_page import ReaderPage

        self.reader = ReaderPage(
            source_manager=self.source_manager,
            content=self.content,
            reading_progress=self.reading_progress,
            font_scale=float(self.settings.get("ui", "font_scale", 1.0)),
            search=self.search,
            settings=self.settings,
        )
        # 阅读器「收藏」→ 写书架收藏库（与发现详情抽屉同一入口 _on_favorite）
        self.reader.favorite_requested.connect(self._on_favorite)
        # 阅读器「⬇ 下载」→ 拉详情后弹章节范围对话框（默认当前章→末章）
        self.reader.download_requested.connect(self._download_from_reader)
        # 收藏判断回调：LibraryStore.has(url)（书架库未构建时先构建）
        self.reader.set_favorite_checker(self._favorite_has)
        store = self._ensure_library_store()
        if store is not None:
            self.reader.set_reading_status_checker(store.reading_status)
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

        self._vlc_warmup_thread = threading.Thread(
            target=warmup_vlc, daemon=True, name="vlc-warmup"
        )
        self._vlc_warmup_thread.start()
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
            event_bus=self._gui_bus,
            theme_manager=self.theme_manager,
            # 仅当前进程复用，首次启动一定为空，不读取历史会话。
            session_cache=get_session_cache(),
            settings=self.settings,
            shelf_folders_provider=self._available_shelf_folders,
        )
        page.read_requested.connect(self._open_reader)
        page.download_requested.connect(self._open_download_dialog)
        page.favorite_requested.connect(self._on_favorite)
        page.add_to_shelf_requested.connect(self._on_batch_add_shelf)
        page.batch_download_requested.connect(self._on_batch_download)
        self.discover_page = page
        return page

    def _on_favorite(self, detail) -> None:
        from framework.ui_perf import op

        with op("on_favorite"):
            self._on_favorite_impl(detail)

    def _on_favorite_impl(self, detail) -> None:
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
        from framework.ui_perf import op

        with op("on_tab_changed"):
            self._on_tab_changed_impl(idx)

    def _on_tab_changed_impl(self, idx: int) -> None:
        """Tab 切换：离开阅读 Tab 先落盘进度；切到书架时重建一次（续读实时更新）。"""
        # 离开阅读 Tab → 落盘当前作品进度（切走不丢续读位置）
        prev = getattr(self, "_last_tab_idx", None)
        reader_idx = self._tab_index.get("reader")
        if prev == reader_idx and idx != reader_idx and self.reader is not None:
            self.reader.flush_progress()
        self._last_tab_idx = idx
        page = self._ensure_tab_page(idx)
        if page is getattr(self, "library_page", None) and hasattr(page, "activate"):
            page.activate()

    # ------------------------------------------------------------------ #
    def _on_batch_add_shelf(self, items, folder="") -> None:
        """搜索页勾选批量 → 加入书架（写收藏库）。"""
        store = self._ensure_library_store()
        if store is None or not items:
            return
        folder = folder if folder and folder != "全部" else ""
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
                folder=folder,
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
        from PySide6.QtCore import QObject, Signal

        class _ShelfDlSignals(QObject):
            done = Signal(object)  # (list[(result, detail_or_None)])

        class _FetchDetailTask(_AppRunnable):
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
                self.emit_safe(
                    self.signals.done, (self._item, self._source, self._url, detail)
                )

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

    def _available_shelf_folders(self) -> list[str]:
        store = self._ensure_library_store()
        if store is None:
            return []
        return store.list_folders()

    def _build_search(self) -> SearchPage:
        page = SearchPage(
            source_manager=self.source_manager,
            search=self.search,
            content=self.content,
            event_bus=self._gui_bus,
            shelf_folder_provider=self._available_shelf_folders,
        )
        page.open_requested.connect(self._open_from_search)
        page.search_clicked.connect(self._record_search)
        page.add_to_shelf_requested.connect(self._on_batch_add_shelf)
        page.batch_download_requested.connect(self._on_batch_download)

        self.search_page = page
        return page


    def _record_search(self, keyword: str, context_url: str = "", folder: str = "") -> None:
        self.search_history.push(keyword, context_url=context_url, folder=folder)

    def _ensure_reader(self):
        """返回可用的阅读器；未 READY 时返回 None（延迟构造到 READY）。"""
        if self.reader is not None:
            return self.reader
        if self._runtime_state != "READY":
            return None
        self._ensure_tab_page(self._tab_index["reader"])
        return self.reader

    def _ensure_download_page(self):
        if getattr(self, "download_page", None) is not None:
            return self.download_page
        if self._runtime_state != "READY":
            return None
        self._ensure_tab_page(self._tab_index["download"])
        return getattr(self, "download_page", None)

    def _queue_navigation(self, channel: str, action) -> None:
        """记录最新导航意图（原始参数快照在闭包里），READY 后精确执行一次。"""
        if self._closing:
            return
        self._pending_navigation = (channel, action)
        idx = self._tab_index.get(channel)
        if idx is not None:
            self.tabs.setCurrentIndex(idx)

    def _show_reader_online(self, source_id, url, content_type, chapter_url="") -> bool:
        reader = self._ensure_reader()
        if reader is None:
            return False
        reader.open(source_id, url, content_type, chapter_url)
        self.tabs.setCurrentIndex(self._tab_index["reader"])
        return True

    def _show_reader_epub(self, path) -> bool:
        reader = self._ensure_reader()
        if reader is None:
            return False
        from pathlib import Path

        reader.open_epub(str(Path(path).resolve()))
        self.tabs.setCurrentIndex(self._tab_index["reader"])
        return True

    def _open_from_search(self, source_id: str, url: str, content_type: str) -> None:
        """搜索页点结果卡片 → 打开 reader 播放/阅读。"""
        if not self._show_reader_online(source_id, url, content_type):
            self._queue_navigation(
                "reader",
                lambda: self._show_reader_online(source_id, url, content_type),
            )

    def _build_download(self) -> DownloadPage:
        from gui.pages.download_page import DownloadPage

        self.download_page = DownloadPage(
            queue=self.download_queue,
            event_bus=self._gui_bus,
            settings=self.settings,
        )
        # 下载完成「打开阅读」→ 内置 epub 阅读器
        self.download_page.open_epub_requested.connect(self._open_epub)
        return self.download_page

    def _open_epub(self, path: str) -> None:
        """下载页「打开阅读」→ 用内置 epub 阅读器打开。

        路径在 _show_reader_epub 内统一 resolve()，与书架扫描生成的续读 key
        一致；未 READY 时排队原始路径快照，READY 后打开。
        """
        if not path:
            return
        if not self._show_reader_epub(path):
            self._queue_navigation("reader", lambda: self._show_reader_epub(path))

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
            shelf_cache_repository=self.shelf_cache_repository,
            source_manager=getattr(self, "source_manager", None),
            content=getattr(self, "content", None),
            settings=self.settings,
        )
        # 点本地 epub → 内置阅读器打开（续读）
        self.library_page.open_epub_requested.connect(self._open_epub)
        # 点收藏在线书 → 在线阅读器打开
        self.library_page.open_online_requested.connect(self._open_online_from_shelf)
        # 收藏在线书右键「下载到本地」→ 加入下载队列
        self.library_page.download_requested.connect(self._download_from_shelf)
        # 点本地视频书 → 弹集选择播本地 mp4（离线）
        self.library_page.play_local_video_requested.connect(self._play_local_video)
        self.library_page.set_local_close_handler(self._close_local_before_delete)
        self.library_page.cards_ready.connect(self._on_library_cards_ready)
        return self.library_page

    def _close_local_before_delete(self, path: str) -> bool:
        reader = getattr(self, "reader", None)
        if reader is None:
            return False
        return reader.close_local_path(path)

    def _on_library_cards_ready(self) -> None:
        """Only construct search after the shelf has painted its first cards."""
        if self._runtime_state != "READY":
            return
        idx = self._tab_index.get("search")
        if idx is None or idx not in range(self.tabs.count()):
            return
        QTimer.singleShot(0, lambda: self._ensure_tab_page(idx))

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

        class _CoverBackfillSignals(QObject):
            finished = Signal(object)  # 完成的任务对象（供主线程注销引用）
            updated = Signal(str, str)  # (url, cover) 单条补写成功，供书架即刻刷新

        class _CoverBackfillTask(_AppRunnable):
            def __init__(self, content, source_manager, store, recs):
                super().__init__()
                self.signals = _CoverBackfillSignals()
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
                try:
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
                            if self._store.set_cover(r.get("url", ""), cover):
                                self.emit_safe(
                                    self.signals.updated, r.get("url", ""), cover
                                )
                        except Exception:  # noqa: BLE001 —— 单条失败静默，继续补其它
                            continue
                finally:
                    # 完成（含异常）→ 通知主线程注销引用，避免集合随触发次数无限增长
                    self.emit_safe(self.signals.finished, self)

        # 持有任务引用防 GC；完成后经信号在主线程移除，避免集合无界增长。
        task = _CoverBackfillTask(content, source_manager, store, recs)
        task.signals.finished.connect(self._on_cover_backfill_finished)
        task.signals.updated.connect(self._on_favorite_cover_backfilled)
        self._cover_backfill_tasks = getattr(self, "_cover_backfill_tasks", None) or []
        self._cover_backfill_tasks.append(task)
        QThreadPool.globalInstance().start(task)

    def _on_favorite_cover_backfilled(self, url: str, cover: str) -> None:
        """补写封面成功 → 让书架卡片立即换上封面，无需重进书架。"""
        page = getattr(self, "library_page", None)
        updater = getattr(page, "update_cover", None)
        if callable(updater):
            updater(url, cover)

    def _on_cover_backfill_finished(self, task) -> None:
        """封面回填任务完成 → 注销其引用（主线程槽；防止集合无界增长）。"""
        tasks = getattr(self, "_cover_backfill_tasks", None)
        if not tasks:
            return
        try:
            tasks.remove(task)
        except ValueError:
            pass

    def _local_progress_allowed(self, library_url: str) -> bool:
        store = getattr(self, "library_store", None)
        if store is None:
            return True
        return store.reading_status(library_url) not in {"completed", "unknown"}

    def _play_local_video(self, rec: dict) -> None:
        """播本地视频：记住上次看到哪集（下次默认定位该集）；单集直接播，多集弹选择。"""
        paths = list(rec.get("episode_paths") or [])
        if not paths:
            return
        # 记忆 key：本地视频书 key=目录路径（与 shelf_service 一致，已归一为绝对路径）
        key = rec.get("key") or rec.get("path") or ""
        prev_title = ""
        can_resume = MainWindow._local_progress_allowed(self, rec.get("url") or "")
        if key and self.reading_progress is not None and can_resume:
            recp = self.reading_progress.resume(key)
            prev_title = (recp or {}).get("chapter_title", "")
        if not prev_title:
            # 本地无进度 → 回退线上收藏进度（同书线上看到哪集，本地也定位到那集）
            url = rec.get("url") or ""
            if url and self.reading_progress is not None and can_resume:
                recp = self.reading_progress.resume(url)
                prev_title = (recp or {}).get("chapter_title", "")

        target = paths[0]
        if len(paths) > 1:
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
        from framework.external_player import open_with_player, flush_player_progress

        flush_player_progress()
        key = key or str(Path(target).resolve())
        can_resume = MainWindow._local_progress_allowed(self, rec.get("url") or "")
        resume = self.reading_progress.resume(key) if self.reading_progress is not None and can_resume else None
        location = (resume.get("location") or {}) if resume and resume.get("chapter_url") == target else {}
        metadata = {
            "key": key, "chapter_url": target, "chapter_title": Path(target).name,
            "title": rec.get("title") or Path(key).name,
            "library_url": rec.get("url") or "", "folder": rec.get("folder") or "",
        }

        def changed(idx, position):
            self._on_local_progress({**metadata, "location": position})

        def handed_off():
            if self.reading_progress is not None and MainWindow._local_progress_allowed(self, metadata["library_url"]):
                self.reading_progress.save(
                    "", key, "video", target, metadata["chapter_title"],
                    title=metadata["title"], library_url=metadata["library_url"],
                    folder=metadata["folder"], detail_url=target,
                )

        open_with_player(target, on_progress=changed, resume_positions={0: location}, on_handoff=handed_off)

    def _on_local_progress(self, payload):
        from framework.external_player import player_session

        if not MainWindow._local_progress_allowed(self, payload.get("library_url") or ""):
            return
        location = payload.get("location") or {}
        session = player_session()
        if self.reading_progress is None or session is None or location.get("session_id") != id(session):
            return
        seconds, duration = location.get("seconds"), location.get("duration")
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (seconds, duration)):
            return
        if not 0 < seconds <= duration:
            return
        self.reading_progress.save(
            "", payload["key"], "video", payload["chapter_url"], payload["chapter_title"],
            position=seconds / duration,
            location={"seconds": seconds, "duration": duration, "chapter_url": payload["chapter_url"]},
            title=payload["title"], library_url=payload["library_url"], folder=payload["folder"],
            detail_url=payload["chapter_url"],
        )

    def _open_online_from_shelf(self, payload) -> None:
        """书架点收藏在线书 → 打开在线阅读器。payload=(source_id, url, content_type)。

        content_type 为空时从源配置兜底推断（兼容早期收藏未存类型的记录）。
        """
        if not isinstance(payload, (tuple, list)) or len(payload) < 3:
            return
        source_id, url, content_type = payload[0], payload[1], payload[2]
        if not content_type:
            try:
                content_type = self.source_manager.get(source_id).content_type
            except Exception:  # noqa: BLE001
                content_type = ""
        if not self._show_reader_online(source_id, url, content_type):
            self._queue_navigation(
                "reader", lambda: self._show_reader_online(source_id, url, content_type)
            )

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
            event_bus=self._gui_bus,
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
        except Exception:  # noqa: BLE001 —— 重载失败保留旧源，仅记录
            log.warning("保存源后重载源目录失败", exc_info=True)
        # 源配置（含 direct）可能变化 → 刷新 CoverLoader 的源注册表
        try:
            from gui.components.cover_loader import CoverLoader

            for _src in self.source_manager.all():
                CoverLoader.instance().register_source(_src)
        except Exception:  # noqa: BLE001 —— 注册失败不影响已保存的源
            log.debug("保存源后刷新封面源注册表失败", exc_info=True)
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
        if self._runtime_state != "READY" or self.download_queue is None:
            self._queue_navigation(
                "download", lambda: self._open_download_dialog(detail)
            )
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
        """从发现详情抽屉「开始阅读」→ 打开阅读器。

        参数快照为原始字段值（source_id/url/content_type/首章 url），避免闭包
        持有可变 detail；未 READY 时排队，READY 后精确打开一次。
        """
        if detail is None:
            return
        source_id = getattr(detail, "source_id", "")
        url = getattr(detail, "url", "")
        content_type = getattr(detail, "content_type", "")
        chapters = list(getattr(detail, "chapters", None) or [])
        first_chapter = chapters[0].url if chapters else ""
        if not self._show_reader_online(source_id, url, content_type, first_chapter):
            self._queue_navigation(
                "reader",
                lambda: self._show_reader_online(
                    source_id, url, content_type, first_chapter
                ),
            )

    def _build_home(self) -> HomePage:
        page = HomePage(
            source_manager=self.source_manager,
            event_bus=self._gui_bus,
            theme_manager=self.theme_manager,
            settings=self.settings,
            history=self.search_history,
            bulk_fetch=self.bulk_fetch,
            reading_progress=self.reading_progress,
            privacy=self._ensure_library_store(),
        )
        self.home_page = page
        page.playback_requested.connect(self._resume_playback)
        page.retry_runtime_requested.connect(self._start_runtime_initialization)
        # 首页内部导航联动主窗口 Tab
        page.add_source_requested.connect(
            lambda: self.tabs.setCurrentIndex(self._tab_index["source"])
        )
        page.settings_requested.connect(
            lambda: self.tabs.setCurrentIndex(self._tab_index["settings"])
        )
        page.search_requested.connect(self._jump_to_search)
        return page

    def _resume_playback(self, record: dict) -> None:
        from framework.reading_progress import history_active

        progress = getattr(self, "reading_progress", None)
        if progress is not None:
            record = progress.all().get(record.get("book_url"))
        if not record or not history_active(record):
            return
        store = self._ensure_library_store()
        if store is None or store.playback_history_hidden(record):
            return
        if record.get("source_id"):
            self._open_online_from_shelf((record["source_id"], record.get("library_url") or record["book_url"], "video"))
        elif Path(record.get("chapter_url", "")).is_file():
            self._play_local_video({
                "key": record["book_url"], "episode_paths": [record["chapter_url"]],
                "url": record.get("library_url", ""), "folder": record.get("folder", ""),
                "title": record.get("title", ""),
            })

    def _jump_to_search(self, keyword: str) -> None:
        """跳转搜索 Tab + 预填关键词并自动搜索（ui-home #3 最近搜索一键重搜）。"""
        if self.search_history.hidden(keyword):
            return
        if self._runtime_state != "READY" or self.search is None:
            self._queue_navigation("search", lambda: self._jump_to_search(keyword))
            return
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
        self._initial_theme_pending = False
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
        # 通知各页：单页回调失败不影响其余页面，记录原因便于定位
        for i in range(self.tabs.count()):
            page = self.tabs.widget(i)
            if hasattr(page, "on_theme_changed"):
                try:
                    page.on_theme_changed(theme)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "主题回调失败：tab=%s page=%s",
                        self.tabs.tabText(i),
                        type(page).__name__,
                        exc_info=True,
                    )

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
            log.debug("读取背景图缓存 key 失败，按未命中处理", exc_info=True)
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
        from PySide6.QtCore import QObject, Signal

        self._bg_seq = getattr(self, "_bg_seq", 0) + 1
        seq = self._bg_seq
        w = max(800, self.width() or 1024)
        h = max(600, self.height() or 768)

        class _BgSignals(QObject):
            done = Signal(int, object)  # (seq, cache_path or None)

        class _BgComposeTask(_AppRunnable):
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
                        self.emit_safe(self.signals.done, self._seq, None)
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
                    self.emit_safe(self.signals.done, self._seq, self._cache_path)
                except Exception:  # noqa: BLE001 —— 合成失败不崩溃，通知回退
                    log.debug("背景图后台合成失败，回退为无背景", exc_info=True)
                    self.emit_safe(self.signals.done, self._seq, None)

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

    def _schedule_settings_refresh(self, changed=None) -> None:
        """合并设置应用后的 GUI 刷新，并且只刷新发生变化的设置对应的部分。

        changed：设置页传来的「变化键集合」（"分区.键名"）。None 表示未知，
        按全量刷新处理（向后兼容直接调用）。多次调用会合并变化键。
        """
        pending = getattr(self, "_pending_settings_changes", None)
        if changed is None or pending is None:
            self._pending_settings_changes = None if changed is None else set(changed)
        else:
            self._pending_settings_changes = set(pending) | set(changed)
        if getattr(self, "_settings_refresh_scheduled", False):
            return
        self._settings_refresh_scheduled = True

        from PySide6.QtCore import QTimer

        def _refresh():
            self._settings_refresh_scheduled = False
            changed_keys = getattr(self, "_pending_settings_changes", None)
            self._pending_settings_changes = None

            def _changed(*keys: str) -> bool:
                # changed_keys=None → 未知/全量；空集合 → 没有任何项变化
                if changed_keys is None:
                    return True
                return any(key in changed_keys for key in keys)

            # 1) 成人源可见性
            if _changed("content.show_adult_sources"):
                _sync_source_visibility(
                    self.settings, self.source_manager, self.event_bus
                )

            # 2) 封面缓存预算
            if _changed("ui.cover_cache_size_mb"):
                from gui.components.cover_loader import CoverLoader
                from framework.cache_service import get_shelf_cache

                CoverLoader.instance().configure(
                    self.settings.get("ui", "cover_cache_size_mb", 256),
                    shelf_cache=get_shelf_cache(
                        str(_app_base_dir() / "data" / "cache")
                    ),
                )

            # 3) 主题 / 背景图 / 透明度
            if _changed("ui.theme", "ui.background_image", "ui.background_opacity"):
                self._apply_theme_qss(self.theme_manager.current_key())

            # 4) 阅读器字号 / 阅读区背景（只刷新对应项）
            reader = getattr(self, "reader", None)
            if reader is not None:
                if _changed("ui.font_scale"):
                    reader.apply_font_scale(
                        float(self.settings.get("ui", "font_scale", 1.0))
                    )
                if _changed("ui.reading_bg", "ui.reading_font_size"):
                    reader.apply_reading_style(
                        self.settings.get("ui", "reading_bg", "") or "",
                        int(self.settings.get("ui", "reading_font_size", 0) or 0),
                    )

        QTimer.singleShot(0, _refresh)

    def _on_settings_applied(self, changed=None) -> None:
        """设置页点「应用」→ 只刷新本次实际修改的设置项。"""
        self._schedule_settings_refresh(changed)

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

    def _log_uncaught(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.error("未捕获异常（UI 线程）", exc_info=(exc_type, exc, tb))

    def _log_thread_uncaught(args) -> None:
        thread_name = getattr(getattr(args, "thread", None), "name", "?")
        log.error(
            "未捕获异常（线程 %s）", thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread_uncaught

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
