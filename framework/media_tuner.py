"""通用媒体播放调优框架（media_tuner.py）。

按媒体类型自动选择网络缓冲/传输参数，并对播放卡顿做统计与自适应
恢复，不绑定具体播放器（VLC / mpv / ffplay 均可复用其策略）：

- classify(url) -> MediaProfile：从 URL/扩展名推断媒体类型
  （HLS / MP4 / DASH / 直播 / 未知），给出推荐网络缓存与上限。
- 卡顿统计（Buffering 窗口）：记录缓冲事件频率；连续 5s 内发生
  >= 3 次独立缓冲即判定"频繁卡顿"→ 建议重播并逐级提高缓存。
- 缓存阶梯：4000 → 6000 → 8000 ms，封顶后不再升级。

典型用法：
    tuner = MediaTuner()
    profile = tuner.classify(url)          # 初始缓冲参数
    tuner.on_buffering(percent)            # 播放器回调（0~100）
    if tuner.should_upgrade():
        ms = tuner.next_buffer_ms(url)     # 加大缓存（无则返回 None 封顶）
        player.restart_with_buffer(ms)     # 播放器侧重启当前流

线程安全：所有方法加锁，可在 libvlc/网络线程调用。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MediaProfile:
    """媒体类型画像：播放器据此选择初始缓冲/传输参数。"""

    kind: str            # hls / mp4 / dash / live / unknown
    buffer_ms: int       # 推荐初始网络缓存（毫秒）
    max_buffer_ms: int   # 自适应缓冲上限（毫秒）


# 类型识别（按 URL 扩展名/特征，全部小写匹配）
_HLS_HINTS = (".m3u8", "m3u8?", "hls", ".ts?")
_DASH_HINTS = (".mpd", "dash")
_LIVE_HINTS = ("live", "flv", ".ts?live")
_MP4_HINTS = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".flv", ".avi")


def _hint(url: str, hints) -> bool:
    low = (url or "").lower()
    return any(h in low for h in hints)


def classify(url: str) -> MediaProfile:
    """按 URL 推断媒体类型与初始缓冲参数。

    - HLS（m3u8）：分片流，网络抖动敏感 → 3500ms
    - DASH（mpd）：分片流，双流合并开销大 → 4000ms
    - 直播（live/flv 无扩展名）：低延迟优先 + 抗抖 → 2500ms
    - MP4/WebM 直链：渐进式下载，靠 Range 拖动 → 1500ms 即可
    - 未知：保守 2500ms
    """
    if _hint(url, _HLS_HINTS):
        return MediaProfile("hls", 3500, 8000)
    if _hint(url, _DASH_HINTS):
        return MediaProfile("dash", 4000, 8000)
    if _hint(url, _LIVE_HINTS):
        return MediaProfile("live", 2500, 6000)
    if _hint(url, _MP4_HINTS):
        return MediaProfile("mp4", 1500, 6000)
    return MediaProfile("unknown", 2500, 6000)


_BUFFER_STEPS = (4000, 6000, 8000)  # 卡顿升级阶梯（ms）


class MediaTuner:
    """卡顿统计 + 自适应缓冲升级（线程安全，通用）。

    实例按播放会话创建（每次 play 重置），避免跨媒体串扰统计。
    """

    def __init__(self, window_s: float = 5.0, threshold: int = 3):
        self._lock = threading.Lock()
        self._window = window_s           # 卡顿判定时间窗（秒）
        self._threshold = threshold       # 窗口内独立缓冲次数阈值
        self._events: list[float] = []    # 缓冲开始时间戳（单调时钟）
        self._last_percent = -1           # 上次缓冲百分比（判断独立缓冲）
        self._upgraded = 0                # 已升级次数（封顶）

    # ------------------------------------------------------------------ #
    def on_buffering(self, percent: int) -> None:
        """播放器缓冲回调（0~100）。

        缓冲开始（percent < 100）时记录时间戳；percent == 100 视为恢复。
        percent 持续 <100 且接近上次值只记一次（同一次缓冲不重复计数）。
        """
        with self._lock:
            try:
                p = int(percent)
            except (TypeError, ValueError):
                return
            if p >= 100:
                self._last_percent = 100
                return
            if self._last_percent < 0:
                # 首条缓冲事件（初始/重置后）必记
                self._events.append(time.monotonic())
            elif self._last_percent < 100:
                # 仍在缓冲中：百分比单调增长/持平是同一轮缓冲，只有明显
                # 回退（如 80→20，播放器重缓冲从低位重新爬）才算新一轮
                if p < self._last_percent - 10:
                    self._events.append(time.monotonic())
            else:
                # 上次已恢复（100）→ 新一轮缓冲
                self._events.append(time.monotonic())
            self._last_percent = p

    def stall_count(self) -> int:
        """时间窗内独立缓冲次数（用于 UI 提示，非升级判定）。"""
        with self._lock:
            cutoff = time.monotonic() - self._window
            self._events = [t for t in self._events if t > cutoff]
            return len(self._events)

    def should_upgrade(self) -> bool:
        """窗口内缓冲次数 >= 阈值 → 判定频繁卡顿，建议升级缓冲重播。"""
        return self.stall_count() >= self._threshold

    def next_buffer_ms(self, url: str) -> int | None:
        """按阶梯返回下一级缓存值；已达当前类型上限返回 None（封顶）。

        每次升级推进一级，播放器重启当前流后调用 next_buffer_ms 继续
        升级；连续升级不超过 3 次（_upgraded 封顶）。
        """
        profile = classify(url)
        with self._lock:
            self._events.clear()      # 升级后重置统计窗口
            if self._upgraded >= len(_BUFFER_STEPS):
                return None
            ms = _BUFFER_STEPS[self._upgraded]
            self._upgraded += 1
            if ms > profile.max_buffer_ms:
                return None
            return ms

    def reset(self) -> None:
        """切换媒体/新会话时重置统计。"""
        with self._lock:
            self._events.clear()
            self._last_percent = -1
            self._upgraded = 0
