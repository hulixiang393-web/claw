"""HoverTitle 跑马灯滚动行为测试。

标题一行放得下 → 静态完整显示；放不下 → 默认省略号，悬停（enter）才连续
滚动、移出（leave）恢复省略号；空标题 → 不滚动不显示。
组件始终自绘（_display 存默认态文本），不依赖 QLabel.text()。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtGui import QEnterEvent  # noqa: E402


def _title(text, width=200):
    from gui.components.hover_title import HoverTitle

    t = HoverTitle(text)
    t.setFixedWidth(width)
    t.setFixedHeight(20)
    t.show()
    return t


def _hover(t):
    from PySide6.QtCore import QPointF

    ev = QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1))
    t.enterEvent(ev)


def _leave(t):
    from PySide6.QtCore import QEvent

    ev = QEvent(QEvent.Leave)
    t.leaveEvent(ev)


def test_fits_shows_full_text(_qapp):
    """放得下：静态显示完整标题。"""
    t = _title("短标题")
    assert t._display == "短标题"
    assert t._marquee_on is False


def test_overflow_shows_ellipsis_by_default(_qapp):
    """放不下：默认省略号显示（不是空白）。"""
    long = "这是一个超级长的标题用于测试跑马灯滚动行为" * 3
    t = _title(long, width=120)
    assert t._display != ""  # 有内容，非空白
    assert "…" in t._display or len(t._display) < len(long)
    assert t._marquee_on is False


def test_hover_starts_marquee(_qapp):
    """悬停后：放不下的标题开始滚动。"""
    long = "这是一个超级长的标题用于测试跑马灯滚动行为" * 3
    t = _title(long, width=120)
    _hover(t)
    assert t._marquee_on is True
    assert t._timer.isActive()


def test_leave_stops_marquee(_qapp):
    """移出后：停止滚动并恢复省略号。"""
    long = "这是一个超级长的标题用于测试跑马灯滚动行为" * 3
    t = _title(long, width=120)
    _hover(t)
    assert t._marquee_on is True
    _leave(t)
    assert t._marquee_on is False
    assert not t._timer.isActive()
    assert t._display != ""  # 恢复省略号，非空白


def test_short_title_hover_no_marquee(_qapp):
    """放得下的短标题：悬停也不滚动。"""
    t = _title("短标题")
    _hover(t)
    assert t._marquee_on is False


def test_marquee_advances_offset(_qapp):
    """定时器 tick 会推进滚动偏移（文字在动）。"""
    long = "这是一个超级长的标题用于测试跑马灯滚动行为" * 3
    t = _title(long, width=120)
    _hover(t)
    off0 = t._offset
    t._on_tick()
    t._on_tick()
    assert t._offset != off0
    assert 0 <= t._offset < t._loop_width


def test_empty_no_marquee(_qapp):
    """空标题：不显示、不滚动。"""
    t = _title("")
    assert t._display == ""
    assert t._marquee_on is False
    _hover(t)
    assert t._marquee_on is False
