"""标题组件（HoverTitle）。

卡片标题行：
- 默认：完整标题一行放得下时静态完整显示；放不下时单行省略号显示。
- 悬停：鼠标悬停在标题上时，放不下的标题自动横向连续滚动（跑马灯），
  移出标题恢复省略号。

实现要点：标题文字**始终自绘**（paintEvent），不依赖 QLabel 原生绘制 ——
调用点给标题设了很小的固定高度（如 setFixedHeight(20)，小于 13px 字体的
实际行高 22px），QLabel 原生绘制会把文字垂直裁剪成空白；自绘按 baseline
（ascent）起画，保证文字在固定高度内始终可见。

- 滚动速度 _MARQUEE_SPEED（像素/秒）；卡片隐藏/移出时停止滚动
- 颜色走 Qt palette Text 角色（对应调用点 QSS 的 `color: palette(text)`）
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import QLabel

# 跑马灯滚动速度（像素/秒）
_MARQUEE_SPEED = 40
# 跑马灯两轮之间的间隔（按当前字号换算宽度，方便阅读切分）
_MARQUEE_SEP = "    "
# 跑马灯重绘节拍（毫秒）
_MARQUEE_TIMER_MS = 33


class HoverTitle(QLabel):
    """标题组件：默认省略号，悬停超长时连续滚动（marquee）。"""

    def __init__(self, text: str = "", parent_card=None, parent=None):
        super().__init__(parent)
        self._full = text or ""
        self._card = None
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setWordWrap(False)
        # 始终自绘，不依赖 QLabel 原生绘制（避免固定高度裁剪成空白）
        self.setText("")

        self._display = ""  # 默认态要绘制的文本（完整 or 单行省略号）
        self._marquee_on = False
        self._offset = 0.0  # 当前滚动偏移（像素）
        self._loop_width = 0  # 一轮滚动的总宽度（全名 + 间隔）
        self._timer = QTimer(self)
        self._timer.setInterval(_MARQUEE_TIMER_MS)
        self._timer.timeout.connect(self._on_tick)

        self.setParentCard(parent_card)

    # ------------------------------------------------------------------ #
    def setParentCard(self, card) -> None:  # noqa: N802
        """绑定所属卡片（用于卡片宽度兜底，避免标题自身宽度尚未布局）。"""
        self._card = card

    def setFullText(self, text: str) -> None:  # noqa: N802
        """更新完整标题（重建宿主后复用卡片时调用）。"""
        self._full = text or ""
        self._stop_marquee()
        self._refresh()

    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        """刷新默认态显示文本：放得下全名，放不下单行省略号。"""
        if self._marquee_on:
            return  # 滚动中不打断，等移出标题时恢复
        self._display = self._compute_display()
        self.update()

    def _compute_display(self) -> str:
        """按当前宽度计算默认态显示文本（完整 or 省略号）。"""
        if not self._full:
            return ""
        w = self._avail_width()
        if w <= 0:
            return self._full  # 宽度未定，先显示完整，resize 后再算
        if self.fontMetrics().horizontalAdvance(self._full) <= w:
            return self._full
        return self.fontMetrics().elidedText(self._full, Qt.ElideRight, w)

    def _need_marquee(self) -> bool:
        """单行放不下完整标题才需要滚动。"""
        if not self._full:
            return False
        w = self._avail_width()
        if w <= 0:
            return False
        return self.fontMetrics().horizontalAdvance(self._full) > w

    def _start_marquee(self) -> None:
        """启动连续滚动：记录一轮宽度，开定时器推进偏移。"""
        if self._marquee_on or not self._full:
            return
        fm = self.fontMetrics()
        self._loop_width = fm.horizontalAdvance(self._full) + fm.horizontalAdvance(
            _MARQUEE_SEP
        )
        if self._loop_width <= 0:
            return
        self._marquee_on = True
        self._offset = 0.0
        self._timer.start()
        self.update()

    def _stop_marquee(self) -> None:
        """停止滚动并恢复默认省略号/全名显示。"""
        if not self._marquee_on:
            return
        self._marquee_on = False
        self._timer.stop()
        self._refresh()

    def _on_tick(self) -> None:
        """推进滚动偏移并重绘（loop 回绕，无缝衔接）。"""
        self._offset += _MARQUEE_SPEED * (_MARQUEE_TIMER_MS / 1000.0)
        if self._offset >= self._loop_width:
            self._offset -= self._loop_width
        self.update()

    # ------------------------------------------------------------------ #
    def paintEvent(self, event):  # noqa: N802
        """始终自绘：默认态画省略号/全名，滚动态画两段文本无缝 loop。

        按 baseline（ascent）起画，垂直不居中偏移（固定高度偏小时保证
        中文主体完整可见，只裁掉英文 descent 区）。
        """
        if not self._marquee_on and not self._display:
            return
        p = QPainter(self)
        p.setClipRect(self.rect())
        fm = self.fontMetrics()
        p.setFont(self.font())
        p.setPen(self.palette().color(QPalette.Text))
        y = fm.ascent()
        if self._marquee_on:
            off = -int(round(self._offset))
            p.drawText(off, y, self._full)
            p.drawText(off + self._loop_width, y, self._full)
        else:
            p.drawText(0, y, self._display)
        p.end()

    def _avail_width(self) -> int:
        if self.width() > 0:
            return self.width()
        card = self._card
        if card is not None and card.width() > 0:
            return max(10, card.width() - self._side_margin() * 2)
        return 0

    def _side_margin(self) -> int:
        """卡片内容左右边距（宽度兜底时对称留同样的缩进）。"""
        card = self._card
        if card is not None and card.layout() is not None:
            m = card.layout().contentsMargins()
            return max(4, (m.left() + m.right()) // 2)
        return 8

    # ------------------------------------------------------------------ #
    def enterEvent(self, event):  # noqa: N802
        super().enterEvent(event)
        if self._need_marquee():
            self._start_marquee()

    def leaveEvent(self, event):  # noqa: N802
        super().leaveEvent(event)
        self._stop_marquee()

    # ------------------------------------------------------------------ #
    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._refresh()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._refresh()
        if self._marquee_on:
            self._timer.start()

    def hideEvent(self, event):  # noqa: N802
        # 隐藏（滚出视口等）时停止滚动并恢复默认省略号显示
        self._stop_marquee()
        super().hideEvent(event)
