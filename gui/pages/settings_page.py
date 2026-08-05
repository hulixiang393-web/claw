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
        self.tabs.addTab(sec, "UI")

    def _on_cache_clear(self) -> None:
        """清除封面内存缓存 + 临时合成图目录。"""
        from PySide6.QtGui import QPixmapCache
        from PySide6.QtWidgets import QMessageBox

        QPixmapCache.clear()
        try:
            from gui.components.cover_loader import CoverLoader

            CoverLoader.instance().clear_cache()
        except Exception:  # noqa: BLE001
            pass
        # 清 data/cache 临时合成图
        from pathlib import Path

        cache_dir = Path(__file__).resolve().parent.parent.parent / "data" / "cache"
        removed = 0
        if cache_dir.is_dir():
            for f in cache_dir.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
        QMessageBox.information(self, "清除缓存", f"已清除缓存（删除 {removed} 个临时文件）")

    def _build_download(self) -> None:
        sec = _Section()
        self._dl_dir = sec._line_browse("输出目录", "output_dir")
        self._dl_tpl = sec._line("命名模板", "naming_template")
        self._dl_merge = sec._check("合并章节为一个文件", "merge_chapters_into_one_file")
        self._dl_skip = sec._check("跳过已存在", "skip_existing")
        self._dl_concurrent = sec._spin("并发下载数", "max_concurrent_downloads", 1, 16)
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

        self._dl_dir.setText(g("download", "output_dir", "downloads"))
        self._dl_tpl.setText(g("download", "naming_template", ""))
        self._dl_merge.setChecked(bool(g("download", "merge_chapters_into_one_file", False)))
        self._dl_skip.setChecked(bool(g("download", "skip_existing", True)))
        self._dl_concurrent.setValue(int(g("download", "max_concurrent_downloads", 6)))

        self._lib_dir.setText(g("library", "shelf_export_dir", "library"))
        self._lib_webdav.setText(g("library", "webdav_url") or "")

        self._diag_level.setCurrentText("DEBUG" if bool(g("diagnostics", "verbose_logging", False)) else "INFO")
        self._diag_dir.setText(g("diagnostics", "log_dir", "logs"))
        self._diag_warn.setValue(int(g("sources_runtime", "broken_source_warn_interval_hours", 24)))
        self._diag_disable.setValue(int(g("sources_runtime", "auto_disable_after_failures", 3)))
        self._diag_strategy.setCurrentText(g("sources_runtime", "selfcheck_strategy", "soft"))

        self._ad_dir.setText(g("adblock", "extra_rule_dir", ""))

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

        s.set("download", "output_dir", self._dl_dir.text().strip() or "downloads")
        s.set("download", "naming_template", self._dl_tpl.text().strip())
        s.set("download", "merge_chapters_into_one_file", self._dl_merge.isChecked())
        s.set("download", "skip_existing", self._dl_skip.isChecked())
        s.set("download", "max_concurrent_downloads", self._dl_concurrent.value())

        s.set("library", "shelf_export_dir", self._lib_dir.text().strip() or "library")
        s.set("library", "webdav_url", self._lib_webdav.text().strip() or None)

        s.set("diagnostics", "verbose_logging", self._diag_level.currentText() == "DEBUG")
        s.set("diagnostics", "log_dir", self._diag_dir.text().strip() or "logs")
        s.set("sources_runtime", "broken_source_warn_interval_hours", self._diag_warn.value())
        s.set("sources_runtime", "auto_disable_after_failures", self._diag_disable.value())
        s.set("sources_runtime", "selfcheck_strategy", self._diag_strategy.currentText())

        s.set("adblock", "extra_rule_dir", self._ad_dir.text().strip())

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
