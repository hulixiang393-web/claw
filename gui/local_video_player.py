# -*- coding: utf-8 -*-
"""本地视频内嵌播放窗口（libvlc 嵌入 App 窗口内）。

替代原来的 `framework.external_player.open_with_player`：书架里点本地视频
不再弹第二个播放器进程的窗口，画面直接在本窗口里。

设计要点
--------
- **全屏不 reparent**：`showFullScreen()` 让本窗口自己全屏，播放器挂载的
  HWND 全程不变。VideoView 里那种把画面控件搬到全屏窗的做法会换 HWND，
  必须重挂 set_hwnd，本地播放没有那个必要，少一处会黑屏的地方。
- **进度两个驱动源**：`time_changed` 事件为主，1s 定时读兜底（部分容器
  不发 TimeChanged）。回写节流 2s，避免高频写 progress.json。
- **续播**：拿到时长后再 seek（此时才能算比例）；落在片尾 1s 内则从头播。
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

# 进度回写节流（秒）：与 VideoView.POSITION_SAVE_INTERVAL_S 保持一致
SAVE_INTERVAL_S = 2.0
# 进度兜底轮询（毫秒）
POLL_MS = 1000


def _fmt_hms(seconds: float) -> str:
    total = max(0, int(seconds or 0))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class LocalVideoPlayer(QDialog):
    """单文件本地视频的内嵌播放窗口。

    回调约定（都由主线程触发）：
    - `on_progress(seconds, duration, position)`：已节流的播放进度
    - `on_handoff()`：真正开始播放那一刻（用于把「最近阅读」落盘一次）
    - `on_finished()`：播放自然结束（调用方决定是否自动下一集）
    """

    def __init__(self, parent=None, *, on_progress=None, on_handoff=None,
                 on_finished=None, title: str = ""):
        # 父级必须是 QWidget（QDialog 的硬约束）：调用方传入非控件对象
        # （例如测试里的轻量窗口替身）时退化为无父窗口，而不是抛 TypeError
        # 把整条播放链路带崩。
        if parent is not None and not isinstance(parent, QWidget):
            parent = None
        super().__init__(parent)
        self._on_progress = on_progress
        self._on_handoff = on_handoff
        self._on_finished = on_finished
        self._player = None
        self._path = ""
        self._length_ms = 0
        self._pending_seek_s = None
        self._last_save_ts = 0.0
        self._handed_off = False
        self._is_fs = False

        self.setWindowTitle(title or "播放")
        self.resize(960, 600)
        self.setStyleSheet("background: #000;")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 画面区：必须是原生窗口，libvlc 靠 set_hwnd(winId()) 渲染
        self.video = QWidget(self)
        self.video.setAttribute(Qt.WA_NativeWindow, True)
        self.video.setStyleSheet("background: #000;")
        self.video.setFocusPolicy(Qt.StrongFocus)
        lay.addWidget(self.video, stretch=1)

        # 控制条
        bar = QWidget(self)
        bar.setStyleSheet("background: rgba(0,0,0,180);")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 6, 10, 6)
        bl.setSpacing(8)
        self.play_btn = self._btn("▶", "播放 / 暂停（空格）", self.toggle_play)
        bl.addWidget(self.play_btn)
        self.progress = QSlider(Qt.Horizontal)
        self.progress.setRange(0, 1000)
        self.progress.setStyleSheet(
            "QSlider::groove:horizontal { height: 4px; background: #555;"
            " border-radius: 2px; }"
            "QSlider::sub-page:horizontal { background: #4da3ff;"
            " border-radius: 2px; }"
            "QSlider::handle:horizontal { width: 12px; margin: -4px 0;"
            " background: #4da3ff; border-radius: 6px; }"
        )
        self.progress.sliderReleased.connect(self._on_seek_released)
        bl.addWidget(self.progress, stretch=1)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet("color: #ddd; font-size: 11px;")
        bl.addWidget(self.time_label)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(80)
        self.vol.setFixedWidth(80)
        self.vol.valueChanged.connect(self._on_volume)
        bl.addWidget(self.vol)
        self.fs_btn = self._btn("⛶", "全屏（F）", self.toggle_fullscreen, 32)
        bl.addWidget(self.fs_btn)
        self.close_btn = self._btn("✕", "关闭（Esc）", self.close, 32)
        bl.addWidget(self.close_btn)
        lay.addWidget(bar)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

    # ------------------------------------------------------------------ #
    def _btn(self, text: str, tip: str, slot, width: int = 36) -> QPushButton:
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setFixedWidth(width)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            "QPushButton { background: transparent; color: #eee; border: none;"
            " font-size: 14px; padding: 2px; }"
            "QPushButton:hover { background: rgba(255,255,255,40);"
            " border-radius: 4px; }"
        )
        b.clicked.connect(slot)
        return b

    # ------------------------------------------------------------------ #
    def play(self, path: str, start_seconds: float | None = None) -> None:
        """播放本地文件；`start_seconds` 为续播位置。"""
        from framework.vlc_player import VlcPlayer

        self._path = path or ""
        self._pending_seek_s = None
        if isinstance(start_seconds, (int, float)) and start_seconds > 0:
            self._pending_seek_s = float(start_seconds)
        self._length_ms = 0
        self._last_save_ts = 0.0
        self._handed_off = False
        self.progress.setValue(0)
        self.time_label.setText("00:00 / 00:00")

        if self._player is None:
            self._player = VlcPlayer(self.video)
            self._player.time_changed.connect(self._on_time)
            self._player.length_changed.connect(self._on_length)
            self._player.ended.connect(self._on_ended)
            self._player.error.connect(self._on_error)
        try:
            self._player.rehook()
        except Exception:  # noqa: BLE001 —— HWND 未就绪：play 里会再挂
            pass
        self._player.play(self._path)
        self._apply_volume()
        self._timer.start()
        self.play_btn.setText("⏸")
        self._handoff()

    def toggle_play(self) -> None:
        if self._player is None:
            return
        try:
            if self._player.is_playing():
                self._player.pause()
                self.play_btn.setText("▶")
            else:
                self._player.resume()
                self.play_btn.setText("⏸")
        except Exception:  # noqa: BLE001 —— 播放器释放竞态
            pass

    def toggle_fullscreen(self) -> None:
        """窗口自身全屏：不 reparent，HWND 不变，避免黑屏。"""
        if self._is_fs:
            self.showNormal()
            self._is_fs = False
            self.fs_btn.setText("⛶")
        else:
            self.showFullScreen()
            self._is_fs = True
            self.fs_btn.setText("⛶")
        self.video.setFocus()

    # ------------------------------------------------------------------ #
    def _apply_volume(self) -> None:
        if self._player is not None:
            try:
                self._player.set_volume(int(self.vol.value()))
            except Exception:  # noqa: BLE001
                pass

    def _on_volume(self, value: int) -> None:
        self._apply_volume()

    def _on_length(self, length_ms: int) -> None:
        try:
            self._length_ms = int(length_ms or 0)
        except (TypeError, ValueError):
            self._length_ms = 0
        target = self._pending_seek_s
        if self._length_ms <= 0 or not target:
            return
        self._pending_seek_s = None
        duration_s = self._length_ms / 1000.0
        if target >= duration_s - 1.0:
            return  # 已到片尾：从头播比跳到末尾合理
        try:
            if self._player is not None:
                self._player.set_position(target / duration_s)
        except Exception:  # noqa: BLE001 —— seek 失败不影响播放
            pass

    def _on_time(self, time_ms: int) -> None:
        self._report(time_ms)

    def _poll(self) -> None:
        """兜底轮询：个别容器不发 TimeChanged。"""
        player = self._player
        if player is None:
            return
        try:
            if not player.is_playing():
                return
            time_ms = int(player.get_time() or 0)
            length_ms = int(player.get_length() or 0)
        except Exception:  # noqa: BLE001
            return
        if length_ms > 0 and length_ms != self._length_ms:
            self._on_length(length_ms)
        if time_ms > 0:
            self._report(time_ms)

    def _report(self, time_ms: int) -> None:
        length_ms = self._length_ms
        if length_ms <= 0 and self._player is not None:
            try:
                length_ms = int(self._player.get_length() or 0)
            except Exception:  # noqa: BLE001
                length_ms = 0
            if length_ms > 0:
                self._length_ms = length_ms
        if length_ms <= 0 or not (0 <= time_ms <= length_ms):
            return
        seconds = time_ms / 1000.0
        duration = length_ms / 1000.0
        if not self.progress.isSliderDown():
            self.progress.blockSignals(True)
            self.progress.setValue(int(time_ms * 1000 / length_ms))
            self.progress.blockSignals(False)
        self.time_label.setText(f"{_fmt_hms(seconds)} / {_fmt_hms(duration)}")
        if self._on_progress is None:
            return
        now = time.monotonic()
        if now - self._last_save_ts < SAVE_INTERVAL_S:
            return
        self._last_save_ts = now
        try:
            self._on_progress(seconds, duration, seconds / duration)
        except Exception:  # noqa: BLE001 —— 回写失败不能影响播放
            pass

    def _on_seek_released(self) -> None:
        if self._player is None or self._length_ms <= 0:
            return
        ratio = max(0.0, min(1.0, self.progress.value() / 1000.0))
        try:
            self._player.set_position(ratio)
        except Exception:  # noqa: BLE001
            pass

    def _on_ended(self) -> None:
        self._timer.stop()
        self.play_btn.setText("▶")
        if self._on_finished is not None:
            try:
                self._on_finished()
            except Exception:  # noqa: BLE001
                pass

    def _on_error(self, message: str) -> None:
        self.time_label.setText("播放出错")
        self._timer.stop()

    def _handoff(self) -> None:
        """真正开始播放：让调用方落一次「最近阅读」。"""
        if self._handed_off or self._on_handoff is None:
            return
        self._handed_off = True
        try:
            self._on_handoff()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    def keyPressEvent(self, event):  # noqa: N802
        key = event.key()
        if key == Qt.Key_Space:
            self.toggle_play()
        elif key == Qt.Key_Left and self._player is not None:
            self._player.seek_relative(-10)
        elif key == Qt.Key_Right and self._player is not None:
            self._player.seek_relative(10)
        elif key == Qt.Key_Up:
            self.vol.setValue(min(100, self.vol.value() + 10))
        elif key == Qt.Key_Down:
            self.vol.setValue(max(0, self.vol.value() - 10))
        elif key == Qt.Key_F:
            self.toggle_fullscreen()
        elif key == Qt.Key_Escape:
            if self._is_fs:
                self.toggle_fullscreen()
            else:
                self.close()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def closeEvent(self, event):  # noqa: N802
        self._timer.stop()
        if self._player is not None:
            try:
                self._player.close()
            except Exception:  # noqa: BLE001
                pass
            self._player = None
        super().closeEvent(event)


def finite_seconds(value) -> float | None:
    """把任意输入收敛成「有限正秒数」，否则 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)
