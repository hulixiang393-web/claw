"""VLC 内嵌播放器（vlc_player.py）。

基于 python-vlc（libVLC）内嵌到 Qt QWidget，通用支持：
- HLS（m3u8）/ DASH（mpd 双流 input-slave）/ MP4 / WebM / yt-dlp 直链
- DASH 音视频分离（B站）：video_url + audio_url 用 input-slave 挂音频轨

关键要点：
- 模块级单例 vlc.Instance（重型对象，多播放器共享）。
- 内嵌时序：widget 强制 WA_NativeWindow，showEvent 后 winId()+set_hwnd；
  QStackedWidget 切页 / 全屏 reparent 后 HWND 变化必须重挂。
- libvlc 回调在 libvlc 线程，绝不直接碰 Qt → 经 _VlcBridge 信号 queued 到主线程。
- 释放：先各 player.close()，App 退出再 shutdown_vlc()。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QObject, Signal

_INSTANCE = None  # 模块级单例 vlc.Instance


def _ensure_vlc_on_path() -> None:
    """libvlc 找不到时，把 VLC 安装目录加进 PATH 兜底。"""
    for base in (
        r"C:\Program Files\VideoLAN\VLC",
        r"C:\Program Files (x86)\VideoLAN\VLC",
    ):
        if os.path.isfile(os.path.join(base, "libvlc.dll")):
            if base not in os.environ.get("PATH", ""):
                os.environ["PATH"] = base + os.pathsep + os.environ.get("PATH", "")
            break


def _get_instance():
    """模块级单例 vlc.Instance（含内嵌去标题、网络缓冲选项）。"""
    global _INSTANCE
    if _INSTANCE is None:
        _ensure_vlc_on_path()
        import vlc

        # --no-video-title-show：内嵌时不显示 VLC 标题条
        # --network-caching=2000：HLS 网络流缓冲（防频繁卡顿）
        _INSTANCE = vlc.Instance([
            "--no-video-title-show",
            "--network-caching=2000",
        ])
    return _INSTANCE


def shutdown_vlc() -> None:
    """App 退出时释放共享实例（必须先 close 所有 player）。"""
    global _INSTANCE
    if _INSTANCE is not None:
        try:
            _INSTANCE.release()
        except Exception:  # noqa: BLE001
            pass
        _INSTANCE = None


class _VlcBridge(QObject):
    """libvlc 回调线程 → Qt 主线程的信号桥（QObject 信号跨线程安全）。"""

    ended = Signal()
    error = Signal(str)
    time_changed = Signal(int)
    length_changed = Signal(int)


class VlcPlayer(QObject):
    """VLC 内嵌播放器（可内嵌到任意 QWidget）。"""

    # 供 VideoView 连接的信号（由 _bridge 转发，libvlc 线程安全）
    ended = Signal()
    error = Signal(str)
    time_changed = Signal(int)
    length_changed = Signal(int)

    def __init__(self, video_widget=None, referer: str = "", user_agent: str = "",
                 parent=None):
        super().__init__(parent)
        self._video_widget = video_widget
        self._referer = referer
        self._user_agent = user_agent
        import vlc

        self._player = vlc.MediaPlayer(_get_instance())
        self._media = None
        self._current_url = ""
        self._attached = False
        # 事件桥接（持引用防 GC，回调只 emit 信号）
        self._bridge = _VlcBridge()
        self._bridge.ended.connect(self.ended)
        self._bridge.error.connect(self.error)
        self._bridge.time_changed.connect(self.time_changed)
        self._bridge.length_changed.connect(self.length_changed)
        self._install_events()
        # 构造即强制 native 窗口（winId 返回真实 HWND）
        if video_widget is not None:
            video_widget.setAttribute(Qt.WA_NativeWindow, True)
            self._attach_window()

    # ------------------------------------------------------------------ #
    # 内嵌
    # ------------------------------------------------------------------ #
    def set_window(self, widget) -> None:
        """换内嵌目标（全屏切回后重挂）。"""
        self._video_widget = widget
        if widget is not None:
            widget.setAttribute(Qt.WA_NativeWindow, True)
        self._attach_window()

    def _attach_window(self) -> None:
        """winId()+set_hwnd 挂内嵌窗口。每次可见后都应重挂（HWND 可能变化）。"""
        w = self._video_widget
        if w is None:
            return
        try:
            hwnd = int(w.winId())
        except Exception:  # noqa: BLE001
            return
        if hwnd:
            try:
                self._player.set_hwnd(hwnd)
                self._attached = True
            except Exception:  # noqa: BLE001
                self._attached = False

    def rehook(self) -> None:
        """showEvent / 切页 / 全屏后重挂内嵌窗口（VideoView 在 showEvent 里调用）。"""
        self._attach_window()

    # ------------------------------------------------------------------ #
    # 播放控制
    # ------------------------------------------------------------------ #
    def play(self, video_url: str, audio_url: str = "", title: str = "") -> None:
        """播放。单流直接 set_mrl；DASH 双流用 input-slave 挂音频轨。"""
        if not video_url:
            self._bridge.error.emit("无播放地址")
            return
        self.stop()
        self._current_url = video_url
        opts = []
        if self._referer:
            opts.append("http-referrer=" + self._referer)
        if self._user_agent:
            opts.append("http-user-agent=" + self._user_agent)

        try:
            if audio_url:
                # DASH 音视频双流：input-slave 必须 media 级、play 之前
                self._media = _get_instance().media_new(video_url)
                self._media.add_option("input-slave=" + audio_url)
                for o in opts:
                    self._media.add_option(o)
                self._player.set_media(self._media)
            else:
                # HLS/MP4/单流
                self._player.set_mrl(video_url, *opts)
            self._attach_window()
            self._player.play()
        except Exception as exc:  # noqa: BLE001
            self._bridge.error.emit(f"VLC 播放失败：{exc}")

    def pause(self) -> None:
        try:
            self._player.pause()
        except Exception:  # noqa: BLE001
            pass

    def resume(self) -> None:
        try:
            self._player.set_pause(0)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        try:
            self._player.stop()
        except Exception:  # noqa: BLE001
            pass

    def set_volume(self, v: int) -> None:
        try:
            self._player.audio_set_volume(int(max(0, min(100, v))))
        except Exception:  # noqa: BLE001
            pass

    def set_rate(self, rate: float) -> None:
        """倍速（0.5~3.0）。"""
        try:
            self._player.set_rate(float(max(0.25, min(4.0, rate))))
        except Exception:  # noqa: BLE001
            pass

    def set_position(self, p: float) -> None:
        """0..1 跳转。"""
        try:
            self._player.set_position(max(0.0, min(1.0, float(p))))
        except Exception:  # noqa: BLE001
            pass

    def get_position(self) -> float:
        try:
            return self._player.get_position() or 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def get_length(self) -> int:
        try:
            return int(self._player.get_length() or 0)
        except Exception:  # noqa: BLE001
            return 0

    def get_time(self) -> int:
        try:
            return int(self._player.get_time() or 0)
        except Exception:  # noqa: BLE001
            return 0

    def is_playing(self) -> bool:
        try:
            return bool(self._player.is_playing())
        except Exception:  # noqa: BLE001
            return False

    def get_state(self) -> int:
        try:
            return int(self._player.get_state())
        except Exception:  # noqa: BLE001
            return -1

    def close(self) -> None:
        """释放本播放器（App 退出前逐个调用；实例由 shutdown_vlc 最后释放）。"""
        try:
            self._player.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._player.release()
        except Exception:  # noqa: BLE001
            pass
        self._player = None

    # ------------------------------------------------------------------ #
    # 事件监听（libvlc 线程回调 → 桥接信号）
    # ------------------------------------------------------------------ #
    def _install_events(self) -> None:
        import vlc

        em = self._player.event_manager()
        em.event_attach(vlc.EventType.MediaPlayerEndReached, self._cb_ended)
        em.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._cb_error)
        em.event_attach(vlc.EventType.MediaPlayerTimeChanged, self._cb_time)
        em.event_attach(vlc.EventType.MediaPlayerLengthChanged, self._cb_length)

    def _cb_ended(self, event):
        self._bridge.ended.emit()

    def _cb_error(self, event):
        self._bridge.error.emit("VLC 播放出错")

    def _cb_time(self, event):
        try:
            self._bridge.time_changed.emit(int(event.u.new_time))
        except Exception:  # noqa: BLE001
            pass

    def _cb_length(self, event):
        try:
            self._bridge.length_changed.emit(int(event.u.new_length))
        except Exception:  # noqa: BLE001
            pass
