"""空状态（EmptyState）。

无源时显示：Q 版插画（占位绘制）+ 二次元台词 + 「去添加源」按钮。
对应 ui-home.md「空状态」。插画成品资源第二阶段替换。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPen, QBrush
from PySide6.QtWidgets import (
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QFrame,
)


class EmptyState(QWidget):
    """空状态：Q版占位 + 台词 + 按钮。"""

    add_source_clicked = Signal()  # → 切 Tab 到源管理

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(16)

        # 占位 Q 版角色（简单圆形，后续替换为插画）
        illustration = QFrame()
        illustration.setFixedSize(120, 120)
        illustration.setStyleSheet(
            "border-radius: 60px; background: #FFEEF4; border: 2px solid #FFD6E0;"
        )
        layout.addWidget(illustration, alignment=Qt.AlignCenter)

        quote = QLabel('"还没有源哦，要不要找一只？"')
        quote.setStyleSheet("color: #7D6B7A; font-size: 15px; font-style: italic;")
        quote.setAlignment(Qt.AlignCenter)
        layout.addWidget(quote)

        btn = QPushButton("去添加源")
        btn.setFixedWidth(140)
        btn.clicked.connect(self.add_source_clicked.emit)
        layout.addWidget(btn, alignment=Qt.AlignCenter)
