"""源选择引导对话框（source_select_dialog.py）。

首次启动引导 + 设置界面「源选择」共用：
- 列出所有源（按类型分组），每行显示 名称 + 类型徽章 + 健康灯
- 默认勾选当前已启用的源（首次启动默认全部启用 → 全部勾选）
- 确定后通过 selected_ids() 返回勾选结果，由调用方应用到源启用状态
- first_run=True 时额外提供「跳过」按钮（跳过 = 保持默认全部启用）

样式跟随 palette()，不写死色值，兼容主题切换。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from framework.source_manager import SourceManager

# 类型徽章展示名（与 source_page 一致）
TYPE_LABEL = {"novel": "小说", "comic": "漫画", "video": "视频"}
# 类型徽章配色（与 source_page 一致）
_TYPE_BADGE = {
    "novel": "#FF8AA5",
    "comic": "#6FA8FF",
    "video": "#5FCF8A",
}
# 健康灯状态文案 / 颜色
_HEALTH_LABEL = {"ok": "正常", "warn": "自检软失败", "broken": "失效"}
_HEALTH_COLORS = {"ok": "#4CAF50", "warn": "#FF9800", "broken": "#F44336"}


class SourceSelectDialog(QDialog):
    """勾选要启用的源。确认后调用 selected_ids() 取勾选结果。"""

    def __init__(
        self, source_manager: SourceManager, first_run: bool = False, parent=None
    ):
        super().__init__(parent)
        self._manager = source_manager
        self._checks: dict = {}  # source_id → QCheckBox

        self.setWindowTitle("选择启用源" if not first_run else "首次启动 · 选择启用源")
        self.resize(520, 560)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # 顶部说明
        desc = QLabel(
            "勾选要启用的源，未勾选的源将被禁用，不再出现在发现 / 搜索页。"
            + ("\n可随时在「设置 → 源选择」中重新调整。" if first_run else "")
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: palette(text2); font-size: 12px;")
        layout.addWidget(desc)

        # 工具栏：全选 / 全不选 + 已选计数
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.check_all_btn = QPushButton("全选")
        self.check_all_btn.setFixedWidth(64)
        self.check_all_btn.clicked.connect(self._check_all)
        bar.addWidget(self.check_all_btn)
        self.uncheck_all_btn = QPushButton("全不选")
        self.uncheck_all_btn.setFixedWidth(64)
        self.uncheck_all_btn.clicked.connect(self._uncheck_all)
        bar.addWidget(self.uncheck_all_btn)
        bar.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: palette(mid); font-size: 12px;")
        bar.addWidget(self.count_label)
        layout.addLayout(bar)

        # 源列表（按类型分组，可滚动）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(scroll, stretch=1)

        container = QWidget()
        self.list_layout = QVBoxLayout(container)
        self.list_layout.setContentsMargins(4, 4, 4, 4)
        self.list_layout.setSpacing(4)
        scroll.setWidget(container)

        self._build_list()

        # 按钮：确定 / 取消（首次启动额外提供「跳过」）
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        if first_run:
            skip_btn = buttons.addButton("跳过", QDialogButtonBox.DestructiveRole)
            skip_btn.clicked.connect(self.reject)
            skip_btn.setToolTip("保持默认全部启用，稍后可在设置里重新选择")
        layout.addWidget(buttons)

        self._update_count()
        self._center()

    # ------------------------------------------------------------------ #
    def _build_list(self) -> None:
        """按类型分组列出所有源（含禁用源，可重新勾选启用）。"""
        sources = sorted(
            self._manager.all(),
            key=lambda s: (s.content_type, s.source_name or s.source_id),
        )
        current_type = None
        for s in sources:
            if s.content_type != current_type:
                current_type = s.content_type
                header = QLabel(TYPE_LABEL.get(current_type, current_type))
                header.setStyleSheet(
                    "font-size: 12px; font-weight: bold; padding-top: 6px;"
                    " color: %s;" % _TYPE_BADGE.get(current_type, "#888888")
                )
                self.list_layout.addWidget(header)
            self.list_layout.addWidget(self._make_row(s))

    def _make_row(self, source) -> QWidget:
        """单源行：勾选框（名称）+ 类型徽章 + 健康灯。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 2, 8, 2)
        lay.setSpacing(8)

        cb = QCheckBox(source.source_name or source.source_id)
        cb.setChecked(source.enabled)  # 默认勾选当前已启用的源
        cb.setToolTip(source.source_id)
        cb.toggled.connect(lambda _: self._update_count())
        lay.addWidget(cb, stretch=1)
        self._checks[source.source_id] = cb

        # 类型徽章
        badge = QLabel(TYPE_LABEL.get(source.content_type, source.content_type))
        badge.setStyleSheet(
            "font-size: 11px; border-radius: 8px; padding: 1px 10px;"
            " color: white; background: %s;" % _TYPE_BADGE.get(source.content_type, "#9E9E9E")
        )
        lay.addWidget(badge)

        # 健康灯（含错误详情 ToolTip）
        health = self._manager.get_health(source.source_id)
        state = health.state
        dot = QLabel("●")
        dot.setStyleSheet(
            "color: %s; font-size: 14px;" % _HEALTH_COLORS.get(state, _HEALTH_COLORS["ok"])
        )
        tip = _HEALTH_LABEL.get(state, state)
        if health.last_error:
            tip += f"\n{health.last_error}"
        dot.setToolTip(tip)
        lay.addWidget(dot)

        return row

    # ------------------------------------------------------------------ #
    def selected_ids(self) -> list:
        """返回勾选中的源 ID 列表。"""
        return [sid for sid, cb in self._checks.items() if cb.isChecked()]

    def _check_all(self) -> None:
        for cb in self._checks.values():
            cb.setChecked(True)

    def _uncheck_all(self) -> None:
        for cb in self._checks.values():
            cb.setChecked(False)

    def _update_count(self) -> None:
        n = sum(1 for cb in self._checks.values() if cb.isChecked())
        self.count_label.setText(f"已选 {n} / {len(self._checks)} 个源")

    def _on_accept(self) -> None:
        """确定：一个都不勾选时二次确认（避免误把全部源禁用）。"""
        if self._checks and not self.selected_ids():
            resp = QMessageBox.question(
                self,
                "未启用任何源",
                "一个源都不启用的话，发现 / 搜索将没有可用源。\n仍要继续吗？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return
        self.accept()

    def _center(self) -> None:
        if self.parentWidget() is not None:
            parent = self.parentWidget().window()
            geo = parent.frameGeometry()
            self.move(
                geo.center().x() - self.width() // 2,
                geo.center().y() - self.height() // 2,
            )
