"""外部播放器调用（external_player.py）。

播放交给独立播放器进程（VLC 桌面版优先，浏览器兜底），不再内嵌 libvlc：
- 独立进程完整渲染管线，无 Qt 主线程/内嵌 hwnd 干扰 → 1080p+ 流畅
- VLC 桌面版自带"恢复播放位置"（记住每个媒体的进度）→ 续读由播放器自己接管
- 防盗链透传：VLC 命令行 --http-referrer 只对 Referer 生效，且实测 VLC 3.0.23
  无法覆盖 User-Agent（CDN 常拒绝 VLC 默认 UA）。因此**带防盗链头的媒体一律
  走本地代理（framework/media_proxy.py）**：代理把源配置的完整 headers
  （Referer/UA/Cookie…）打在请求上，播放器只播本地回环 URL，全类型可看。

播放器探测顺序：
1. VLC 桌面版（常见安装路径 + PATH）
2. 系统默认打开方式（webbrowser / os.startfile）
"""
import os
import secrets
import shutil
import socket
import subprocess
import webbrowser

try:
    import requests
except ImportError:  # pragma: no cover —— VLC 控制接口非必需，缺库时 player_command 静默 False
    requests = None

from .media_proxy import proxy_url_for

_VLC_CANDIDATES = [
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    r"C:\Users\%s\AppData\Local\Programs\VLC\vlc.exe" % os.environ.get("USERNAME", ""),
]

_found_vlc = None

# 最近一次拉起的播放器进程句柄（模块级）：每次换集/重开播放器都会拉起新的
# VLC 实例，旧实例可能仍在播放/挂着——新开前先 terminate 旧的，避免多 VLC
# 窗口堆积。旧进程可能已自行退出（用户手动关闭/播放结束），terminate 必须
# try/except 幂等包裹。
_last_proc: "subprocess.Popen | None" = None

# 最近一次拉起的 VLC 的 HTTP 控制接口状态（host/port/password）：
# 仅在 VLC 成功拉起后写入；_terminate_previous 关旧进程时清空为 None；
# 浏览器 fallback / 启动失败时不写。player_command 据此向 VLC 发控制命令。
_control_state: dict | None = None


def _locate_vlc() -> str | None:
    """定位 VLC 桌面版可执行文件（候选路径 + PATH）。"""
    global _found_vlc
    if _found_vlc is not None:
        return _found_vlc or None
    for c in _VLC_CANDIDATES:
        if os.path.isfile(c):
            _found_vlc = c
            return c
    v = shutil.which("vlc")
    if v:
        _found_vlc = v
        return v
    _found_vlc = ""
    return None


def _terminate_previous() -> None:
    """关闭上一次拉起的播放器进程（幂等）。

    换集/重开播放器时终止旧 VLC 实例；旧进程可能已退出或句柄失效
    （用户手动关闭/播放自然结束后句柄仍在），terminate 一律 try/except
    包裹，不抛异常、不干扰本次拉起新版。同时清空 _control_state：
    旧实例被关后其 HTTP 控制会话即失效（换集重开会重建）。
    """
    global _last_proc, _control_state
    proc, _last_proc = _last_proc, None
    _control_state = None
    if proc is None:
        return
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001 —— 进程已退出/句柄失效：静默
        pass


def _pick_http_port() -> int:
    """随机空闲端口（bind 0 拿端口再 close）；异常兜底固定 8090。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
    except OSError:
        return 8090


def open_with_player(url: str, audio: str = "", referer: str = "",
                     user_agent: str = "", headers: dict | None = None,
                     ad_block: dict | None = None,
                     force_proxy: bool = False,
                     episodes: list | None = None) -> str:
    """用外部播放器打开媒体地址。

    url      媒体直链（单流）
    audio    DASH 音频轨地址（非空时以 input-slave 挂入）
    referer / user_agent / headers  防盗链透传。referer/user_agent 是兼容旧
            调用的便捷参数；headers 提供完整头（含 Cookie 等）。任何防盗链
            头存在时走本地代理（VLC 无法设置 UA，只有代理能根治）。HLS 流
            一律走本地代理（含广告段过滤），即使无防盗链头也让代理剔除广告段。
    ad_block 源 ad_block 配置，非空时代理转发 m3u8 会剔除广告段。
    force_proxy 代理转发时强制走系统代理会话（跳过直连探测）：用于直连不稳
            但系统代理稳定的源（如 hanime mp4 直连 0.3~1MB/s 波动但代理
            1.8~2.6MB/s）。传 True 时主 MRL/音频轨/episodes 每集的本地代理
            URL 都带 force_proxy；不传（默认 False）行为与旧版完全一致。
    episodes 当前集起的完整播放列表 list[tuple[url, audio, title]]，
            **当前集放首位**（首项一般即 url 这一集；主 MRL 已是当前集，
            列表从第 2 项起逐集追加）。VLC 收到多 MRL 自动组成播放列表，
            用户在播放器内用 P/N（上一首/下一首）或播放列表面板切集。
            每集 URL 后的音频轨以 :input-slave= 紧跟其 MRL。列表内某集
            URL 为空则跳过该集，不影响其余集。None → 与旧行为完全一致。

    附加能力：VLC 分支启动时附带 HTTP 控制接口（--extraintf=http），GUI
    键盘事件可经 player_command() 转成 VLC 控制命令（播放/暂停/进退等）。
    """
    global _last_proc, _control_state
    if not url:
        return ""
    vlc = _locate_vlc()
    if vlc:
        # 汇总防盗链头
        hdrs = dict(headers or {})
        if referer:
            hdrs.setdefault("Referer", referer)
        if user_agent:
            hdrs.setdefault("User-Agent", user_agent)

        def _resolve(target: str, src_audio: str) -> tuple:
            """把上游媒体 URL 解析成播放 URL（与单集路径同一套判定）。

            HLS 一律走本地代理（广告过滤）；非 HLS 带防盗链头才走代理；
            否则直连。音频轨非空也走同一代理。tuple = (play_url, audio_url)。
            force_proxy 透传给代理：主 MRL/音频/episodes 每集都带。
            """
            is_hls = target.split("?", 1)[0].lower().endswith(".m3u8")
            if hdrs or is_hls:
                play_url = proxy_url_for(target, hdrs, ad_block=ad_block,
                                         force_proxy=force_proxy)
                audio_url = (
                    proxy_url_for(src_audio, hdrs, ad_block=ad_block,
                                  force_proxy=force_proxy)
                    if src_audio else ""
                )
            else:
                play_url = target
                audio_url = src_audio
            return play_url, audio_url

        play_url, audio_url = _resolve(url, audio)
        # 缓冲调优：按媒体类型给 VLC 设 network-caching（HLS 分片流网络抖动
        # 敏感，慢 CDN 每片 1-2s 时默认 300ms 缓冲会频繁卡顿/加载慢）。
        # 复用 media_tuner.classify 的缓冲画像，缺省 HLS 5000ms 抗慢 CDN。
        # 注意：类型要用**原始媒体 URL** 判定——play_url 走本地代理后是
        # http://127.0.0.1:/s/token，不含 .m3u8/.mp4 特征，会被判成 unknown
        # 拿 2500ms，走代理的慢 HLS 反而缓冲更小（播放卡顿的根因之一）。
        try:
            from .media_tuner import classify as _classify
            _profile = _classify(url)
            _caching = max(_profile.buffer_ms, 5000) if _profile.kind == "hls" else _profile.buffer_ms
            # 经本地代理转发（防盗链头）多一跳、更抖，缓冲再加大抗卡顿
            if play_url != url:
                _caching = max(_caching, 8000)
        except Exception:  # noqa: BLE001
            _caching = 5000
        # 播放处理（调研 VLC 流播放调优）：除加大网络缓冲外，加 --no-drop-late-frames
        # 让 VLC 不丢晚到的帧（默认丢帧会表现为画面卡顿跳动）；不强制硬件解码——
        # DXVA2/D3D11VA 的 copy-back 开销在某些机器上反而更卡，交给 VLC 自动判断。
        # 组装顺序：全局选项在前 → 当前集 MRL（其后紧跟其音频 input-slave）
        # → 其余各集 MRL（音频非空则以 :input-slave= 紧跟其后）。
        args = [
            vlc, "--no-video-title-show", "--no-drop-late-frames",
            f"--network-caching={_caching}",
        ]
        # VLC HTTP 控制接口（App 内按键 → player_command 转发 VLC 命令）：
        # 随机空闲端口 + 随机密码，放全局选项区、任何 MRL 之前。basic auth
        # 用户名为空、密码为 --http-password 值（VLC 3.0.23 实测可用）。
        port = _pick_http_port()
        http_password = secrets.token_hex(8)
        args += [
            "--extraintf=http", "--http-host=127.0.0.1",
            f"--http-port={port}", f"--http-password={http_password}",
        ]
        args.append(play_url)
        if audio_url:
            args.append(f":input-slave={audio_url}")
        if episodes:
            for ep_url, ep_audio, _ep_title in episodes[1:]:
                if not ep_url:
                    continue  # 该集缺流：跳过，不影响其余集
                ep_play, ep_audio_play = _resolve(ep_url, ep_audio)
                args.append(ep_play)
                if ep_audio_play:
                    args.append(f":input-slave={ep_audio_play}")
        # 新开播放器前先关掉上一次拉起的实例（避免多 VLC 窗口堆积）——
        # 旧进程可能已自行退出，terminate 幂等包裹，不影响新实例拉起。
        _terminate_previous()
        try:
            from .subprocess_no_window import no_window_kwargs

            _last_proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, **no_window_kwargs()  # Windows 静默，不弹控制台窗口
            )
            _control_state = {
                "host": "127.0.0.1", "port": port, "password": http_password,
            }
            return "已用外部播放器打开"
        except Exception:  # noqa: BLE001 —— VLC 启动失败降级系统默认
            pass
    webbrowser.open(url)
    return "已在浏览器中打开"


def player_command(command: str, val: str = "") -> bool:
    """向最近一次拉起的 VLC 发送 HTTP 控制命令。

    无控制会话（浏览器 fallback / 启动失败 / 播放器已关 / requests 缺失）
    或网络失败一律静默返回 False；成功（HTTP 2xx/3xx）返回 True。GUI 键盘
    事件直接转发成 VLC 命令（pl_play / pl_pause / pl_stop / pl_next /
    pl_prev / seek +30 / volume +10…），命令名或取值由调用方给出。
    """
    state = _control_state
    if not state or requests is None:
        return False
    params = {"command": command}
    if val:
        params["val"] = val
    try:
        r = requests.get(
            f"http://127.0.0.1:{state['port']}/requests/status.xml",
            params=params, auth=("", state["password"]), timeout=2,
        )
        return r.status_code < 400
    except Exception:  # noqa: BLE001 —— 网络失败/端口未起：静默 False
        return False
