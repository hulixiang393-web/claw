"""设置界面（settings_page.py）。

全量覆盖 app_config.json 配置（对应 ui-settings.md）：
- 网络：UA/超时/重试/请求间隔/代理/并发搜索/图片上限
- UI：主题/字体缩放/封面缓存/默认页大小
- 下载：输出目录/命名模板/合并文本/跳过已存在/并发下载
- 书架：导出目录/WebDAV(预留)
- 诊断：日志级别/日志目录/告警间隔/禁用阈值/自检策略
- 广告规则：额外规则目录
底部：恢复默认（确认弹窗）/ 应用（持久化）。
多数字段实时生效，需重启字段标注「重启后生效」。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .base_page import BasePage


class _Section(QWidget):
    """设置分区：表单布局（label → 控件）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._form = QFormLayout(self)
        self._form.setContentsMargins(20, 16, 20, 16)
        self._form.setSpacing(10)
        self._form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def _row(self, label: str, widget: QWidget, hint: str = "") -> None:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(widget)
        if hint:
            h = QLabel(hint)
            h.setStyleSheet("color: palette(mid); font-size: 11px;")
            row.addWidget(h)
        row.addStretch(1)
        self._form.addRow(label, row)

    def _line(self, label: str, key: str, hint: str = "") -> QLineEdit:
        w = QLineEdit()
        self._row(label, w, hint)
        w.setProperty("key", key)
        return w

    def _line_browse(
        self,
        label: str,
        key: str,
        hint: str = "",
        file_mode: bool = False,
        filter_: str = "",
    ) -> QLineEdit:
        """带「浏览」按钮的行：文件/目录选择，选完回填。"""
        w = QLineEdit()
        btn = QPushButton("浏览")
        btn.setFixedWidth(56)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(w, stretch=1)
        row.addWidget(btn)
        if hint:
            h = QLabel(hint)
            h.setStyleSheet("color: palette(mid); font-size: 11px;")
            row.addWidget(h)
        row.addStretch(1)
        self._form.addRow(label, row)
        w.setProperty("key", key)

        def _pick() -> None:
            if file_mode:
                path, _ = QFileDialog.getOpenFileName(w, "选择文件", w.text(), filter_ or "所有文件 (*)")
            else:
                path = QFileDialog.getExistingDirectory(w, "选择目录", w.text())
            if path:
                w.setText(path)

        btn.clicked.connect(_pick)
        return w

    def _spin(self, label: str, key: str, lo: int, hi: int, suffix: str = "", hint: str = "") -> QSpinBox:
        w = QSpinBox()
        w.setRange(lo, hi)
        w.setSuffix(suffix)
        self._row(label, w, hint)
        w.setProperty("key", key)
        return w

    def _double(self, label: str, key: str, lo: float, hi: float, hint: str = "") -> QDoubleSpinBox:
        w = QDoubleSpinBox()
        w.setRange(lo, hi)
        w.setDecimals(1)
        w.setSingleStep(0.1)
        self._row(label, w, hint)
        w.setProperty("key", key)
        return w

    def _check(self, label: str, key: str, hint: str = "") -> QCheckBox:
        w = QCheckBox()
        self._row(label, w, hint)
        w.setProperty("key", key)
        return w

    def _combo(self, label: str, key: str, items: list, hint: str = "") -> QComboBox:
        w = QComboBox()
        w.addItems(items)
        self._row(label, w, hint)
        w.setProperty("key", key)
        return w


class SettingsPage(BasePage):
    """设置页：分区 Tab + 应用/恢复默认。"""

    theme_changed = Signal(str)  # 主题切换 → App 层刷新全局 QSS
    settings_applied = Signal()  # 点「应用」→ App 层重跑主题 QSS / 字体缩放 / 背景图
    source_select_requested = Signal()  # 点「源选择」→ App 层打开源选择引导对话框

    def __init__(self, settings, theme_manager, parent=None):
        super().__init__(parent)
        self._sm = settings
        self._tm = theme_manager

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tabs = QTabWidget()
        self._build_network()
        self._build_ui()
        self._build_download()
        self._build_library()
        self._build_diag()
        self._build_adblock()
        self._build_llm()
        layout.addWidget(self.tabs, stretch=1)

        # 底部按钮
        btns = QHBoxLayout()
        btns.setContentsMargins(16, 10, 16, 10)
        self.reset_btn = QPushButton("恢复默认")
        self.reset_btn.setStyleSheet("background-color: palette(midlight);")
        self.reset_btn.clicked.connect(self._on_reset)
        btns.addWidget(self.reset_btn)
        btns.addStretch(1)
        self.apply_btn = QPushButton("应用")
        self.apply_btn.clicked.connect(self._on_apply)
        btns.addWidget(self.apply_btn)
        layout.addLayout(btns)

        self._load_values()
        # 主题实时切换
        self._ui_theme.currentIndexChanged.connect(self._on_theme_selected)

    # ------------------------------------------------------------------ #
    # 分区构建
    # ------------------------------------------------------------------ #
    def _build_network(self) -> None:
        sec = _Section()
        self._net_ua = sec._line("User-Agent", "default_user_agent")
        self._net_timeout = sec._spin("超时(秒)", "default_timeout", 1, 120)
        self._net_retries = sec._spin("重试次数", "default_retries", 0, 10)
        self._net_interval = sec._spin("请求间隔(ms)", "default_request_interval", 0, 10000, " ms")
        self._net_proxy = sec._line("代理", "proxy", "重启后生效")
        self._net_concurrent = sec._spin("并发搜索数", "concurrent_search_sources", 1, 16)
        self._net_img_max = sec._spin("图片大小上限", "max_bytes_per_image", 1, 100, " MB")
        self.tabs.addTab(sec, "网络")

    def _build_ui(self) -> None:
        sec = _Section()
        self._ui_theme = sec._combo("主题", "theme", ["sakura", "mint", "midnight"])
        self._ui_font = sec._double("字体缩放", "font_scale", 0.5, 2.0)
        self._ui_cover_cache = sec._spin("封面缓存", "cover_cache_size_mb", 16, 1024, " MB")
        self._ui_page_size = sec._spin("默认页大小", "default_page_size", 5, 100)
        self._ui_bg_image = sec._line_browse(
            "背景图片", "background_image", "留空 = 纯色主题", file_mode=True,
            filter_="图片 (*.png *.jpg *.jpeg *.bmp *.webp)",
        )
        self._ui_bg_opacity = sec._double("背景透明度", "background_opacity", 0.0, 1.0, "仅背景图时生效")
        self._ui_reading_bg = sec._line(
            "阅读背景色", "reading_bg",
            "留空 = 跟随主题（如 #FDF6EC 或 #FFFFFF）",
        )
        self._ui_reading_font = sec._spin(
            "阅读字号", "reading_font_size", 0, 36, " px", "0 = 跟随全局"
        )
        # 缓存清除行
        row = QHBoxLayout()
        row.setSpacing(8)
        self._cache_clear_btn = QPushButton("清除缓存")
        self._cache_clear_btn.clicked.connect(self._on_cache_clear)
        row.addWidget(self._cache_clear_btn)
        hint = QLabel("清除封面内存缓存与临时合成图")
        hint.setStyleSheet("color: palette(mid); font-size: 11px;")
        row.addWidget(hint)
        row.addStretch(1)
        sec._form.addRow("缓存", row)

        # Redis 持久化缓存管理：书架 / 搜索&发现 两个独立池（分开清除）
        def _usage(store):
            try:
                return (store.bytes_used() / (1024 ** 3)) if store else 0
            except Exception:  # noqa: BLE001
                return 0

        from framework.cache_service import get_shelf_cache, get_search_cache

        usage_row = QHBoxLayout()
        usage_row.setSpacing(18)
        self._ui_shelf_usage = QLabel(f"{_usage(get_shelf_cache()):.2f} GB（书架）")
        self._ui_search_usage = QLabel(f"{_usage(get_search_cache()):.2f} GB（搜索&发现）")
        usage_row.addWidget(self._ui_shelf_usage)
        usage_row.addWidget(self._ui_search_usage)
        usage_row.addStretch(1)
        sec._form.addRow("缓存用量", usage_row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._cache_clear_shelf_btn = QPushButton("清除书架缓存")
        self._cache_clear_shelf_btn.clicked.connect(
            lambda: self._on_cache_pool_clear("shelf")
        )
        self._cache_clear_search_btn = QPushButton("清除搜索&发现缓存")
        self._cache_clear_search_btn.clicked.connect(
            lambda: self._on_cache_pool_clear("search")
        )
        btn_row.addWidget(self._cache_clear_shelf_btn)
        btn_row.addWidget(self._cache_clear_search_btn)
        hint2 = QLabel("书架：封面/详情/章节；搜索&发现：搜索结果/首页列表")
        hint2.setStyleSheet("color: palette(mid); font-size: 11px;")
        btn_row.addWidget(hint2)
        btn_row.addStretch(1)
        sec._form.addRow("缓存清除", btn_row)

        # 源选择入口：重开首次启动的引导对话框，重新勾选启用源
        src_row = QHBoxLayout()
        src_row.setSpacing(8)
        self._source_select_btn = QPushButton("源选择 / 重新下载源")
        self._source_select_btn.clicked.connect(self._on_source_select)
        src_row.addWidget(self._source_select_btn)
        src_hint = QLabel("重新勾选要启用的源，未勾选的源从发现/搜索页隐藏")
        src_hint.setStyleSheet("color: palette(mid); font-size: 11px;")
        src_row.addWidget(src_hint)
        src_row.addStretch(1)
        sec._form.addRow("源选择", src_row)
        self.tabs.addTab(sec, "UI")

    def _on_cache_clear(self) -> None:
        """清除封面内存缓存 + 临时合成图目录（保留 Redis 持久化 .gz 文件）。"""
        from PySide6.QtGui import QPixmapCache
        from PySide6.QtWidgets import QMessageBox

        QPixmapCache.clear()
        try:
            from gui.components.cover_loader import CoverLoader

            CoverLoader.instance().clear_cache()
        except Exception:  # noqa: BLE001
            pass
        # 清 data/cache 临时合成图（保留 redis_shelf.gz/redis_search.gz 持久化缓存）
        from pathlib import Path

        cache_dir = Path(__file__).resolve().parent.parent.parent / "data" / "cache"
        removed = 0
        if cache_dir.is_dir():
            for f in cache_dir.iterdir():
                if f.is_file() and f.suffix.lower() != ".gz":
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
        QMessageBox.information(self, "清除缓存", f"已清除缓存（删除 {removed} 个临时文件；Redis 持久化缓存请用下方按钮单独清除）")

    def _on_cache_pool_clear(self, pool: str) -> None:
        """清除指定缓存池（shelf 或 search）并刷新用量显示。

        书架（shelf）：封面/详情/章节正文/漫画页图。
        搜索&发现（search）：搜索结果 / 发现首页列表（同一 10G 池）。
        """
        from framework.cache_service import get_shelf_cache, get_search_cache
        from PySide6.QtWidgets import QMessageBox

        store = get_shelf_cache() if pool == "shelf" else get_search_cache()
        label = "书架" if pool == "shelf" else "搜索&发现"
        if store is not None:
            before = store.bytes_used()
            store.clear()
            try:
                store.flush_checked()  # 立即落盘（持久化清除状态，重启不复活）
            except Exception:  # noqa: BLE001
                pass
            if pool == "shelf":
                self._ui_shelf_usage.setText(f"0.00 GB（书架）")
            else:
                self._ui_search_usage.setText(f"0.00 GB（搜索&发现）")
            QMessageBox.information(
                self, "清除缓存",
                f"已清除{label}缓存（释放约 {before / (1024 ** 3):.2f} GB）",
            )
        else:
            QMessageBox.information(self, "清除缓存", f"{label}缓存未初始化")

    def _on_source_select(self) -> None:
        """点「源选择 / 重新下载源」→ 由 App 层打开同一个引导对话框。"""
        self.source_select_requested.emit()

    def _build_download(self) -> None:
        sec = _Section()
        self._dl_dir = sec._line_browse("输出目录", "output_dir")
        self._dl_tpl = sec._line("命名模板", "naming_template")
        self._dl_merge = sec._check("合并章节为一个文件", "merge_chapters_into_one_file")
        self._dl_skip = sec._check("跳过已存在", "skip_existing")
        self._dl_concurrent = sec._spin("并发下载数", "max_concurrent_downloads", 1, 16)
        self._dl_parallel = sec._spin("同源章节并发", "max_parallel_chapters", 1, 8, " 路")
        self.tabs.addTab(sec, "下载")

    def _build_library(self) -> None:
        sec = _Section()
        self._lib_dir = sec._line("书架导出目录", "shelf_export_dir")
        self._lib_webdav = sec._line("WebDAV 地址", "webdav_url", "预留")
        self.tabs.addTab(sec, "书架")

    def _build_diag(self) -> None:
        sec = _Section()
        self._diag_level = sec._combo("日志级别", "verbose_logging", ["INFO", "DEBUG"])
        self._diag_dir = sec._line("日志目录", "log_dir", "重启后生效")
        self._diag_warn = sec._spin("坏源告警间隔(小时)", "broken_source_warn_interval_hours", 1, 168, " h")
        self._diag_disable = sec._spin("自动禁用阈值(失败次数)", "auto_disable_after_failures", 1, 20)
        self._diag_strategy = sec._combo("自检策略", "selfcheck_strategy", ["off", "soft", "strict"])
        self.tabs.addTab(sec, "诊断")

    def _build_adblock(self) -> None:
        sec = _Section()
        self._ad_dir = sec._line("额外规则目录", "extra_rule_dir", "追加/覆盖内置规则")
        self.tabs.addTab(sec, "广告规则")

    def _build_llm(self) -> None:
        """LLM 设置 Tab：云端模型 + 本地 Ollama。"""
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QGroupBox, QProgressBar

        sec = _Section()

        # 云端模型组
        cloud_group = QGroupBox("云端模型（OpenAI 兼容）")
        cloud_form = QFormLayout(cloud_group)
        cloud_form.setContentsMargins(12, 8, 12, 8)
        cloud_form.setSpacing(6)

        self._llm_api_key = QLineEdit()
        self._llm_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._llm_api_key.setPlaceholderText("sk-...")
        self._llm_api_key.setProperty("key", "llm_cloud_api_key")
        cloud_form.addRow("API Key", self._llm_api_key)

        self._llm_base_url = QLineEdit()
        self._llm_base_url.setPlaceholderText("https://api.openai.com/v1")
        self._llm_base_url.setProperty("key", "llm_cloud_base_url")
        cloud_form.addRow("Base URL", self._llm_base_url)

        self._llm_model = QLineEdit()
        self._llm_model.setPlaceholderText("gpt-4 / qwen-plus / ...")
        self._llm_model.setProperty("key", "llm_cloud_model")
        cloud_form.addRow("模型名", self._llm_model)

        sec._form.addRow("云端配置", cloud_group)

        # 本地模型组
        local_group = QGroupBox("本地模型（Ollama）")
        local_form = QFormLayout(local_group)
        local_form.setContentsMargins(12, 8, 12, 8)
        local_form.setSpacing(6)

        self._llm_ollama_url = QLineEdit()
        self._llm_ollama_url.setPlaceholderText("http://127.0.0.1:11434")
        self._llm_ollama_url.setText("http://127.0.0.1:11434")
        self._llm_ollama_url.setProperty("key", "llm_local_base_url")
        local_form.addRow("Ollama 地址", self._llm_ollama_url)

        self._llm_ollama_model = QComboBox()
        self._llm_ollama_model.setEditable(True)
        self._llm_ollama_model.setProperty("key", "llm_local_model")
        local_form.addRow("模型", self._llm_ollama_model)

        ollama_btn_row = QHBoxLayout()
        self._llm_ollama_start_btn = QPushButton("🤖 启动 Ollama")
        self._llm_ollama_start_btn.clicked.connect(self._on_ollama_start)
        ollama_btn_row.addWidget(self._llm_ollama_start_btn)
        self._llm_ollama_status = QLabel("未知")
        self._llm_ollama_status.setStyleSheet("font-size: 11px;")
        ollama_btn_row.addWidget(self._llm_ollama_status)
        ollama_btn_row.addStretch(1)
        local_form.addRow("状态", ollama_btn_row)

        refresh_row = QHBoxLayout()
        self._llm_refresh_models_btn = QPushButton("刷新模型列表")
        self._llm_refresh_models_btn.clicked.connect(self._on_refresh_models)
        refresh_row.addWidget(self._llm_refresh_models_btn)
        refresh_row.addStretch(1)
        local_form.addRow("模型列表", refresh_row)

        sec._form.addRow("本地配置", local_group)

        self.tabs.addTab(sec, "LLM")

    def _on_ollama_start(self) -> None:
        """启动 Ollama 并刷新状态。"""
        from framework.llm import OllamaManager
        url = self._llm_ollama_url.text().strip() or "http://127.0.0.1:11434"
        self._llm_ollama_status.setText("启动中...")
        self._llm_ollama_start_btn.setEnabled(False)

        mgr = OllamaManager(url)
        ok = mgr.start()
        if ok:
            self._llm_ollama_status.setText("运行中 ✓")
            self._on_refresh_models()
        else:
            self._llm_ollama_status.setText("启动失败（未安装？）")
        self._llm_ollama_start_btn.setEnabled(True)

    def _on_refresh_models(self) -> None:
        """获取 Ollama 模型列表。"""
        from framework.llm import OllamaManager
        url = self._llm_ollama_url.text().strip() or "http://127.0.0.1:11434"
        mgr = OllamaManager(url)
        models = mgr.models()
        self._llm_ollama_model.clear()
        if models:
            self._llm_ollama_model.addItems(models)
            self._llm_ollama_status.setText(f"运行中（{len(models)} 模型）")
        else:
            self._llm_ollama_status.setText("未获取到模型列表")

    # ------------------------------------------------------------------ #
    # 载入 / 保存
    # ------------------------------------------------------------------ #
    def _load_values(self) -> None:
        g = self._sm.get
        self._net_ua.setText(g("network", "default_user_agent", ""))
        self._net_timeout.setValue(int(g("network", "default_timeout", 10)))
        self._net_retries.setValue(int(g("network", "default_retries", 3)))
        self._net_interval.setValue(int(g("network", "default_request_interval", 0)))
        self._net_proxy.setText(g("network", "proxy") or "")
        self._net_concurrent.setValue(int(g("network", "concurrent_search_sources", 4)))
        self._net_img_max.setValue(int(g("network", "max_bytes_per_image", 5242880) / 1024 // 1024))

        theme = g("ui", "theme", "sakura")
        self._ui_theme.setCurrentText(theme)
        self._ui_font.setValue(float(g("ui", "font_scale", 1.0)))
        self._ui_cover_cache.setValue(int(g("ui", "cover_cache_size_mb", 256)))
        self._ui_page_size.setValue(int(g("ui", "default_page_size", 20)))
        self._ui_bg_image.setText(g("ui", "background_image", "") or "")
        self._ui_bg_opacity.setValue(float(g("ui", "background_opacity", 0.6)))
        self._ui_reading_bg.setText(g("ui", "reading_bg", "") or "")
        self._ui_reading_font.setValue(int(g("ui", "reading_font_size", 0)))

        self._dl_dir.setText(g("download", "output_dir", "downloads"))
        self._dl_tpl.setText(g("download", "naming_template", ""))
        self._dl_merge.setChecked(bool(g("download", "merge_chapters_into_one_file", False)))
        self._dl_skip.setChecked(bool(g("download", "skip_existing", True)))
        self._dl_concurrent.setValue(int(g("download", "max_concurrent_downloads", 6)))
        self._dl_parallel.setValue(int(g("download", "max_parallel_chapters", 3)))

        self._lib_dir.setText(g("library", "shelf_export_dir", "library"))
        self._lib_webdav.setText(g("library", "webdav_url") or "")

        self._diag_level.setCurrentText("DEBUG" if bool(g("diagnostics", "verbose_logging", False)) else "INFO")
        self._diag_dir.setText(g("diagnostics", "log_dir", "logs"))
        self._diag_warn.setValue(int(g("sources_runtime", "broken_source_warn_interval_hours", 24)))
        self._diag_disable.setValue(int(g("sources_runtime", "auto_disable_after_failures", 3)))
        self._diag_strategy.setCurrentText(g("sources_runtime", "selfcheck_strategy", "soft"))

        self._ad_dir.setText(g("adblock", "extra_rule_dir", ""))

        # LLM 设置（来自 LlmKeyStore，不存 app_config.json 的 Key）
        try:
            from framework.llm import LlmKeyStore
            ks = LlmKeyStore()
            cloud = ks.cloud()
            local = ks.local()
            self._llm_api_key.setText(cloud.get("api_key", ""))
            self._llm_base_url.setText(cloud.get("base_url", ""))
            self._llm_model.setText(cloud.get("model", ""))
            self._llm_ollama_url.setText(local.get("base_url", "http://127.0.0.1:11434"))
            if local.get("model"):
                self._llm_ollama_model.setCurrentText(local["model"])
        except Exception:  # noqa: BLE001
            pass

    def _on_apply(self) -> None:
        """把所有控件值写回 settings 并保存。"""
        s = self._sm
        s.set("network", "default_user_agent", self._net_ua.text().strip())
        s.set("network", "default_timeout", self._net_timeout.value())
        s.set("network", "default_retries", self._net_retries.value())
        s.set("network", "default_request_interval", self._net_interval.value())
        s.set("network", "proxy", self._net_proxy.text().strip() or None)
        s.set("network", "concurrent_search_sources", self._net_concurrent.value())
        s.set("network", "max_bytes_per_image", self._net_img_max.value() * 1024 * 1024)

        s.set("ui", "font_scale", self._ui_font.value())
        s.set("ui", "cover_cache_size_mb", self._ui_cover_cache.value())
        s.set("ui", "default_page_size", self._ui_page_size.value())
        s.set("ui", "background_image", self._ui_bg_image.text().strip())
        s.set("ui", "background_opacity", self._ui_bg_opacity.value())
        s.set("ui", "reading_bg", self._ui_reading_bg.text().strip())
        s.set("ui", "reading_font_size", self._ui_reading_font.value())

        s.set("download", "output_dir", self._dl_dir.text().strip() or "downloads")
        s.set("download", "naming_template", self._dl_tpl.text().strip())
        s.set("download", "merge_chapters_into_one_file", self._dl_merge.isChecked())
        s.set("download", "skip_existing", self._dl_skip.isChecked())
        s.set("download", "max_concurrent_downloads", self._dl_concurrent.value())
        s.set("download", "max_parallel_chapters", self._dl_parallel.value())

        s.set("library", "shelf_export_dir", self._lib_dir.text().strip() or "library")
        s.set("library", "webdav_url", self._lib_webdav.text().strip() or None)

        s.set("diagnostics", "verbose_logging", self._diag_level.currentText() == "DEBUG")
        s.set("diagnostics", "log_dir", self._diag_dir.text().strip() or "logs")
        s.set("sources_runtime", "broken_source_warn_interval_hours", self._diag_warn.value())
        s.set("sources_runtime", "auto_disable_after_failures", self._diag_disable.value())
        s.set("sources_runtime", "selfcheck_strategy", self._diag_strategy.currentText())

        s.set("adblock", "extra_rule_dir", self._ad_dir.text().strip())

        # LLM 设置 → 写入 LlmKeyStore（data/llm_keys.json，不碰 app_config.json）
        try:
            from framework.llm import LlmKeyStore
            ks = LlmKeyStore()
            existing = ks.load()
            cloud = existing.get("cloud") or {}
            cloud["api_key"] = self._llm_api_key.text().strip()
            cloud["base_url"] = self._llm_base_url.text().strip()
            cloud["model"] = self._llm_model.text().strip()
            local = existing.get("local") or {}
            local["base_url"] = self._llm_ollama_url.text().strip() or "http://127.0.0.1:11434"
            local["model"] = self._llm_ollama_model.currentText().strip()
            ks.save({"cloud": cloud, "local": local})
        except Exception:  # noqa: BLE001
            pass

        s.save()
        self.settings_applied.emit()

    def _on_theme_selected(self, idx: int) -> None:
        """主题选择实时切换全局样式。"""
        theme = self._ui_theme.currentText()
        self._sm.set("ui", "theme", theme)
        self._tm.switch_to(theme)
        self.theme_changed.emit(theme)

    def _on_reset(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        resp = QMessageBox.question(
            self, "恢复默认", "确定恢复所有设置为默认值吗？", QMessageBox.Yes | QMessageBox.No
        )
        if resp != QMessageBox.Yes:
            return
        self._sm.reset_to_defaults()
        self._sm.save()
        self._load_values()
        # 主题回到默认
        theme = self._sm.get("ui", "theme", "sakura")
        self._tm.switch_to(theme)
        self.theme_changed.emit(theme)

    def refresh(self) -> None:
        self._load_values()
