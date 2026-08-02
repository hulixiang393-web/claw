"""最近搜索（RecentSearches）。

最多 20 条，点击一键重搜（信号 → 切 Tab 搜）。
对应 ui-home.md「最近搜索」。
"""

from __future__ import annotations

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from framework.search_history import SearchHistory


class RecentSearches(QWidget):
    """最近搜索列表（最近 20 条）。"""

    search_clicked = Signal(str)  # 点击词条时发信号，GUI 层接

    def __init__(self, history: SearchHistory, parent=None):
        super().__init__(parent)
        self._history = history

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        title = QLabel("最近搜索")
        title.setObjectName("statsLabel")
        outer.addWidget(title)

        self._list_widget = QWidget()
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._list_widget)
        scroll.setFixedHeight(160)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

    def reload(self) -> None:
        """从 SearchHistory 重渲染。"""
        # 清旧
        while self._list_layout.count():
            child = self._list_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        items = self._history.recent()
        if not items:
            lbl = QLabel("  暂无搜索记录")
            lbl.setObjectName("statsLabel")
            self._list_layout.addWidget(lbl)
            return

        for word in items:
            row = QHBoxLayout()
            lbl = QLabel(f"  {word}")
            lbl.setObjectName("statsLabel")
            lbl.setStyleSheet("padding: 3px 0;")
            row.addWidget(lbl, stretch=1)

            btn = QPushButton("重搜")
            btn.setFixedWidth(50)
            btn.setFixedHeight(26)
            btn.clicked.connect(lambda _, w=word: self.search_clicked.emit(w))
            row.addWidget(btn, alignment=Qt.AlignRight)

            container = QWidget()
            container.setLayout(row)
            container.setStyleSheet("border-radius: 6px; padding: 2px 6px;")
            container.mousePressEvent = lambda _, w=word: self.search_clicked.emit(w)
            container.setCursor(Qt.PointingHandCursor)
            self._list_layout.addWidget(container)

        self._list_layout.addStretch(1)
