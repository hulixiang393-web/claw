"""外部播放器拉起层（**非 VLC 优先**）。

为什么要有这一层
----------------
内嵌 libvlc 播长 TS 分片时「播一会儿就卡」：VLC 的 TS 解复用对时间戳不连续、
分片边界抖动、CDN 突发降速都很敏感，缓冲一见底就重同步，画面直接一顿。
mpv 用 ffmpeg 解复用，自带 readahead 与音画同步策略，对这类源稳得多，
所以默认按 **mpv → PotPlayer → MPC-HC → VLC → ffplay** 的优先级挑一个。

职责边界
--------
- 只做「发现播放器 → 拼命令行 → 拉起进程」，**不解析播放状态**；
- URL 由调用方给：走本地代理（广告过滤、防盗链头、分片缓存都在代理侧），
  播放器只拿到 ``http://127.0.0.1:PORT/...``；
- 所有参数集中在 :func:`build_argv`，按播放器版本微调只改一处。

配置（app_config.json）
----------------------
``video`` 段::

    {
      "player": "auto",              # auto | mpv | potplayer | mpchc | vlc | ffplay
      "player_path": "",             # 显式指定 exe（优先于 player）
      "player_extra_args": [],       # 追加给播放器的参数
      "external_fullscreen": false
    }
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:  # 配置读取失败不该影响拉起（调用方也可以显式传参）
    from .settings_manager import SettingsManager
except Exception:  # pragma: no cover  # noqa: BLE001
    SettingsManager = None  # type: ignore[assignment]

CONFIG_SECTION = "video"


class PlayerNotFound(RuntimeError):
    """没找到任何可用播放器。"""


# --------------------------------------------------------------------------- #
# 播放器描述
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlayerSpec:
    key: str
    name: str
    exe_names: tuple = ()          # PATH 里的可执行名（按优先级）
    win_dirs: tuple = ()           # %ENV% 展开后的候选目录
    exe_in_dir: tuple = ()         # 在候选目录里找的可执行名


# 顺序即优先级：VLC 垫后（它正是用户觉得卡的元凶）
PLAYERS: tuple = (
    PlayerSpec(
        key="mpv", name="mpv",
        exe_names=("mpv.exe", "mpv.com"),
        win_dirs=(r"%ProgramFiles%\MPV Player",
                  r"%ProgramFiles%\mpv",
                  r"%ProgramFiles(x86)%\mpv",
                  r"%LOCALAPPDATA%\Programs\mpv",
                  r"%LOCALAPPDATA%\mpv"),
        exe_in_dir=("mpv.exe", "mpv.com"),
    ),
    PlayerSpec(
        key="potplayer", name="PotPlayer",
        exe_names=("PotPlayerMini64.exe", "PotPlayerMini.exe"),
        win_dirs=(r"%ProgramFiles%\DAUM\PotPlayer",
                  r"%ProgramFiles%\PotPlayer",
                  r"%ProgramFiles(x86)%\DAUM\PotPlayer",
                  r"%ProgramFiles(x86)%\PotPlayer"),
        exe_in_dir=("PotPlayerMini64.exe", "PotPlayerMini.exe"),
    ),
    PlayerSpec(
        key="mpchc", name="MPC-HC",
        exe_names=("mpc-hc64.exe", "mpc-hc.exe"),
        win_dirs=(r"%ProgramFiles%\K-Lite Codec Pack\MPC-HC64",
                  r"%ProgramFiles%\MPC-HC64",
                  r"%ProgramFiles%\MPC-HC",
                  r"%ProgramFiles(x86)%\K-Lite Codec Pack\MPC-HC64",
                  r"%ProgramFiles(x86)%\MPC-HC"),
        exe_in_dir=("mpc-hc64.exe", "mpc-hc.exe"),
    ),
    PlayerSpec(
        key="vlc", name="VLC",
        exe_names=("vlc.exe",),
        win_dirs=(r"%ProgramFiles%\VideoLAN\VLC",
                  r"%ProgramFiles(x86)%\VideoLAN\VLC"),
        exe_in_dir=("vlc.exe",),
    ),
    PlayerSpec(
        key="ffplay", name="ffplay",
        exe_names=("ffplay.exe",),
        win_dirs=(),
        exe_in_dir=(),
    ),
)

_BY_KEY = {spec.key: spec for spec in PLAYERS}


def _expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def _is_executable_file(path: str) -> bool:
    try:
        return Path(path).is_file()
    except OSError:
        return False


def candidate_paths(spec: PlayerSpec) -> list:
    """该播放器的候选绝对路径（按优先级）。

    PATH 优先于固定目录：用户用 scoop/winget/choco 装的通常只在 PATH 里；
    但 ``mpv.com`` 是控制台壳，同目录有 ``mpv.exe`` 时优先用它（否则拉起时
    会闪一个黑框）。
    """
    found = []
    for name in spec.exe_names:
        hit = shutil.which(name)
        if hit:
            hit_path = Path(hit)
            gui_exe = hit_path.with_name("mpv.exe")
            if hit_path.suffix.lower() == ".com" and gui_exe.is_file():
                found.append(str(gui_exe))
            found.append(str(hit_path))
            break
    for raw_dir in spec.win_dirs:
        dir_path = Path(_expand(raw_dir))
        for exe in spec.exe_in_dir:
            path = dir_path / exe
            if _is_executable_file(str(path)):
                found.append(str(path))
    # 去重保序
    seen, out = set(), []
    for item in found:
        key = os.path.normcase(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def detect() -> dict:
    """探测本机可用播放器：``{key: exe 路径}``（插入顺序即优先级）。"""
    result = {}
    for spec in PLAYERS:
        for path in candidate_paths(spec):
            if _is_executable_file(path):
                result[spec.key] = path
                break
    return result


def resolve(preferred: str = "auto", explicit: str = "") -> tuple:
    """挑一个播放器，返回 ``(key, exe_path)``；一个都没有时返回 ``(None, None)``。

    ``explicit`` 是用户在配置里写死的 exe，给它就尊重它（key 按其文件名猜）。
    """
    explicit = (explicit or "").strip()
    if explicit:
        path = _expand(explicit)
        if _is_executable_file(path):
            return _guess_key(path), path
        # 写错路径不静默降级到别的播放器会让人困惑，这里退回自动探测
    key = (preferred or "auto").strip().lower()
    found = detect()
    if key and key != "auto":
        if key in found:
            return key, found[key]
        # 指定了但没装 → 落到自动，仍能播
    for spec in PLAYERS:
        if spec.key in found:
            return spec.key, found[spec.key]
    return None, None


def _guess_key(path: str) -> str:
    name = Path(path).name.lower()
    if name.startswith("mpv"):
        return "mpv"
    if name.startswith("potplayer"):
        return "potplayer"
    if name.startswith("mpc"):
        return "mpchc"
    if name.startswith("vlc"):
        return "vlc"
    if name.startswith("ffplay"):
        return "ffplay"
    return "custom"


def player_display_name(key: str) -> str:
    spec = _BY_KEY.get(key)
    return spec.name if spec else "自定义播放器"


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def _mpv_argv(exe: str, url: str, headers: dict, title: str, start_s: int,
              fullscreen: bool, extra) -> list:
    argv = [
        exe,
        "--force-window=yes",
        "--no-terminal",
        "--hwdec=auto-safe",
        # 抗抖动：给足 readahead，别等缓冲见底再重同步（VLC 卡就卡在这）
        "--cache=yes",
        "--demuxer-max-bytes=384MiB",
        "--demuxer-max-back-bytes=64MiB",
        "--demuxer-readahead-secs=90",
        "--network-timeout=30",
        "--cache-pause-wait=2",
    ]
    if headers.get("Referer"):
        argv.append("--referrer=" + headers["Referer"])
    if headers.get("User-Agent"):
        argv.append("--user-agent=" + headers["User-Agent"])
    if title:
        argv.append("--title=" + title)
    if start_s and start_s > 0:
        argv.append("--start=%d" % int(start_s))
    if fullscreen:
        argv.append("--fullscreen")
    argv.extend(extra)
    argv.append(url)
    return argv


def _potplayer_argv(exe: str, url: str, headers: dict, title: str, start_s: int,
                    fullscreen: bool, extra) -> list:
    argv = [exe, url, "/new"]
    if title:
        argv.append("/title=" + title)
    if headers.get("Referer"):
        argv.append("/referer=" + headers["Referer"])
    if headers.get("User-Agent"):
        argv.append("/useragent=" + headers["User-Agent"])
    if start_s and start_s > 0:
        argv.append("/seek=" + str(int(start_s)))
    if fullscreen:
        argv.append("/fullscreen")
    argv.extend(extra)
    return argv


def _mpchc_argv(exe: str, url: str, headers: dict, title: str, start_s: int,
                fullscreen: bool, extra) -> list:
    argv = [exe, url, "/play", "/new"]
    if start_s and start_s > 0:
        argv.extend(["/start", str(int(start_s) * 1000)])
    if fullscreen:
        argv.append("/fullscreen")
    argv.extend(extra)
    return argv


def _vlc_argv(exe: str, url: str, headers: dict, title: str, start_s: int,
              fullscreen: bool, extra) -> list:
    argv = [exe, "--network-caching=12000", "--no-video-title-show"]
    if headers.get("Referer"):
        argv.append("--http-referrer=" + headers["Referer"])
    if headers.get("User-Agent"):
        argv.append("--http-user-agent=" + headers["User-Agent"])
    if title:
        argv.append("--meta-title=" + title)
    if start_s and start_s > 0:
        argv.append("--start-time=%d" % int(start_s))
    if fullscreen:
        argv.append("--fullscreen")
    argv.extend(extra)
    argv.append(url)
    return argv


def _ffplay_argv(exe: str, url: str, headers: dict, title: str, start_s: int,
                 fullscreen: bool, extra) -> list:
    argv = [exe, "-loglevel", "error"]
    if title:
        argv.extend(["-window_title", title])
    header_lines = "\r\n".join(f"{k}: {v}" for k, v in headers.items() if v)
    if header_lines:
        argv.extend(["-headers", header_lines + "\r\n"])
    if start_s and start_s > 0:
        argv.extend(["-ss", str(int(start_s))])
    if fullscreen:
        argv.append("-fs")
    argv.extend(extra)
    argv.append(url)
    return argv


_ARGV_BUILDERS = {
    "mpv": _mpv_argv,
    "potplayer": _potplayer_argv,
    "mpchc": _mpchc_argv,
    "vlc": _vlc_argv,
    "ffplay": _ffplay_argv,
}

# DASH 双流（视频+独立音频轨）时外挂音频的参数；不支持的播放器忽略即可
_AUDIO_ARG = {
    "mpv": "--audio-file=",
    "vlc": "--input-slave=",
}


def build_argv(key: str, exe: str, url: str, headers: dict | None = None,
               title: str = "", start_s: int = 0, fullscreen: bool = False,
               extra_args=(), audio_url: str = "", ipc_server: str = "") -> list:
    """按播放器类型拼命令行。未识别的播放器按 mpv 风格兜底（最通用）。"""
    if not url:
        raise ValueError("缺少播放地址")
    headers = dict(headers or {})
    extra = [str(a) for a in (extra_args or ()) if str(a)]
    builder = _ARGV_BUILDERS.get(key, _mpv_argv)
    argv = builder(exe, url, headers, title or "", int(start_s or 0),
                   bool(fullscreen), extra)
    audio_url = (audio_url or "").strip()
    if audio_url:
        flag = _AUDIO_ARG.get(key)
        if flag:
            # 各播放器都把媒体地址放在最后一位：音频参数插在它前面
            argv.insert(len(argv) - 1, flag + audio_url)
    # JSON IPC 只有 mpv 支持（App 靠它读进度/发控制命令）
    if ipc_server and key == "mpv":
        argv.insert(len(argv) - 1, "--input-ipc-server=" + ipc_server)
        argv.insert(len(argv) - 1, "--idle=yes")
    return argv


# --------------------------------------------------------------------------- #
# 拉起
# --------------------------------------------------------------------------- #
@dataclass
class Launched:
    key: str
    name: str
    exe: str
    argv: list = field(default_factory=list)
    process: object = None
    ipc_server: str = ""        # mpv 的 JSON IPC 端点（其它播放器为空）

    @property
    def pid(self):
        return getattr(self.process, "pid", None)


_RUNNING: list = []


def _reap_finished() -> None:
    """回收已退出的播放器进程（避免 handle 泄漏）。"""
    alive = []
    for proc in _RUNNING:
        try:
            if proc.poll() is None:
                alive.append(proc)
        except Exception:  # noqa: BLE001
            continue
    _RUNNING[:] = alive


def _spawn(argv: list) -> subprocess.Popen:
    """分离式拉起：不占父进程控制台、不随父进程退出被杀。

    Windows 用 DETACHED_PROCESS（播放器有自己的窗口，不能加 CREATE_NO_WINDOW，
    否则 mpv 的窗口也会被隐藏）。``creationflags=`` 必须写成字面关键字：
    tests/test_no_console_windows.py 静态扫描要求每个 subprocess 调用点显式抑制
    控制台窗口。
    """
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    win = sys.platform == "win32"
    # posix 只接受 creationflags=0（非零即 ValueError），Windows 要 DETACHED_PROCESS。
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if win else 0
    try:
        return subprocess.Popen(
            argv, creationflags=flags, start_new_session=not win, **kwargs)
    except OSError as exc:
        raise PlayerNotFound(f"拉起播放器失败：{exc}") from exc


def launch(url: str, preferred: str = "auto", explicit_path: str = "",
           headers: dict | None = None, title: str = "", start_s: int = 0,
           fullscreen: bool = False, extra_args=(), audio_url: str = "",
           ipc_server: str = "") -> Launched:
    """用外部播放器打开 ``url``（通常是本地代理地址）。"""
    key, exe = resolve(preferred, explicit_path)
    if not key or not exe:
        raise PlayerNotFound(
            "没有找到可用的外部播放器（推荐装 mpv：winget install mpv）")
    argv = build_argv(key, exe, url, headers=headers, title=title,
                      start_s=start_s, fullscreen=fullscreen,
                      extra_args=extra_args, audio_url=audio_url,
                      ipc_server=ipc_server)
    _reap_finished()
    proc = _spawn(argv)
    _RUNNING.append(proc)
    return Launched(key=key, name=player_display_name(key), exe=exe,
                    argv=argv, process=proc,
                    ipc_server=ipc_server if key == "mpv" else "")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Prefs:
    player: str = "auto"
    player_path: str = ""
    extra_args: tuple = ()
    fullscreen: bool = False


def load_prefs(base_dir=None) -> Prefs:
    """读 ``app_config.json`` 的 video 段；任何异常都退回默认值。"""
    if SettingsManager is None:
        return Prefs()
    try:
        root = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
        sm = SettingsManager(root / "app_config.json")
        section = sm.get_section(CONFIG_SECTION) or {}
    except Exception:  # noqa: BLE001 —— 配置坏了也要能播
        return Prefs()
    extra = section.get("player_extra_args") or ()
    if isinstance(extra, str):
        extra = [extra]
    try:
        extra = tuple(str(a) for a in extra if str(a).strip())
    except TypeError:
        extra = ()
    return Prefs(
        player=str(section.get("player") or "auto"),
        player_path=str(section.get("player_path") or ""),
        extra_args=extra,
        fullscreen=bool(section.get("external_fullscreen")),
    )
