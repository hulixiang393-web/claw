"""最近搜索（RecentSearches）。

最多 20 条，点击一键重搜（信号 → 切 Tab 搜）。
对应 PROJECT_GUIDE.md §6.2「最近搜索」。
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


class _ClickableRow(QWidget):
    """可点击词条行：覆写 mousePressEvent 触发重搜。

    实例属性赋值（container.mousePressEvent = lambda ...）在 PySide6 里
    不进 Qt 虚表，事件分发永不回调 → 词条空白区点击不生效。
    改类方法覆写才可靠（与 video_view.py 同坑注释一致）。
    """

    clicked = Signal(str)

    def __init__(self, word: str, parent=None):
        super().__init__(parent)
        self._word = word

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._word)
        super().mousePressEvent(event)


class RecentSearches(QWidget):
    """最近搜索列表（最近 20 条）。"""

    search_clicked = Signal(str)  # 点击词条时发信号，GUI 层接
    changed = Signal()

    def __init__(self, history: SearchHistory, parent=None):
        super().__init__(parent)
        self._history = history

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        title_row = QHBoxLayout()
        title = QLabel("最近搜索")
        title.setObjectName("statsLabel")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.clear_btn = QPushButton("清空")
        self.clear_btn.setFixedHeight(26)
        self.clear_btn.clicked.connect(self._clear_history)
        title_row.addWidget(self.clear_btn)
        outer.addLayout(title_row)

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

            delete_btn = QPushButton("删除")
            delete_btn.setFixedWidth(50)
            delete_btn.setFixedHeight(26)
            delete_btn.clicked.connect(lambda _, w=word: self._remove_word(w))
            row.addWidget(delete_btn, alignment=Qt.AlignRight)

            container = _ClickableRow(word)
            container.setLayout(row)
            container.setStyleSheet("border-radius: 6px; padding: 2px 6px;")
            container.clicked.connect(self.search_clicked.emit)
            container.setCursor(Qt.PointingHandCursor)
            self._list_layout.addWidget(container)

        self._list_layout.addStretch(1)

    def _remove_word(self, word: str) -> None:
        if self._history.remove(word):
            self.reload()
            self.changed.emit()

    def _clear_history(self) -> None:
        if not self._history.recent():
            return
        self._history.clear()
        self.reload()
        self.changed.emit()
