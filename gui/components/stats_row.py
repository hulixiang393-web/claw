"""统计卡片区（StatsRow）。

显示：小说/漫画/视频源数、已启用、失效源（红点）。
数据来自 SourceManager。对应 ui-home.md「统计卡片区」。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from framework.source_manager import SourceManager


class _StatCard(QWidget):
    """单张统计卡片：大数字 + 标签。"""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.setObjectName("statsCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(2)

        self.value_label = QLabel("0")
        self.value_label.setObjectName("statsValue")
        self.value_label.setAlignment(Qt.AlignCenter)

        self.name_label = QLabel(label)
        self.name_label.setObjectName("statsLabel")
        self.name_label.setAlignment(Qt.AlignCenter)

        layout.addWidget(self.value_label)
        layout.addWidget(self.name_label)
        self.setFixedWidth(110)

    def set_value(self, value) -> None:
        self.value_label.setText(str(value))


class StatsRow(QWidget):
    """统计卡片行：小说/漫画/视频/启用/失效(红点)。"""

    def __init__(self, source_manager: SourceManager, parent=None):
        super().__init__(parent)
        self._manager = source_manager

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.card_novel = _StatCard("小说")
        self.card_comic = _StatCard("漫画")
        self.card_video = _StatCard("视频")
        self.card_enabled = _StatCard("已启用")
        self.card_broken = _StatCard("失效")

        for card in (
            self.card_novel,
            self.card_comic,
            self.card_video,
            self.card_enabled,
            self.card_broken,
        ):
            layout.addWidget(card)
        layout.addStretch(1)

    def reload(self) -> None:
        """从 SourceManager 重算统计。"""
        counts = self._manager.count_by_type()
        self.card_novel.set_value(counts.get("novel", 0))
        self.card_comic.set_value(counts.get("comic", 0))
        self.card_video.set_value(counts.get("video", 0))
        self.card_enabled.set_value(self._manager.count_enabled())
        broken = self._manager.count_broken()
        self.card_broken.set_value(broken)

    def set_broken_badge(self, count: int) -> None:
        """失效源红点角标。count>0 时显示红底数字。"""
        self.card_broken.set_value(count)
