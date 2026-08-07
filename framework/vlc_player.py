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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin

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


class _ProxyHandler(BaseHTTPRequestHandler):
    """本地代理请求处理：把 VLC 的本地请求转发到真实 HLS 服务器。

    URL 映射：`/p/<绝对url>` → 请求真实 URL；m3u8 响应里重写分片
    相对路径为本地代理路径（VLC 只连 127.0.0.1，不再碰目标站 TLS）。
    """

    def do_GET(self):  # noqa: N802
        t = self.server.target
        path = self.path
        if path.startswith("/p/"):
            real = path[3:]
        else:
            real = urljoin(t["base"], path.lstrip("/"))
        import requests

        try:
            r = requests.get(real, headers=t["headers"], timeout=20)
        except Exception:  # noqa: BLE001
            self.send_response(502)
            self.end_headers()
            return
        if r.status_code != 200:
            self.send_response(r.status_code)
            self.end_headers()
            return
        body = r.content
        ctype = r.headers.get("Content-Type", "") or ""
        if "m3u8" in ctype or body[:8] == b"#EXTM3U":
            text = body.decode("utf-8", "replace")
            text = self._rewrite(text, t)
            body = text.encode("utf-8")
            ctype = "application/vnd.apple.mpegurl"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:  # noqa: BLE001
            pass

    def _rewrite(self, text: str, t: dict) -> str:
        """m3u8 里非注释行（分片/子清单 URL）全部改写为本地代理路径。"""
        pb = t["proxy"]
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                out.append(line)
                continue
            out.append(pb + "/p/" + urljoin(t["base"], line))
        return "\n".join(out)

    def log_message(self, *args):  # noqa: A002
        pass


class VlcStreamProxy:
    """本地 HTTP 代理：VLC 的 gnutls 与部分站点（阿里 OSS 等）TLS 不兼容
    （requests/OpenSSL 却可访问）。起本地代理转发 m3u8/分片，VLC 播
    http://127.0.0.1 直连，绕过 VLC 侧 TLS 握手。

    生命周期：构造即监听；close() 关闭。一个播放器一个代理（同站复用）。
    """

    def __init__(self, base_url: str, referer: str = "", user_agent: str = ""):
        self._base = base_url.rstrip("/")
        ua = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
        self._headers = {"User-Agent": ua}
        if referer:
            self._headers["Referer"] = referer
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
        self._port = self._srv.server_address[1]
        self._proxy_base = f"http://127.0.0.1:{self._port}"
        self._srv.target = {
            "base": self._base,
            "headers": self._headers,
            "proxy": self._proxy_base,
        }
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def local(self, url: str) -> str:
        """真实 URL → 本地代理 URL。"""
        return f"{self._proxy_base}/p/{url}"

    def close(self) -> None:
        try:
            self._srv.shutdown()
            self._srv.server_close()
        except Exception:  # noqa: BLE001
            pass


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
    # 内部：watchdog 子线程 → 主线程调度代理重播（winId/set_hwnd 必须主线程）
    _proxy_retry = Signal(str, str)

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
        self._proxy = None  # 本地代理（TLS 兼容问题自动降级用）
        # 事件桥接（持引用防 GC，回调只 emit 信号）
        self._bridge = _VlcBridge()
        self._bridge.ended.connect(self.ended)
        self._bridge.error.connect(self.error)
        self._bridge.time_changed.connect(self.time_changed)
        self._bridge.length_changed.connect(self.length_changed)
        self._proxy_retry.connect(self._retry_with_proxy)
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
        # 新播放前释放上次的本地代理与 media（多次播放会累积资源 → 界面卡）
        if self._proxy is not None:
            try:
                self._proxy.close()
            except Exception:  # noqa: BLE001
                pass
            self._proxy = None
        if self._media is not None:
            try:
                self._media.release()
            except Exception:  # noqa: BLE001
                pass
            self._media = None
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
            self._start_play_watchdog(video_url, audio_url)
        except Exception as exc:  # noqa: BLE001
            self._bridge.error.emit(f"VLC 播放失败：{exc}")

    def _start_play_watchdog(self, url: str, audio_url: str) -> None:
        """播放看门狗：VLC 的 gnutls 与部分站点 TLS 不兼容（Opening 卡死/
        Error），4s 未进入播放 → 自动降级本地代理重播（绕过 VLC 侧 TLS）。

        已在用代理（_proxy 非空）则跳过，避免重复降级。
        """

        def _watch() -> None:
            import time as _t

            _t.sleep(4)
            try:
                p = self._player
                if p is None or self._proxy is not None:
                    return
                st = p.get_state()
                # VLC state: 1=Opening, 2=Buffering, 3=Playing, 7=Error
                if st in (1, 7):
                    # 调度回主线程：winId/set_hwnd 等 Qt 操作必须主线程，否则内嵌黑屏
                    self._proxy_retry.emit(url, audio_url)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_watch, daemon=True).start()

    def _retry_with_proxy(self, url: str, audio_url: str) -> None:
        """用本地代理重播（requests/OpenSSL 访问，VLC 播本地直连）。"""
        proxy = None
        try:
            from urllib.parse import urlsplit

            parts = urlsplit(url)
            base = f"{parts.scheme}://{parts.netloc}"
            proxy = VlcStreamProxy(
                base, referer=self._referer, user_agent=self._user_agent
            )
            local = proxy.local(url)
            self.stop()
            self._proxy = proxy
            if audio_url:
                self._media = _get_instance().media_new(local)
                self._media.add_option("input-slave=" + audio_url)
                self._player.set_media(self._media)
            else:
                self._player.set_mrl(local)
            self._attach_window()
            self._player.play()
        except Exception:  # noqa: BLE001
            if proxy is not None:
                try:
                    proxy.close()
                except Exception:  # noqa: BLE001
                    pass
            self._proxy = None

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
        """VLC 播放状态（0=Nothing 1=Opening 2=Buffering 3=Playing
        4=Paused 5=Stopped 6=Ended 7=Error）。

        python-vlc 各版本 get_state 返回类型不一（Enum / 原始小端 uint32
        字节 / int），统一归一化；失败返回 -1。
        """
        try:
            st = self._player.get_state()
            if isinstance(st, bytes):
                return int.from_bytes(st, "little")
            if hasattr(st, "value"):  # vlc.State 枚举（可能非 IntEnum）
                return int(st.value)
            return int(st)
        except Exception:  # noqa: BLE001
            return -1

    def close(self) -> None:
        """释放本播放器（App 退出前逐个调用；实例由 shutdown_vlc 最后释放）。"""
        if self._proxy is not None:
            try:
                self._proxy.close()
            except Exception:  # noqa: BLE001
                pass
            self._proxy = None
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
