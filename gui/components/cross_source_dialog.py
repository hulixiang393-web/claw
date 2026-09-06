"""跨源换源候选对话框（cross_source_dialog.py）。

当阅读界面当前源失效时，列出从其他同类型源搜索到的候选（相似度 ≥90%）供用户
点选。用户点选候选后「换源」按钮启用，确定后 `selected` 保存选中的候选。

样式跟随 palette()，不写死色值，兼容主题切换（沿用 gui/components/ 对话框惯例）。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from framework.cross_source import CrossSourceCandidate


class _CandidateRow(QWidget):
    """单条候选卡：封面缩略图 + 源名 · 标题 · 相似度%。"""

    def __init__(self, cand: CrossSourceCandidate, parent=None):
        super().__init__(parent)
        self.candidate = cand
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)

        # 封面缩略图（无封面时显示占位）
        self.cover = QLabel("封面")
        self.cover.setFixedSize(64, 80)
        self.cover.setScaledContents(True)
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setStyleSheet(
            "background: palette(mid); color: palette(text2);"
            " border-radius: 4px; font-size: 10px;"
        )
        lay.addWidget(self.cover)
        self._load_cover(cand.cover)

        # 文字信息列
        info = QVBoxLayout()
        info.setSpacing(2)
        title = QLabel(cand.title or cand.url)
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        info.addWidget(title)

        meta = QLabel(f"{cand.source_name} · 相似度 {cand.similarity * 100:.0f}%")
        meta.setStyleSheet("color: palette(mid); font-size: 11px;")
        info.addWidget(meta)

        raw = QLabel(cand.url or "")
        raw.setStyleSheet("color: palette(mid); font-size: 10px;")
        raw.setWordWrap(True)
        info.addWidget(raw)
        lay.addLayout(info, stretch=1)

    def _load_cover(self, url: str) -> None:
        """异步加载封面（无则保留占位）。"""
        if not url:
            return
        try:
            from .cover_loader import CoverLoader

            def _set(pm):
                try:
                    if pm is None or pm.isNull():
                        return
                    self.cover.setPixmap(pm)
                except RuntimeError:
                    pass

            CoverLoader.instance().load(url, _set, cache=True, persist=False)
        except Exception:  # noqa: BLE001
            pass


class CrossSourceDialog(QDialog):
    """跨源换源候选选择对话框。

    构造：CrossSourceDialog(candidates, parent)
    - candidates：list[CrossSourceCandidate]，可为空（显示「无结果」提示）
    - 用户点选候选后「换源」按钮启用；确定后 self.selected = 选中的候选。
    """

    def __init__(self, candidates, parent=None):
        super().__init__(parent)
        self._candidates = list(candidates or [])
        self.selected: Optional[CrossSourceCandidate] = None
        self._items: list = []  # 防止 item 被 GC

        self.setWindowTitle("跨源换源")
        self.resize(420, 460)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        desc = QLabel(
            "当前源可能已失效，请在以下其他同类型源候选中选择要继续阅读的作品："
            if self._candidates
            else "未找到相似度 ≥90% 的作品（其他同类型源均无匹配结果）。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: palette(text2); font-size: 12px;")
        layout.addWidget(desc)

        self.list = QListWidget()
        self.list.setWordWrap(True)
        layout.addWidget(self.list, stretch=1)

        if self._candidates:
            self._populate()
        self.list.itemSelectionChanged.connect(self._on_selection)

        # 按钮：换源（无命中候选时禁用）/ 取消
        buttons = QDialogButtonBox(parent=self)
        self.switch_btn = buttons.addButton("换源", QDialogButtonBox.AcceptRole)
        self.switch_btn.setEnabled(False)
        if not self._candidates:
            self.switch_btn.setToolTip("没有可换的候选源")
        cancel = buttons.addButton("取消", QDialogButtonBox.RejectRole)
        cancel.setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _populate(self) -> None:
        """填充候选卡列表。"""
        self.list.clear()
        self._items.clear()
        for cand in self._candidates:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, cand)
            row = _CandidateRow(cand)
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            self._items.append(item)

    def _on_selection(self) -> None:
        """选中候选后启用「换源」按钮。"""
        item = self.list.currentItem()
        self.switch_btn.setEnabled(item is not None)

    def _on_accept(self) -> None:
        """确定：保存选中的候选。"""
        item = self.list.currentItem()
        if item is None:
            return
        self.selected = item.data(Qt.UserRole)
        self.accept()
