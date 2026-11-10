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
import shutil
import subprocess
import sys
import tempfile
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
_IPC_POLL_INTERVAL = 0.02
_START_WATCHDOG_S = 20.0
# 支持 m3u 播放列表的播放器（ffplay 不支持：给了列表也只会当普通文件播）
_PLAYLIST_KEYS = frozenset({"mpv", "potplayer", "mpchc", "vlc"})

# 需要观察的 mpv 属性（顺序即 observe_property 的整数 id，从 1 开始）
_OBSERVED_PROPERTIES = (
    "time-pos",
    "duration",
    "pause",
    "core-idle",
    "demuxer-cache-state",
    "volume",
    "mute",
    "speed",
    "media-title",
    # 音画同步：avsync 是 mpv 解码器算出的音视频差值（正=音频领先），
    # audio-delay 是当前音频延迟，纠偏时两者一起用。
    "avsync",
    "audio-delay",
    # 播放列表当前位置（0 基）：用户在外播里点「下一集 / 上一集」时变，本地
    # 视频靠它跟随当前集，否则进度会一直记在起播那一集上。
    "playlist-pos",
)


# --------------------------------------------------------------------------- #
# JSON IPC（Windows 命名管道 / POSIX socket）
# --------------------------------------------------------------------------- #
class _MpvIPC:
    """mpv 的 ``--input-ipc-server`` 客户端（按行分隔 JSON）。

    读线程把事件交给 ``on_event`` 回调；回调里只做信号 emit（Qt 会自动
    跨线程排队），不做阻塞操作。

    **为什么读循环要轮询而不是阻塞读**：mpv 的 ``observe_property`` 事件和
    命令回执只发给**发命令的那条连接**，所以观察与控制必须共用一条连接。
    可 Windows 命名管道是同步句柄 —— 句柄上挂着一次阻塞 ``ReadFile`` 时，
    另一个线程的 ``WriteFile`` 要等它完成。播放暂停后 mpv 不再推送任何
    属性变化，读永远不返回，于是"暂停后拖进度条/调音量"就死在这条 write
    上（实测 seek 卡住 6s 以上）。这里改用 ``PeekNamedPipe`` 先问"有多少
    字节可读"，有数据才 ``read(可用字节数)``，句柄上永远没有挂起的读，
    写立刻返回；POLL 间隔 20ms，对进度条刷新足够。POSIX socket 读写互不
    阻塞，直接阻塞 ``read`` 即可。
    """

    def __init__(self, server: str, on_event) -> None:
        self.server = server
        self._on_event = on_event
        self._conn = None
        self._handle = None      # Windows 下的 OS 句柄（PeekNamedPipe 用）
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
        self._handle = _os_pipe_handle(self._conn)
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

    def _peek_available(self) -> int:
        """可读字节数；-1 表示管道已断（Windows 专用，其他平台返回 -1）。"""
        if self._handle is None:
            return -1
        return _peek_pipe(self._handle)

    def _read_loop(self) -> None:
        buf = b""
        polling = self._handle is not None
        while self.ok:
            if polling:
                available = self._peek_available()
                if available < 0:
                    break
                if available == 0:
                    time.sleep(_IPC_POLL_INTERVAL)
                    continue
                want = min(available, 65536)
            else:
                want = 65536
            try:
                chunk = self._conn.read(want)
            except (OSError, ValueError, AttributeError):
                break
            if not chunk:
                if polling:
                    time.sleep(_IPC_POLL_INTERVAL)
                    continue
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
        self._handle = None
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


def _os_pipe_handle(conn):
    """取文件对象背后的 Windows OS 句柄；拿不到就返回 None（走阻塞读）。"""
    if sys.platform != "win32":
        return None
    try:
        import msvcrt

        return int(msvcrt.get_osfhandle(conn.fileno()))
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _peek_pipe(handle: int) -> int:
    """PeekNamedPipe 的薄封装：返回可读字节数，失败返回 -1。"""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        peek = kernel32.PeekNamedPipe
        peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                         wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD]
        peek.restype = wintypes.BOOL
        available = wintypes.DWORD(0)
        ok = peek(wintypes.HANDLE(handle), None, 0, None,
                  ctypes.byref(available), None)
        if not ok:
            return -1
        return int(available.value)
    except (OSError, AttributeError, TypeError, ValueError):
        return -1


# --------------------------------------------------------------------------- #
# 播放后端
# --------------------------------------------------------------------------- #
class PlayerSession(QObject):
    """一次播放会话：拉起外部播放器（默认 mpv）并（尽量）接管控制。"""

    # -- 音画漂移看门狗（下面几个阈值给测试留了可调口子）----------------------
    # 触发阈值：人耳对 0.2s 以上的口型偏差很敏感，0.15s 以下的抖动不值得动。
    AVSYNC_TRIGGER_S = 0.25
    # 一帧到位阈值：偏差大到这个程度一次就纠（缓冲抖动只会是单次尖峰）
    AVSYNC_HARD_S = 0.60
    # 两次纠偏之间的最小间隔：mpv 改 audio-delay 后要重新收敛，立刻再改会来回抖
    AVSYNC_COOLDOWN_S = 12.0
    # 单次会话最多纠几次：超过说明片源本身时序就坏了（比如清单里两段片子硬拼），
    # 反复改只会让声音忽前忽后，不如保持原样让用户换线路。
    AVSYNC_MAX_FIXES = 4
    # 起播/切集后的预热：这段时间 avsync 会被起播抖动污染
    AVSYNC_WARMUP_MS = 5000
    AVSYNC_WARMUP_S = 5.0
    # 一次采样就报出 >1.5s 的偏差不可能是"真漂移"（漂移是慢慢累积的）：
    # 2026-11-04 现场 avsync=-3.616s 出现在播放到 5.0s 的第一个采样上，
    # 当时看门狗直接把 audio-delay 推到 -3.000s，声音被推飞 3 秒。
    AVSYNC_MAX_PLAUSIBLE_S = 1.5
    AVSYNC_MAX_DELAY_S = 3.0

    ended = Signal()
    error = Signal(str)
    time_changed = Signal(int)      # 毫秒
    length_changed = Signal(int)    # 毫秒
    buffering = Signal(int)         # 0~100（100=缓冲完成）
    state_changed = Signal(int)     # 见 ST_*
    sync_corrected = Signal(float)  # 音画纠偏量（秒，正=把音频往后推）
    playlist_pos_changed = Signal(int)  # 外播列表当前项（0 基），-1=无列表

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
        self._file_loaded = False
        self._watchdog = None
        self._player_key = ""
        self._player_name = ""
        self._leases: list = []      # 待释放的代理租约回收函数
        self._playlist_path = ""     # 外部播放器当前加载的 m3u 播放列表
        self._playlist_dir = ""      # 列表临时目录（close 时删除）
        self._playlist_pos = -1      # 列表当前项（0 基，-1=无列表）
        # 音画同步看门狗状态
        self._avsync = 0.0           # mpv 报告的音视频差值（秒，正=音频领先）
        self._audio_delay = 0.0      # 当前 audio-delay 累计值（秒）
        self._base_audio_delay = 0.0  # 用户附加参数给的基线（--audio-delay=）
        self._avsync_hits = 0        # 连续超阈次数（用来滤掉单次缓冲尖峰）
        self._sync_fixes = 0         # 本次会话已纠偏次数
        self._sync_last_fix_at = 0.0
        self._sync_armed_at = 0.0    # 预热起点（monotonic，file-loaded 时刻）
        self._pending_resume_s = 0   # 列表模式下待 seek 的续播点（秒）

    # -- 生命周期 -----------------------------------------------------------
    def play(self, video_url: str, audio_url: str = "", title: str = "",
             kind: str = "", network_caching_ms: int = 0,
             start_s: int = 0, playlist=None, playlist_index: int = 0) -> None:
        """在外部播放器里开播 ``video_url``（通常是本地代理地址）。

        playlist：[(url, 标题), …]，按集序给出整部作品的播放列表。播放器因此
        自带「下一集」能力（mpv playlist-next / PotPlayer、VLC 的列表项），
        切换时不重启进程；每项地址是媒体代理的 /e/<key>/<idx>，命中预取缓存
        就直接 302 开播。为空时退化为单文件播放。
        """
        if not video_url:
            raise ValueError("缺少播放地址")
        self.close()
        prefs = desktop_player.load_prefs()
        cached = max(0, int(network_caching_ms or 0))
        server = self._ipc_server()
        playlist_path, playlist_start = "", 0
        # 续播点不能用 --start 交给带列表的 mpv：`start` 是**逐文件**选项，
        # 会应用到列表的每一项（2026-11-04 实测：第 2 项也从第 4 秒起播）。
        # 续到 20 分钟后，后面每一集都会从头被切掉 20 分钟。列表模式改成
        # 起播后对当前集 IPC 绝对 seek 一次（见 file-loaded 分支）。
        resume_s = max(0, int(start_s or 0))
        if playlist:
            playlist_path, playlist_start = self._prepare_playlist(
                playlist, playlist_index, prefs
            )
            if playlist_path:
                video_url = playlist_path
                audio_url = ""
        # 只有 mpv 能在起播后补 seek（JSON IPC）。其它播放器没有回执通道，
        # 继续用它们各自的 --start/--start-time 参数（列表已被裁到当前集）。
        seekable = False
        if playlist_path:
            try:
                seekable = desktop_player.resolve(
                    prefs.player, prefs.player_path)[0] == "mpv"
            except Exception:  # noqa: BLE001 —— 探测失败按"不可 seek"处理
                seekable = False
        self._pending_resume_s = resume_s if seekable else 0
        launched = desktop_player.launch(
            video_url,
            preferred=prefs.player,
            explicit_path=prefs.player_path,
            headers=self._headers(),
            title=title or "",
            start_s=0 if seekable else resume_s,
            extra_args=self._merged_args(prefs.extra_args, kind, cached),
            audio_url=audio_url,
            # mpv 专属：IPC 服务（其它播放器忽略，界面退化为「拉起即放手」）
            ipc_server=server,
            playlist_start=playlist_start if playlist_path else None,
        )
        self._proc = launched.process
        self._player_key = launched.key
        self._player_name = launched.name
        self._pending_audio = audio_url or ""
        self._file_loaded = False
        # 用户附加参数里的 --audio-delay 是本会话的音画基线（纠偏在其上叠加）
        self._base_audio_delay = self._arg_audio_delay(
            prefs.extra_args, self._user_args
        )
        self._reset_sync_state()
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

    def _prepare_playlist(self, entries, index: int, prefs) -> tuple:
        """写 m3u 播放列表到临时目录，返回 ``(路径, 起始项)``。

        mpv 拿整份列表 + ``--playlist-start`` 定位当前集（上一集也能用）；
        其它播放器不认这个参数，直接把列表裁到当前集开头（避免从第 1 集
        开始播错集）。任何一步失败都返回空路径，调用方退化单文件播放。
        """
        items = [(str(url), str(title or ""))
                 for url, title in (entries or ()) if url]
        if not items:
            return "", 0
        try:
            start = max(0, min(int(index or 0), len(items) - 1))
        except (TypeError, ValueError):
            start = 0
        try:
            player_key, _exe = desktop_player.resolve(
                prefs.player, prefs.player_path
            )
        except Exception:  # noqa: BLE001 —— 探测失败按 mpv 风格（整份列表）
            player_key = ""
        if player_key not in _PLAYLIST_KEYS:
            return "", 0
        if player_key != "mpv":
            items = items[start:]
            start = 0
        directory = tempfile.mkdtemp(prefix="claw-playlist-")
        path = os.path.join(directory, "episodes.m3u")
        lines = ["#EXTM3U"]
        for position, (url, title) in enumerate(items, start=1):
            label = " ".join(str(title or "").split()) or f"第{position}集"
            lines.append(f"#EXTINF:-1,{label}")
            lines.append(url)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except OSError:
            shutil.rmtree(directory, ignore_errors=True)
            return "", 0
        self._playlist_dir = directory
        self._playlist_path = path
        return path, start

    @property
    def has_playlist(self) -> bool:
        """当前会话是否加载了带「下一集」的播放列表。"""
        return bool(self._playlist_path)

    def playlist_next(self) -> bool:
        """让播放器前进到播放列表下一项（仅 mpv 的 IPC 可用）。"""
        return self._playlist_command("playlist-next")

    def playlist_prev(self) -> bool:
        """让播放器回到播放列表上一项（仅 mpv 的 IPC 可用）。"""
        return self._playlist_command("playlist-prev")

    def _playlist_command(self, command: str) -> bool:
        if not self.has_playlist:
            return False
        ipc = self._ipc
        if ipc is None or not ipc.ok:
            return False
        # weak = 列表边界不做动作（避免列表末尾回绕/退出）
        return ipc.send([command, "weak"]) > 0

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
        # observe_property 的签名是 ``observe_property <id> <name>``：
        # 第一个参数必须是**整数 id**（客户端自选，用来在 property-change
        # 里认出是哪条观察）。早先这里传的是属性名，mpv 一律回
        # ``{"error": "invalid parameter"}`` —— 结果一个 time-pos/duration
        # 事件都不来，进度条、时长、"载入中"全部死掉。
        for index, prop in enumerate(_OBSERVED_PROPERTIES, start=1):
            ipc.send(["observe_property", index, prop])
        # 播放列表模式下把「上一集 / 下一集」绑到 mpv 窗口的 < / >：
        # 用户在播放器里直接切集时，App 靠 /e/ 的 on_play 回调跟随当前集。
        if self.has_playlist:
            ipc.send(["keybind", ">", "playlist-next"])
            ipc.send(["keybind", "<", "playlist-prev"])
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
            elif name == "avsync":
                self._on_avsync(value)
            elif name == "audio-delay":
                try:
                    self._audio_delay = float(value)
                except (TypeError, ValueError):
                    pass
            elif name == "playlist-pos":
                try:
                    self._playlist_pos = int(value)
                except (TypeError, ValueError):
                    return
                self.playlist_pos_changed.emit(self._playlist_pos)
        elif event == "start-file":
            self._file_loaded = False
            self._avsync_hits = 0
            self._set_state(ST_OPENING)
        elif event == "file-loaded":
            # 只有 file-loaded 之后收到的 idle 才代表「文件播完/被卸载」；
            # mpv 带 --idle=yes 启动时会在加载文件**之前**先发一条 idle，
            # 那条必须忽略，否则一开播就被判成已结束（界面显示"已播完"）。
            self._file_loaded = True
            # 新片/下一集 = 新时间轴：音画偏差与纠偏次数从零开始
            self._reset_sync_state()
            self._set_state(ST_PLAYING)
            # 列表模式的续播点在这里补（--start 会逐项生效，不能用）
            resume_s = self._pending_resume_s
            if resume_s > 0:
                self._pending_resume_s = 0
                self._send(["seek", resume_s, "absolute"])
        elif event == "end-file":
            self._file_loaded = False
            if message.get("reason") == "eof":
                self._time_ms = self._length_ms or self._time_ms
                self._set_state(ST_ENDED)
                self.ended.emit()
            else:
                self._set_state(ST_STOPPED)
        elif event == "idle":
            if self._file_loaded and self._state not in (ST_ENDED, ST_ERROR):
                self._file_loaded = False
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

    # -- 音画同步看门狗 -----------------------------------------------------
    def _on_avsync(self, value) -> None:
        """mpv 报的音视频差值 → 偏差持续超阈时用 audio-delay 把音频拉回画面。

        现场（omofuna 2026-11 实测）：缓存分片的真实帧周期是 3.12s，而清单
        `#EXTINF` 一律写 3.00s（477 片累计少算约 57s），mpv 以音频时钟为主
        时钟，长片播放 / 多次续播之后"音频比画面快一点"的偏差会累积到肉眼
        可辨——用户描述就是"看着看着声音和画面不同步了"。这类漂移不是解码
        卡顿（丢帧只影响画面流畅度），换线路、换清晰度都治不了，必须在**有
        解码器时钟信息的地方**纠正。

        mpv 的 `avsync` 属性正是它自己算出的音视频差值（正 = 音频领先），
        `audio-delay` 是音频相对视频的延迟，因此 `audio-delay += avsync`
        就是把音频对齐到画面。

        纠偏必须保守，否则用户会听到"声音一顿一顿地跳"：
        - 起播 `AVSYNC_WARMUP_MS` 内不看（起播抖动会报出 0.2~0.5s 的假偏差）；
        - 0.25~0.6s 要**连续两次**采样都超阈才动手（缓冲抖动是单次尖峰）；
        - 两次纠偏之间至少隔 `AVSYNC_COOLDOWN_S`（改完要重新收敛）；
        - 一次会话最多纠 `AVSYNC_MAX_FIXES` 次，超了说明片源时序本身坏了，
          保持原样让用户换线路，而不是把声音越推越偏。
        """
        try:
            drift = float(value)
        except (TypeError, ValueError):
            return
        self._avsync = drift
        ipc = self._ipc
        if ipc is None or self._state != ST_PLAYING:
            self._avsync_hits = 0
            return
        if self._time_ms < self.AVSYNC_WARMUP_MS:
            return
        # 预热以**墙钟**为准：续播点会直接把 time-pos 顶到几十分钟，
        # 只看 time-pos 等于"一开播就算预热完毕"（这是上面 -3.6s 的成因）。
        armed = self._sync_armed_at
        if armed and time.monotonic() - armed < self.AVSYNC_WARMUP_S:
            return
        magnitude = abs(drift)
        if magnitude > self.AVSYNC_MAX_PLAUSIBLE_S:
            # 起播/缓冲期的假读数：不是真漂移，忽略并且不占用纠偏次数
            self._avsync_hits = 0
            log.debug("avsync=%+.3fs 超可信范围，按起播噪声忽略", drift)
            return
        if magnitude < self.AVSYNC_TRIGGER_S:
            self._avsync_hits = 0
            return
        self._avsync_hits += 1
        if magnitude < self.AVSYNC_HARD_S and self._avsync_hits < 2:
            return
        if self._sync_fixes >= self.AVSYNC_MAX_FIXES:
            return
        now = time.monotonic()
        if now - self._sync_last_fix_at < self.AVSYNC_COOLDOWN_S:
            return
        limit = self.AVSYNC_MAX_DELAY_S
        target = max(-limit, min(limit, self._audio_delay + drift))
        applied = target - self._audio_delay
        if abs(applied) < 0.05:
            return
        self._audio_delay = target
        self._sync_fixes += 1
        self._sync_last_fix_at = now
        self._avsync_hits = 0
        self._send(["set_property", "audio-delay", f"{target:.3f}"])
        log.info(
            "音画漂移纠偏：avsync=%+.3fs → audio-delay=%+.3fs（第 %d/%d 次，"
            "播放到 %.1fs）",
            drift, target, self._sync_fixes, self.AVSYNC_MAX_FIXES,
            self._time_ms / 1000.0,
        )
        self.sync_corrected.emit(applied)

    def _reset_sync_state(self) -> None:
        """换片/切集时重置看门狗：新时间轴的偏差要从零累计。

        但 `audio-delay` 的**基线**要保留：用户可能在附加参数里自带
        `--audio-delay=0.2`（某些片源音频本来就早 0.2s）。纠偏是在这个基线上
        叠加，抹成 0 等于一开播就把用户的设置推翻。
        """
        self._avsync = 0.0
        self._audio_delay = self._base_audio_delay
        self._avsync_hits = 0
        self._sync_fixes = 0
        self._sync_last_fix_at = 0.0
        self._sync_armed_at = time.monotonic()

    @staticmethod
    def _arg_audio_delay(*groups) -> float:
        """从播放器附加参数里读 `--audio-delay=<秒>`（没有则 0）。"""
        for group in groups:
            for arg in group or ():
                text = str(arg)
                if text.startswith("--audio-delay="):
                    try:
                        return float(text.split("=", 1)[1])
                    except (TypeError, ValueError):
                        continue
        return 0.0

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
        # 只有真正播过（或失败过）才补一个"已停止"；刚建会话就 close()
        # 不该把状态从 NOTHING 推到 STOPPED —— 否则界面会闪一下"已停止"。
        if self._state not in (ST_NOTHING, ST_ENDED, ST_ERROR):
            self._set_state(ST_STOPPED)
        self._time_ms = 0
        self._length_ms = 0
        # 播放列表临时目录随会话结束清理（播放器进程已退出，不再读它）
        directory, self._playlist_dir = self._playlist_dir, ""
        self._playlist_path = ""
        self._playlist_pos = -1
        if directory:
            shutil.rmtree(directory, ignore_errors=True)

    # 兼容旧调用点：VlcPlayer 需要 rehook HWND，这里没有窗口可挂
    def rehook(self) -> None:
        return None

    def release(self) -> None:
        self.close()
