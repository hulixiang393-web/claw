"""播放后端（player_session.py）——**mpv + JSON IPC**，不依赖 VLC。

为什么重写
----------
原内嵌播放器基于 python-vlc（``framework/vlc_player.py``），现场问题是
「播一会儿就卡」：VLC 的 TS 解复用对时间戳抖动过于敏感。VLC 已整体下线，
这里改用 mpv 作唯一后端：

- 画面在 mpv 自己的窗口里（不再 ``set_hwnd`` 嵌进 Qt 控件）；
- 通过 ``--input-ipc-server`` 的命名管道读写状态与命令，所以 **App 里的
  播放/暂停、进度条、音量、倍速、进度回写、缓冲提示全部照旧可用**，
  不再是「拉起一个窗口就失去控制」。

对外契约与旧 ``VlcPlayer`` **保持一致**（信号 + 方法名），这样
``gui/pages/reader/video_view.py`` 的 20 多处调用点不用改语义：

    ended / error / time_changed / length_changed / buffering / state_changed
    play / pause / resume / toggle_pause / set_position / get_position /
    get_time / get_length / get_state / is_active / is_playing / is_paused /
    set_volume / toggle_mute / is_muted / set_rate / seek_relative / close

状态码沿用旧语义（0 Nothing / 1 Opening / 2 Buffering / 3 Playing /
4 Paused / 5 Stopped / 6 Ended / 7 Error），调用方无需改动。

IPC 不可用时（mpv 未安装、管道没建成、用户改用 PotPlayer 等）**降级而不是
失败**：播放照常交给外部播放器，只是状态/进度拿不到（信号不发）。
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from . import desktop_player

log = logging.getLogger(__name__)

# 状态码（与历史 VLC 语义一致，video_view 的图标/覆盖层逻辑依赖同一套值）
ST_NOTHING = 0
ST_OPENING = 1
ST_BUFFERING = 2
ST_PLAYING = 3
ST_PAUSED = 4
ST_STOPPED = 5
ST_ENDED = 6
ST_ERROR = 7

_IPC_CONNECT_TIMEOUT = 8.0
_IPC_CONNECT_INTERVAL = 0.1
_START_WATCHDOG_S = 20.0


# --------------------------------------------------------------------------- #
# JSON IPC（Windows 命名管道 / POSIX socket）
# --------------------------------------------------------------------------- #
class _MpvIPC:
    """mpv 的 ``--input-ipc-server`` 客户端（按行分隔 JSON）。

    读线程把事件交给 ``on_event`` 回调；回调里只做信号 emit（Qt 会自动
    跨线程排队），不做阻塞操作。
    """

    def __init__(self, server: str, on_event) -> None:
        self.server = server
        self._on_event = on_event
        self._conn = None
        self._reader = None
        self._alive = threading.Event()
        self._ids = itertools.count(1)
        self._write_lock = threading.Lock()

    # -- 连接 ---------------------------------------------------------------
    def connect(self, timeout: float = _IPC_CONNECT_TIMEOUT) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self._conn = open(self.server, "r+b", buffering=0)
                break
            except OSError:
                time.sleep(_IPC_CONNECT_INTERVAL)
        else:
            return False
        self._alive.set()
        self._reader = threading.Thread(target=self._read_loop,
                                        name="mpv-ipc", daemon=True)
        self._reader.start()
        return True

    @property
    def ok(self) -> bool:
        return self._alive.is_set()

    # -- 读写 ---------------------------------------------------------------
    def send(self, command: list) -> int:
        if not self.ok or self._conn is None:
            return -1
        request_id = next(self._ids)
        payload = json.dumps({"command": command,
                              "request_id": request_id}) + "\n"
        try:
            with self._write_lock:
                self._conn.write(payload.encode("utf-8"))
        except (OSError, ValueError):
            self.close()
            return -1
        return request_id

    def _read_loop(self) -> None:
        buf = b""
        while self.ok:
            try:
                chunk = self._conn.read(65536)
            except (OSError, ValueError, AttributeError):
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                try:
                    self._on_event(message)
                except Exception:  # noqa: BLE001 —— 单条事件异常不能断流
                    log.warning("mpv IPC 事件处理失败", exc_info=True)
        self._alive.clear()

    def close(self) -> None:
        self._alive.clear()
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# 播放后端
# --------------------------------------------------------------------------- #
class PlayerSession(QObject):
    """一次播放会话：拉起外部播放器（默认 mpv）并（尽量）接管控制。"""

    ended = Signal()
    error = Signal(str)
    time_changed = Signal(int)      # 毫秒
    length_changed = Signal(int)    # 毫秒
    buffering = Signal(int)         # 0~100（100=缓冲完成）
    state_changed = Signal(int)     # 见 ST_*

    def __init__(self, video_widget=None, referer: str = "",
                 user_agent: str = "", user_args=None, parent=None) -> None:
        super().__init__(parent)
        # video_widget 参数只为兼容旧调用点保留：画面在 mpv 自己的窗口里
        self._referer = referer or ""
        self._user_agent = user_agent or ""
        self._user_args = tuple(user_args or ())
        self._proc = None
        self._ipc = None
        self._lock = threading.Lock()
        self._state = ST_NOTHING
        self._time_ms = 0
        self._length_ms = 0
        self._volume = 100
        self._muted = False
        self._rate = 1.0
        self._pending_audio = ""
        self._started_at = 0.0
        self._watchdog = None
        self._player_key = ""
        self._player_name = ""
        self._leases: list = []      # 待释放的代理租约回收函数

    # -- 生命周期 -----------------------------------------------------------
    def play(self, video_url: str, audio_url: str = "", title: str = "",
             kind: str = "", network_caching_ms: int = 0,
             start_s: int = 0) -> None:
        """在外部播放器里开播 ``video_url``（通常是本地代理地址）。"""
        if not video_url:
            raise ValueError("缺少播放地址")
        self.close()
        prefs = desktop_player.load_prefs()
        cached = max(0, int(network_caching_ms or 0))
        server = self._ipc_server()
        launched = desktop_player.launch(
            video_url,
            preferred=prefs.player,
            explicit_path=prefs.player_path,
            headers=self._headers(),
            title=title or "",
            start_s=max(0, int(start_s or 0)),
            extra_args=self._merged_args(prefs.extra_args, kind, cached),
            audio_url=audio_url,
            # mpv 专属：IPC 服务（其它播放器忽略，界面退化为「拉起即放手」）
            ipc_server=server,
        )
        self._proc = launched.process
        self._player_key = launched.key
        self._player_name = launched.name
        self._pending_audio = audio_url or ""
        self._set_state(ST_OPENING)
        # 代理租约：播放器暂停/长时间不请求时 token 不被空闲看门狗回收
        self._hold_proxy_lease(launched.process)
        # 只有 mpv 支持 JSON IPC；其他播放器只能「拉起即放手」
        if launched.key == "mpv":
            self._attach_ipc(launched)
        else:
            # 无 IPC：乐观置为播放中，界面不再显示「载入中」转圈
            self._set_state(ST_PLAYING)
            self.length_changed.emit(0)

    def _hold_proxy_lease(self, process) -> None:
        """播放器进程活着就占着代理租约；进程退出后自动释放。

        代理的空闲看门狗只在「无租约」时回收 server/token，播放器暂停或
        长时间不重连时 URL 必须依然有效，否则一旦播放器去重连就 404。
        """
        try:
            from .media_proxy import MediaProxy

            proxy = MediaProxy.instance()
            lease = proxy.acquire_lease()
        except Exception:  # noqa: BLE001 —— 代理不可用不影响播放
            return

        def _release() -> None:
            try:
                proxy.release_lease(lease)
            except Exception:  # noqa: BLE001
                pass

        def _wait() -> None:
            try:
                if process is not None:
                    process.wait()
            except Exception:  # noqa: BLE001
                pass
            _release()

        with self._lock:
            self._leases.append(_release)
        threading.Thread(target=_wait, name="mpv-lease", daemon=True).start()

    def _headers(self) -> dict:
        headers = {}
        if self._referer:
            headers["Referer"] = self._referer
        if self._user_agent:
            headers["User-Agent"] = self._user_agent
        return headers

    def _merged_args(self, extra, kind: str, caching_ms: int) -> tuple:
        args = list(extra or ())
        args.extend(self._user_args)
        if kind == "hls" and caching_ms > 0:
            # mpv 的 demuxer 缓存（字节）按缓冲时长粗略换算，与 VLC 口径一致
            mb = max(32, min(1024, caching_ms // 1000 * 8))
            args.append(f"--demuxer-max-bytes={mb}MiB")
        return tuple(args)

    def _ipc_server(self) -> str:
        name = f"claw-mpv-{uuid.uuid4().hex[:10]}"
        if sys.platform == "win32":
            return rf"\\.\pipe\{name}"
        return str(Path(os.environ.get("TMPDIR", "/tmp")) / f"{name}.sock")

    def _attach_ipc(self, launched) -> None:
        server = getattr(launched, "ipc_server", "") or ""
        if not server:
            self._set_state(ST_PLAYING)
            return
        ipc = _MpvIPC(server, self._on_ipc_event)
        if not ipc.connect():
            log.warning("mpv IPC 连接失败（%s）：播放继续，但界面拿不到进度", server)
            self._set_state(ST_PLAYING)
            return
        self._ipc = ipc
        for prop in ("time-pos", "duration", "pause", "core-idle",
                     "demuxer-cache-state", "volume", "mute", "speed",
                     "media-title"):
            ipc.send(["observe_property", prop, prop])
        self._ipc.send(["set_property", "pause", "no"])
        if self._pending_audio:
            self._ipc.send(["audio-add", self._pending_audio, "select"])
        self._started_at = time.time()
        self._start_watchdog()

    def _start_watchdog(self) -> None:
        def _watch() -> None:
            time.sleep(_START_WATCHDOG_S)
            if self._state in (ST_OPENING, ST_BUFFERING) and self._time_ms <= 0:
                self.error.emit("播放器起播超时")

        self._watchdog = threading.Thread(target=_watch, name="mpv-watchdog",
                                          daemon=True)
        self._watchdog.start()

    # -- IPC 事件 -----------------------------------------------------------
    def _on_ipc_event(self, message: dict) -> None:
        event = message.get("event")
        if event == "property-change":
            name = message.get("name")
            value = message.get("data")
            if value is None:
                return
            if name == "time-pos":
                self._time_ms = int(float(value) * 1000)
                self.time_changed.emit(self._time_ms)
                if self._state in (ST_OPENING, ST_BUFFERING):
                    self._set_state(ST_PLAYING)
            elif name == "duration":
                self._length_ms = int(float(value) * 1000)
                self.length_changed.emit(self._length_ms)
            elif name == "pause":
                if value:
                    self._set_state(ST_PAUSED)
                elif self._state in (ST_PAUSED, ST_NOTHING, ST_OPENING):
                    self._set_state(ST_PLAYING)
            elif name == "core-idle":
                if value and self._state == ST_PLAYING:
                    self._set_state(ST_BUFFERING)
                elif not value and self._state == ST_BUFFERING:
                    self._set_state(ST_PLAYING)
            elif name == "demuxer-cache-state":
                self._on_cache_state(value)
            elif name == "volume":
                self._volume = int(float(value))
            elif name == "mute":
                self._muted = bool(value)
            elif name == "speed":
                self._rate = float(value)
        elif event == "start-file":
            self._set_state(ST_OPENING)
        elif event == "file-loaded":
            self._set_state(ST_PLAYING)
        elif event == "end-file":
            if message.get("reason") == "eof":
                self._time_ms = self._length_ms or self._time_ms
                self._set_state(ST_ENDED)
                self.ended.emit()
            else:
                self._set_state(ST_STOPPED)
        elif event == "idle":
            self._set_state(ST_STOPPED)

    def _on_cache_state(self, value) -> None:
        """缓存状态 → 缓冲百分比（喂给「载入中/缓冲中」进度条）。"""
        try:
            if not isinstance(value, dict):
                return
            if value.get("underrun"):
                percent = 0
            else:
                percent = int(float(value.get("cache-buffering-state") or 0))
            self.buffering.emit(max(0, min(100, percent)))
        except (TypeError, ValueError):
            return

    def _set_state(self, state: int) -> None:
        if state == self._state:
            return
        self._state = state
        self.state_changed.emit(state)

    # -- 控制面 -------------------------------------------------------------
    def pause(self) -> None:
        self._send(["set_property", "pause", "yes"])

    def resume(self) -> None:
        self._send(["set_property", "pause", "no"])

    def toggle_pause(self) -> None:
        if self._state == ST_PAUSED:
            self.resume()
        else:
            self.pause()

    def set_position(self, ratio: float) -> None:
        ratio = max(0.0, min(1.0, float(ratio or 0.0)))
        if self._length_ms > 0:
            self._send(["seek", str(self._length_ms / 1000.0 * ratio), "absolute"])
        else:
            self._send(["set_property", "percent-pos", str(ratio * 100.0)])

    def seek_relative(self, seconds: float) -> None:
        self._send(["seek", str(float(seconds)), "relative"])

    def set_volume(self, value: int) -> None:
        self._volume = max(0, min(100, int(value)))
        self._send(["set_property", "volume", str(self._volume)])

    def toggle_mute(self) -> None:
        self._muted = not self._muted
        self._send(["set_property", "mute", "yes" if self._muted else "no"])

    def is_muted(self) -> bool:
        return self._muted

    def set_rate(self, rate: float) -> None:
        self._rate = float(rate or 1.0)
        self._send(["set_property", "speed", f"{self._rate:g}"])

    def get_position(self) -> float:
        if self._length_ms <= 0:
            return 0.0
        return max(0.0, min(1.0, self._time_ms / float(self._length_ms)))

    def get_length(self) -> int:
        return self._length_ms

    def get_time(self) -> int:
        return self._time_ms

    def get_state(self) -> int:
        return self._state

    def is_playing(self) -> bool:
        return self._state == ST_PLAYING

    def is_active(self) -> bool:
        return self._state in (ST_PLAYING, ST_BUFFERING)

    def is_paused(self) -> bool:
        return self._state == ST_PAUSED

    def _send(self, command: list) -> None:
        ipc = self._ipc
        if ipc is not None and ipc.ok:
            ipc.send(command)

    # -- 收尾 ---------------------------------------------------------------
    def close(self) -> None:
        ipc, self._ipc = self._ipc, None
        if ipc is not None:
            try:
                ipc.send(["quit"])
            except Exception:  # noqa: BLE001
                pass
            ipc.close()
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.wait(timeout=2.0)
            except Exception:  # noqa: BLE001 —— 没退就强杀
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        with self._lock:
            releases, self._leases = self._leases, []
        for release in releases:
            release()
        if self._state not in (ST_ENDED, ST_ERROR):
            self._set_state(ST_STOPPED)
        self._time_ms = 0
        self._length_ms = 0

    # 兼容旧调用点：VlcPlayer 需要 rehook HWND，这里没有窗口可挂
    def rehook(self) -> None:
        return None

    def release(self) -> None:
        self.close()
