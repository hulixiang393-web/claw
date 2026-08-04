"""章节范围选择对话框（download_range_dialog.py）。

点详情抽屉「下载」时弹出：可选全部章节或指定起止范围。
样式跟随 palette()，不写死色值，兼容主题切换。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)


class DownloadRangeDialog(QDialog):
    """选择下载章节范围（视频任务可选画质）。"""

    def __init__(self, book_title: str, total: int, content_type: str = "", qualities: list | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("下载章节")
        self.setMinimumWidth(360)
        self._total = total
        self._selected: tuple | None = None  # (start_idx, end_idx) 1-indexed 或 None=全部
        self._quality = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel(f"《{book_title}》")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        title.setWordWrap(True)
        layout.addWidget(title)

        # 全部
        self.all_radio = QRadioButton(f"全部章节（{total} 章）")
        self.all_radio.setChecked(True)
        layout.addWidget(self.all_radio)

        # 指定范围
        self.range_radio = QRadioButton("指定范围")
        layout.addWidget(self.range_radio)

        range_row = QHBoxLayout()
        range_row.setContentsMargins(24, 0, 0, 0)
        self.start_spin = QSpinBox()
        self.start_spin.setRange(1, max(1, total))
        self.end_spin = QSpinBox()
        self.end_spin.setRange(1, max(1, total))
        self.end_spin.setValue(max(1, total))
        range_row.addWidget(QLabel("从"))
        range_row.addWidget(self.start_spin, stretch=1)
        range_row.addWidget(QLabel("到"))
        range_row.addWidget(self.end_spin, stretch=1)
        layout.addLayout(range_row)

        self.hint = QLabel("")
        self.hint.setStyleSheet("color: palette(mid); font-size: 11px;")
        layout.addWidget(self.hint)

        # 视频任务：画质选择（默认最高；qualities 缺省用通用档位）
        self._quality = ""
        if content_type == "video":
            q_row = QHBoxLayout()
            q_row.addWidget(QLabel("画质"))
            self.quality_combo = QComboBox()
            q_opts = qualities if qualities else ["最高", "1080p", "720p", "480p", "360p"]
            self.quality_combo.addItems(q_opts)
            # 顶掉 label 里的中文 → 存英文 key；"最高"→best
            self.quality_combo.setCurrentIndex(0)
            q_row.addWidget(self.quality_combo, stretch=1)
            layout.addLayout(q_row)

        # 确定/取消
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.button(QDialogButtonBox.Ok).setText("加入下载")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.range_radio.toggled.connect(lambda on: (self.start_spin.setEnabled(on), self.end_spin.setEnabled(on)))
        self.end_spin.valueChanged.connect(self._update_hint)

        self._update_hint()
        self._center()

    def _update_hint(self) -> None:
        s, e = self.start_spin.value(), self.end_spin.value()
        if s <= e:
            self.hint.setText(f"将下载第 {s} ~ {e} 章，共 {e - s + 1} 章")
        else:
            self.hint.setText("结束章节应不小于起始章节")

    def _center(self) -> None:
        if self.parentWidget() is not None:
            parent = self.parentWidget().window()
            geo = parent.frameGeometry()
            self.move(
                geo.center().x() - self.width() // 2,
                geo.center().y() - self.height() // 2,
            )

    def _on_accept(self) -> None:
        s, e = self.start_spin.value(), self.end_spin.value()
        if self.range_radio.isChecked():
            if s > e:
                self.hint.setText("结束章节应不小于起始章节")
                return
            self._selected = (s, e)
        else:
            self._selected = None  # 全部
        self.accept()

    # ------------------------------------------------------------------ #
    def selection(self) -> tuple | None:
        """返回 (start_idx, end_idx) 1-indexed；None = 全部章节。"""
        return self._selected

    # ------------------------------------------------------------------ #
    def quality(self) -> str:
        """返回选中的画质 key（最高→best / 1080p / 720p / ...）；非视频空串。"""
        if not hasattr(self, "quality_combo"):
            return ""
        txt = self.quality_combo.currentText()
        if txt == "最高" or txt == "best":
            return "best"
        return txt.lower()


def build_selection(total: int, selection: tuple | None) -> list[bool]:
    """把对话框返回的 selection 转成与章节等长的勾选列表（True=下载）。"""
    if total <= 0:
        return []
    if selection is None:
        return [True] * total
    s, e = selection
    s = max(1, min(total, s))
    e = max(s, min(total, e))
    return [s <= i <= e for i in range(1, total + 1)]
