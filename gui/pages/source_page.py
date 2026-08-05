"""源管理界面（source_page.py）。

对应 ui-source.md 全部功能点：
- 工具栏：全启用 / 全禁用 / 全部诊断 / ＋添加源
- 源列表（行式）：名称、类型徽章、启用开关、健康灯、健康历史、权重、操作
- 单源操作：诊断 / 编辑 / 删除（二次确认：软删除或删文件）
- 诊断视图：失效源展开错误详情
- 空状态：无源时显示「去添加源」
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal, QThreadPool, QRunnable, QObject
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from framework.source_manager import (
    SourceManager,
    SourceHealth,
    HEALTH_OK,
    HEALTH_WARN,
    HEALTH_BROKEN,
)
from framework.selfcheck import StructureChecker

from gui.components import EmptyState
from .base_page import BasePage

# 类型徽章展示名
TYPE_LABEL = {"novel": "小说", "comic": "漫画", "video": "视频"}
# 健康历史最多展示的点数
HISTORY_DOTS = 6

# 健康灯颜色（绿/黄/红）
_HEALTH_COLORS = {
    HEALTH_OK: "#4CAF50",
    HEALTH_WARN: "#FF9800",
    HEALTH_BROKEN: "#F44336",
}

# 类型徽章配色
_TYPE_BADGE = {
    "novel": "#FF8AA5",
    "comic": "#6FA8FF",
    "video": "#5FCF8A",
}


class _DiagWorkerSignals(QObject):
    """诊断任务信号。"""

    finished = Signal(str, bool, str)  # (source_id, ok, error)


class _DiagTask(QRunnable):
    """后台对单个源跑一次自检。"""

    def __init__(self, checker, source, url):
        super().__init__()
        self.signals = _DiagWorkerSignals()
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
            pass  # 页面已销毁


def diagnose_url(source) -> str:
    """构造自检目标 URL：优先 discovery 列表入口，否则 base_url 或 homepage。

    返回绝对 URL。供诊断按钮 & 全量诊断调用。
    """
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


class SourceRow(QWidget):
    """单个源行：名称 / 类型徽章 / 启用开关 / 健康灯 / 历史 / 权重 / 操作。"""

    edit_clicked = Signal(str)  # source_id → 开编辑器
    login_clicked = Signal(object)  # source → 打开登录弹窗

    def __init__(self, source, health: SourceHealth, parent=None):
        super().__init__(parent)
        self._source = source
        self._source_id = source.source_id
        self._type = source.content_type
        self._health = health
        self._enabled_cb = None
        self._weight_cb = None
        self._diag_btn_cb = None
        self._delete_cb = None

        self.setObjectName("sourceRow")
        # 行最小高度保证行距舒适（内容超高时自适应增高，不锁死）
        self.setMinimumHeight(46)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(8)

        # 名称（flex 自动占满剩余空间；固定控件已收窄，长名不会挤掉操作按钮）
        self.name_label = QLabel(source.source_name)
        self.name_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        self.name_label.setToolTip(f"{source.source_name}\n{source.source_id}")
        self.name_label.setAlignment(Qt.AlignVCenter)
        self.name_label.setMinimumWidth(60)
        layout.addWidget(self.name_label, stretch=1)

        # 类型徽章
        self.badge = QLabel(TYPE_LABEL.get(self._type, self._type))
        self.badge.setStyleSheet(
            "font-size: 11px; border-radius: 8px; padding: 1px 10px;"
            " color: white; background: %s;" % _TYPE_BADGE.get(self._type, "#9E9E9E")
        )
        layout.addWidget(self.badge)

        # 启用开关
        self.enabled_box = QCheckBox("启用")
        self.enabled_box.setChecked(source.enabled)
        self.enabled_box.toggled.connect(self._on_enabled_toggled)
        layout.addWidget(self.enabled_box)

        # 健康灯
        self.health_dot = QLabel("●")
        self.health_dot.setFixedWidth(16)
        layout.addWidget(self.health_dot)

        # 健康历史（最多 HISTORY_DOTS 个点）
        self.history_label = QLabel("")
        self.history_label.setFixedWidth(HISTORY_DOTS * 9)
        layout.addWidget(self.history_label)

        # 权重
        self.weight_spin = QDoubleSpinBox()
        self.weight_spin.setRange(0.0, 10.0)
        self.weight_spin.setDecimals(1)
        self.weight_spin.setSingleStep(0.1)
        self.weight_spin.setValue(source.weight)
        self.weight_spin.setFixedWidth(50)
        self.weight_spin.setToolTip("搜索排序权重")
        self.weight_spin.valueChanged.connect(self._on_weight_changed)
        layout.addWidget(self.weight_spin)

        # 操作按钮（统一窄宽+清晰字号，保证一行放下且文字不裁剪）
        _btn_style = "font-size: 12px; padding: 3px 6px;"
        # 登录按钮：仅需要登录的源显示（无 auth 配置默认隐藏，省空间）
        has_auth = bool((source.raw.get("auth") or {}).get("login_url"))
        self.login_btn = QPushButton("登录")
        self.login_btn.setFixedWidth(46)
        self.login_btn.setStyleSheet(_btn_style)
        self.login_btn.setToolTip("登录该源站点并保存 Cookie（需要登录的源）")
        self.login_btn.setVisible(has_auth)
        self.login_btn.clicked.connect(lambda: self.login_clicked.emit(self._source))
        layout.addWidget(self.login_btn)

        self.diag_btn = QPushButton("诊断")
        self.diag_btn.setFixedWidth(46)
        self.diag_btn.setStyleSheet(_btn_style)
        self.diag_btn.clicked.connect(self._on_diag)
        layout.addWidget(self.diag_btn)

        self.edit_btn = QPushButton("编辑")
        self.edit_btn.setFixedWidth(46)
        self.edit_btn.setStyleSheet(_btn_style)
        self.edit_btn.clicked.connect(lambda: self.edit_clicked.emit(self._source_id))
        layout.addWidget(self.edit_btn)

        self.del_btn = QPushButton("删除")
        self.del_btn.setFixedWidth(46)
        self.del_btn.setStyleSheet(_btn_style)
        self.del_btn.clicked.connect(self._on_delete)
        layout.addWidget(self.del_btn)

        self._update_health(health)

    # ------------------------------------------------------------------ #
    def source(self):
        return self._source

    # 注入回调（SourcePage 统一管理写入，SourceRow 不直接依赖 manager）
    def set_enabled_cb(self, cb) -> None:
        self._enabled_cb = cb

    def set_weight_cb(self, cb) -> None:
        self._weight_cb = cb

    def set_diag_cb(self, cb) -> None:
        self._diag_btn_cb = cb

    def set_delete_cb(self, cb) -> None:
        self._delete_cb = cb

    # ------------------------------------------------------------------ #
    def _on_enabled_toggled(self, checked: bool) -> None:
        if self._enabled_cb:
            self._enabled_cb(self._source_id, checked)

    def _on_weight_changed(self, value: float) -> None:
        if self._weight_cb:
            self._weight_cb(self._source_id, value)

    def _on_diag(self) -> None:
        if self._diag_btn_cb:
            self._diag_btn_cb(self._source, self)

    def _on_delete(self) -> None:
        if self._delete_cb:
            self._delete_cb(self._source)

    # ------------------------------------------------------------------ #
    def set_enabled_state(self, enabled: bool) -> None:
        """同步勾选状态（不触发回调）。"""
        self.enabled_box.blockSignals(True)
        self.enabled_box.setChecked(enabled)
        self.enabled_box.blockSignals(False)

    def _update_health(self, health: SourceHealth) -> None:
        """刷新健康灯与历史点。"""
        state = health.state
        color = _HEALTH_COLORS.get(state, _HEALTH_COLORS[HEALTH_OK])
        self.health_dot.setStyleSheet("color: %s; font-size: 14px;" % color)
        self.health_dot.setToolTip(
            {"ok": "正常", "warn": "自检软失败", "broken": "失效"}.get(state, state)
        )
        history = health.history[-HISTORY_DOTS:]
        if history:
            dots = []
            for h in history:
                c = _HEALTH_COLORS.get(h, "#CCCCCC")
                dots.append('<span style="color:%s">●</span>' % c)
            self.history_label.setText("".join(dots))
        else:
            self.history_label.setText("")

    def set_health(self, health: SourceHealth) -> None:
        self._health = health
        self._update_health(health)

    def set_error(self, err: str) -> None:
        """显示/隐藏错误详情。err 为空 → 隐藏。"""
        self.setToolTip(err if err else "")

    def on_theme_changed(self) -> None:
        self.update()


class SourcePage(BasePage):
    """源管理列表页。"""

    # 对外信号：编辑某源 → App 层开编辑器（source_id="" 表示新建）
    edit_requested = Signal(str)

    def __init__(self, source_manager: SourceManager, checker: StructureChecker,
                 sources_dir, cookie_manager=None, parent=None):
        super().__init__(parent)
        self._manager = source_manager
        self._checker = checker
        self._sources_dir = Path(sources_dir)
        self._cookie_manager = cookie_manager
        self._rows: list = []
        self._diag_count = {"total": 0, "done": 0}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # 工具栏
        self._build_toolbar()
        layout.addLayout(self._toolbar)

        # 诊断进度
        self.progress_label = QLabel("")
        self.progress_label.setVisible(False)
        self.progress_label.setStyleSheet("color: palette(mid); font-size: 12px;")
        layout.addWidget(self.progress_label)

        # 源列表
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.scroll, stretch=1)

        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(4)
        self.scroll.setWidget(self.list_container)

        # 空状态（默认隐藏）
        self.empty_state = EmptyState()
        self.empty_state.add_source_clicked.connect(self._on_add_source)
        layout.addWidget(self.empty_state, stretch=1)

        self.refresh()

    # ------------------------------------------------------------------ #
    def _build_toolbar(self) -> None:
        self._toolbar = QHBoxLayout()
        self._toolbar.setSpacing(8)

        self.enable_all_btn = QPushButton("全启用")
        self.enable_all_btn.setFixedWidth(72)
        self.enable_all_btn.clicked.connect(self._on_enable_all)
        self._toolbar.addWidget(self.enable_all_btn)

        self.disable_all_btn = QPushButton("全禁用")
        self.disable_all_btn.setFixedWidth(72)
        self.disable_all_btn.clicked.connect(self._on_disable_all)
        self._toolbar.addWidget(self.disable_all_btn)

        self.diag_all_btn = QPushButton("全部诊断")
        self.diag_all_btn.setFixedWidth(88)
        self.diag_all_btn.clicked.connect(self._on_diagnose_all)
        self._toolbar.addWidget(self.diag_all_btn)

        self._toolbar.addStretch(1)

        # 版本信息（右侧，防挤压：允许收缩时省略，不参与 stretch）
        self.version_label = QLabel("")
        self.version_label.setStyleSheet(
            "color: palette(mid); font-size: 11px; padding-right: 8px;"
        )
        self.version_label.setAlignment(Qt.AlignVCenter)
        self._toolbar.addWidget(self.version_label)

        self.add_btn = QPushButton("＋添加源")
        self.add_btn.setFixedWidth(90)
        self.add_btn.clicked.connect(self._on_add_source)
        self._toolbar.addWidget(self.add_btn)

    # ------------------------------------------------------------------ #
    def _rebuild_rows(self) -> None:
        """从 SourceManager 清空并重建所有源行。"""
        while self.list_layout.count():
            child = self.list_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._rows = []

        sources = self._manager.all()
        # 版本信息：源总数 + 各类型数量
        self._update_version_label(sources)
        for source in sources:
            health = self._manager.get_health(source.source_id)
            row = SourceRow(source, health)
            row.set_enabled_cb(self._on_set_enabled)
            row.set_weight_cb(self._on_set_weight)
            row.set_diag_cb(self._on_diag_one)
            row.set_delete_cb(self._on_delete_source)
            row.login_clicked.connect(self._on_login)
            row.edit_clicked.connect(self.edit_requested)
            self.list_layout.addWidget(row)
            self._rows.append(row)
        self.list_layout.addStretch(1)
        self._update_empty_state()

    def refresh(self) -> None:
        self._rebuild_rows()

    def _update_empty_state(self) -> None:
        has_sources = len(self._manager.all()) > 0
        self.scroll.setVisible(has_sources)
        self.empty_state.setVisible(not has_sources)

    def _update_version_label(self, sources: list) -> None:
        """工具栏版本信息：Schema 版本 + 源总数 + 类型分布。"""
        total = len(sources)
        counts = {}
        for s in sources:
            counts[s.content_type] = counts.get(s.content_type, 0) + 1
        parts = []
        for k in ("novel", "comic", "video"):
            if k in counts:
                parts.append(f"{TYPE_LABEL.get(k, k)} {counts[k]}")
        txt = f"Schema v2 · 共 {total} 个源"
        if parts:
            txt += " · " + " · ".join(parts)
        self.version_label.setText(txt)
        self.version_label.setToolTip(txt)

    # ------------------------------------------------------------------ #
    def _on_set_enabled(self, source_id: str, enabled: bool) -> None:
        self._manager.set_enabled(source_id, enabled)

    def _on_set_weight(self, source_id: str, weight: float) -> None:
        self._manager.set_weight(source_id, weight)

    # ---------- 启停 / 诊断 ------------------------------------------- #
    def _on_enable_all(self) -> None:
        for s in self._manager.all():
            self._manager.set_enabled(s.source_id, True)
        self._sync_row_enabled()

    def _on_disable_all(self) -> None:
        for s in self._manager.all():
            self._manager.set_enabled(s.source_id, False)
        self._sync_row_enabled()

    def _sync_row_enabled(self) -> None:
        for s in self._manager.all():
            for row in self._rows:
                if row.source().source_id == s.source_id:
                    row.set_enabled_state(s.enabled)
                    break

    def _on_diagnose_all(self) -> None:
        """全部诊断：后台逐个跑 selfcheck，更新健康灯。"""
        sources = [s for s in self._manager.all() if s.enabled]
        if not sources:
            return
        for s in sources:
            self._run_diag(s)
        self.progress_label.setText(f"正在诊断 (0/{len(sources)})...")
        self.progress_label.setVisible(True)
        self._diag_count = {"total": len(sources), "done": 0}
        self.diag_all_btn.setEnabled(False)

    def _run_diag(self, source) -> None:
        """后台跑单源自检，完成后更新健康 + 刷新对应行。"""
        url = diagnose_url(source)
        if not url:
            self._on_diag_done(source.source_id, False, "缺少可自检的 URL")
            return
        task = _DiagTask(self._checker, source, url)
        task.signals.finished.connect(self._on_diag_done)
        self._diag_task = task  # 防 GC
        QThreadPool.globalInstance().start(task)

    def _on_diag_one(self, source, row) -> None:
        """单源诊断：跑自检，完成后刷新该行健康灯。"""
        self._diag_count = {"total": 1, "done": 0}
        self.progress_label.setVisible(False)
        self._run_diag(source)

    def _on_diag_done(self, source_id: str, ok: bool, err: str) -> None:
        """诊断完成：更新 SourceManager 健康状态 + 刷新行。

        状态映射：
        - 自检通过 → 绿（ok）
        - 软失败（check 返回 False，无异常）→ 黄（warn）
        - 硬失败（strict 抛 StructureChangedError / 其他异常）→ 红（broken）
        """
        if len(err) > 80:
            err = err[:80] + "..."
        if ok:
            state = HEALTH_OK
        elif err:
            state = HEALTH_BROKEN
        else:
            state = HEALTH_WARN
        self._manager.update_health(source_id, state, err)
        for row in self._rows:
            if row.source().source_id == source_id:
                health = self._manager.get_health(source_id)
                row.set_health(health)
                row.set_error(err if not ok else "")
                break
        # 进度
        self._diag_count["done"] += 1
        done, total = self._diag_count["done"], self._diag_count["total"]
        if done >= total:
            self.progress_label.setText("诊断完成")
            self.diag_all_btn.setEnabled(True)
            self._schedule_progress_hide()
        else:
            self.progress_label.setText(f"正在诊断 ({done}/{total})...")

    def _schedule_progress_hide(self) -> None:
        from PySide6.QtCore import QTimer

        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(lambda: self.progress_label.setVisible(False))
        t.start(3000)

    # ---------- 添加 / 编辑 / 删除 ------------------------------------ #
    def _on_add_source(self) -> None:
        self.edit_requested.emit("")

    def _on_login(self, source) -> None:
        """打开登录弹窗：内嵌浏览器登录源站点，保存 Cookie。"""
        if self._cookie_manager is None:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(self, "登录", "Cookie 管理器未初始化")
            return
        from gui.components.login_dialog import LoginDialog

        # 登录 URL：优先 auth.login_url，否则 homepage / base_url
        auth = (source.raw.get("auth") or {})
        login_url = (
            auth.get("login_url")
            or source.homepage
            or source.base_url
        )
        if not login_url:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(self, "登录", "该源未配置登录地址")
            return
        markers = list(auth.get("login_success_markers") or [])
        dlg = LoginDialog(
            source.source_id,
            login_url,
            cookie_manager=self._cookie_manager,
            success_markers=markers or None,
            parent=self,
        )
        dlg.logged_in.connect(self._on_login_saved)
        dlg.exec()

    def _on_login_saved(self, source_id: str) -> None:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.information(self, "登录", f"「{source_id}」Cookie 已保存，请求将自动附带")

    def _on_delete_source(self, source) -> None:
        from PySide6.QtWidgets import QMessageBox

        name = source.source_name or source.source_id
        msg = QMessageBox(self)
        msg.setWindowTitle("删除源")
        msg.setText(f"确认删除「{name}」？")
        msg.setInformativeText(
            "默认软删除（禁用该源，配置保留可恢复）。也可同时删除配置文件（不可恢复）。"
        )
        soft_btn = msg.addButton("软删除", QMessageBox.AcceptRole)
        hard_btn = msg.addButton("同时删除文件", QMessageBox.DestructiveRole)
        cancel_btn = msg.addButton("取消", QMessageBox.RejectRole)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == soft_btn:
            self._manager.set_enabled(source.source_id, False)
            self.refresh()
        elif clicked == hard_btn:
            self._confirm_hard_delete(source)

    def _confirm_hard_delete(self, source) -> None:
        from PySide6.QtWidgets import QMessageBox

        resp = QMessageBox.warning(
            self,
            "永久删除",
            f"配置文件 {source.source_path} 将被永久删除，此操作不可恢复。确定吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return
        try:
            Path(source.source_path).unlink(missing_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "删除失败", f"配置文件删除失败：{exc}")
            return
        # 从内存移除并重建列表
        self._manager.load_dir(self._sources_dir)
        self.refresh()

    # ------------------------------------------------------------------ #
    def on_theme_changed(self, theme: str) -> None:
        for row in self._rows:
            row.on_theme_changed()