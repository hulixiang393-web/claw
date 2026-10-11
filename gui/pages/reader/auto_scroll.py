"""自动滚动公共实现（novel/comic 收敛为同一状态机）。

此前小说/漫画两个阅读视图各自实现了一套几乎相同的自动滚动机械逻辑：
按钮文字切换、速度滑块、定时器驱动、启动/停止状态机、到底自动停止、
用户拖动滚动条时停止。差异仅剩「每帧推进多少像素」：
- 小说：固定步进（档位 × 5px / 35ms tick）；
- 漫画：按真实经过时间 × 速度曲线（16ms 精确帧驱动）。

本模块收敛全部机械逻辑为单一实现，把「推进量」保留为视图各自实现的
`_auto_scroll_step(dt)` 钩子，确保各自默认滚动行为不变。

视图侧需要提供：
- 控件：self.auto_scroll_btn / self.auto_scroll_speed_slider
- 状态：self._auto_scrolling、self._auto_pos、self._auto_last_tick、self._auto_timer
- 模式判定：_auto_scroll_allowed()（默认 True，novel/comic 各自按 _mode 判定）
- 推进量：_auto_scroll_step(dt) -> float（px）
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer

_IDLE_SUFFIX = "自动滚动"
_ACTIVE_SUFFIX = "停止"


class AutoScrollMixin:
    """自动滚动公共状态机。"""

    AUTO_SCROLL_TICK_MS = 35
    AUTO_SCROLL_PRECISE = False

    def _build_auto_scroll_timer(self) -> QTimer:
        """创建并连接自动滚动定时器（视图 __init__ 中调用）。"""
        self._auto_scrolling = False
        self._auto_pos = 0.0
        self._auto_last_tick = 0.0
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(self.AUTO_SCROLL_TICK_MS)
        if self.AUTO_SCROLL_PRECISE:
            self._auto_timer.setTimerType(Qt.PreciseTimer)
        self._auto_timer.timeout.connect(self._auto_scroll_tick)
        return self._auto_timer

    def _auto_scroll_allowed(self) -> bool:
        """当前模式是否允许自动滚动（各视图按 self._mode 判定）。"""
        return True

    def _set_auto_scroll_btn(self, active: bool) -> None:
        """把按钮文字在「前缀+自动滚动」/「前缀+停止」之间切换。

        前缀（如 ▶）由各视图创建按钮时确定，这里只替换后缀，保证文字与
        原实现逐字一致。
        """
        text = self.auto_scroll_btn.text()
        if active:
            base = (
                text[: -len(_IDLE_SUFFIX)]
                if text.endswith(_IDLE_SUFFIX)
                else text.rstrip()
            )
            self.auto_scroll_btn.setText(base + (" " if base else "") + _ACTIVE_SUFFIX)
        else:
            base = (
                text[: -len(_ACTIVE_SUFFIX)]
                if text.endswith(_ACTIVE_SUFFIX)
                else text
            )
            self.auto_scroll_btn.setText(base.rstrip() + " " + _IDLE_SUFFIX)

    def _toggle_auto_scroll(self) -> None:
        """切换自动滚动：停止/启动，按钮文字同步更新。"""
        if self._auto_scrolling:
            self._stop_auto_scroll()
            return
        if not self._auto_scroll_allowed():
            return
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() <= 0:
            return
        self._auto_scrolling = True
        self._auto_pos = float(vbar.value())
        self._auto_last_tick = time.monotonic()
        self._set_auto_scroll_btn(True)
        self._auto_timer.start()

    def _stop_auto_scroll(self) -> None:
        """停止自动滚动并复位按钮文字。"""
        if not self._auto_scrolling:
            return
        self._auto_scrolling = False
        self._auto_timer.stop()
        self._set_auto_scroll_btn(False)

    def _update_auto_scroll_slider_state(self) -> None:
        """根据当前模式启用/禁用自动滚动速度滑块。

        只按模式判定：漫画懒加载分批渲染时内容高度可能为 0，若按
        maximum>0 判定会把滑块误禁用且不再刷新，画廊模式下始终可拖。
        """
        self.auto_scroll_speed_slider.setEnabled(self._auto_scroll_allowed())

    def _auto_scroll_tick(self) -> None:
        """推进自动滚动：按 _auto_scroll_step 的增量向下滚动，到底自动停止。"""
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() <= 0:
            self._stop_auto_scroll()
            return
        now = time.monotonic()
        dt = now - self._auto_last_tick
        self._auto_last_tick = now
        if dt > 0.1:  # 长时间挂起（切后台/卡顿恢复）不一次性猛跳
            dt = self.AUTO_SCROLL_TICK_MS / 1000.0
        self._auto_pos += self._auto_scroll_step(dt)
        if self._auto_pos >= vbar.maximum():
            vbar.setValue(vbar.maximum())
            self._stop_auto_scroll()
            return
        vbar.setValue(int(self._auto_pos))

    def _auto_scroll_step(self, dt: float) -> float:
        """每帧推进量（px）。novel/comic 各自实现。"""
        raise NotImplementedError