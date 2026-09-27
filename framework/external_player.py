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
import re
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

# 系列播放列表的裁剪上限。Windows CreateProcess 命令行硬上限 32767 字符；
# 集数上千时一次性 enqueue 数千项既撑爆命令行也给 VLC 自身 playlist 增压。
# 正常路径仍按 1..N 全集入列（当前集在列内中段，由启动握手定位）；
# 仅当全集超这两项上限时，才降级为「当前集往后」的窗口。
_SERIES_MAX_MRL = 300
_SERIES_MAX_CMD = 30000

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

# 最近一次 VLC 会话的播放列表快照（playlist.json 解析结果）。握手线程填它，
# video_view 据此建立 {集下标: vlc_id} 映射。_terminate_previous 清空：
# 旧会话的 id 对新会话无意义，留着会让 App 切集串台。
_playlist_items: list[dict] = []


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
    （用户手动关闭/播放结束），terminate 一律 try/except
    包裹，不抛异常、不干扰本次拉起新版。同时清空 _control_state 与
    _playlist_items：旧实例被关后其 HTTP 控制会话即失效（换集重开会重建），
    播放列表 id 映射同样对旧实例失效。
    """
    global _last_proc, _control_state, _playlist_items
    proc, _last_proc = _last_proc, None
    _control_state = None
    _playlist_items = []
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


def _is_local_proxy_url(target: str) -> bool:
    """是否本机 media_proxy 的 URL（/s/<token> 或 /e/<key>/<idx>）。

    惰性系列每集 URL 本就是代理地址，**不能再包一层代理**（否则多一跳转发
    + 双重广告过滤）。VLC 只访问回环，故按前缀判定即可。
    """
    t = (target or "").lower()
    return t.startswith("http://127.0.0.1:") or t.startswith("http://localhost:")


def _sanitize_title(title: str, idx: int = 0) -> str:
    """MRL #title 片段消毒。

    - 剥掉 #（会截断 MRL）、\\r\\n\\t（破坏 argv 解析）
    - 折叠连续空白
    - 空 → 「第{idx+1}集」
    - **首字符是数字时加「集」前缀**：VLC 的 fragment 若匹配
      mrl-title=DIGIT*DIGIT 会被解释为**时间偏移**（跳到第 N 秒）而非标题
    """
    t = re.sub(r"[#\r\n\t]+", " ", str(title or ""))
    t = " ".join(t.split()).strip()
    if not t:
        t = f"第{idx + 1}集"
    if t[0].isdigit():
        t = "集" + t
    return t


def _mrl_with_title(url: str, title: str, idx: int = 0) -> str:
    """带显示标题的 MRL（URL#[title]）。"""
    return f"{url}#{_sanitize_title(title, idx)}"


def _fit_series(episodes: list, start_idx: int,
                resolve) -> tuple[list[tuple[int, str]], bool]:
    """裁剪系列列表，返回 ([(集下标, MRL), ...], 是否截断)。

    **正常路径：全集按第 1 集 → 最后一集严格顺序入列。** 当前集往往位于
    列表中段——正因如此才需要启动握手：按 MRL 匹配到当前集的 vlc_id 后
    `pl_play&id` 定位。若这里只取「当前集往后」的窗口，当前集恒为首项，
    握手就失去意义，且第 1..start_idx-1 集在 VLC 里再也选不到。

    仅当全集超出 _SERIES_MAX_MRL 条数或 _SERIES_MAX_CMD 总字符数时，才降级为
    「从当前集往后」的窗口（此时当前集恒为窗口首项，握手依然找得到）。

    resolve(url, audio) -> play_url：非本机代理 URL 才经它包一层代理
    （惰性 /e/ URL 原样使用）。**先解析再计长**——代理 URL 比原 URL 长，
    先计长会低估命令行占用。空 URL 的集整条跳过。至少保留 1 条
    （单条超长也不丢，保证「当前集能播」优先于命令行长度）。
    """
    def _mrl_of(idx: int, entry) -> str:
        ep_play = entry[0] if _is_local_proxy_url(entry[0]) else resolve(
            entry[0], entry[1] if len(entry) > 1 else "")
        return _mrl_with_title(ep_play,
                               entry[2] if len(entry) > 2 else "", idx)

    items: list[tuple[int, str]] = []
    used = 0
    truncated = False
    for idx, entry in enumerate(episodes):
        if not entry or not entry[0]:
            continue
        mrl = _mrl_of(idx, entry)
        if items and (len(items) >= _SERIES_MAX_MRL
                      or used + len(mrl) > _SERIES_MAX_CMD):
            truncated = True
            break
        items.append((idx, mrl))
        used += len(mrl)

    if truncated and start_idx > 0:
        # 全集装不下 → 降级为「当前集往后」的窗口。items 自身已按两个上限
        # 截断，故其下标 >= start_idx 的后缀必然仍满足上限，无需重新解析。
        window = [(i, m) for i, m in items if i >= start_idx]
        if window:
            return window, True
        # 当前集落在已解析范围之外（超长剧集选了很靠后的集）→ 必须重新取
        # 窗口，否则当前集根本不在列表里、无法播放。
        sub, _ = _fit_series(episodes[start_idx:], 0, resolve)
        return [(i + start_idx, m) for i, m in sub], True

    return items, truncated


def open_with_player(url: str, audio: str = "", referer: str = "",
                     user_agent: str = "", headers: dict | None = None,
                     ad_block: dict | None = None,
                     force_proxy: bool = False,
                     episodes: list | None = None,
                     caching_ms: int = 0, classify_url: str = "",
                     on_playlist_ready=None) -> str:
    """用外部播放器打开媒体地址。

    url      媒体直链（单流）。episodes 非空时它只用于**定位起始项**与分类，
             实际入列的 MRL 全部来自 episodes。
    audio    DASH 音频轨地址（非空时以 input-slave 挂入；系列路径忽略）
    referer / user_agent / headers  防盗链透传。referer/user_agent 是兼容旧
             调用的便捷参数；headers 提供完整头（含 Cookie 等）。任何防盗链
             头存在时走本地代理（VLC 无法设置 UA，只有代理能根治）。HLS 流
             一律走本地代理（含广告段过滤），即使无防盗链头也让代理剔除广告段。
    ad_block 源 ad_block 配置，非空时代理转发 m3u8 会剔除广告段。
    force_proxy 代理转发时强制走系统代理会话（跳过直连探测）。
    episodes **全集完整有序播放列表** list[tuple[url, audio, title]]：
             episodes[i] = 第 i 集（0-based，严格播放顺序），**episodes[0]
             不会被跳过**。每条 MRL 追加 `#<消毒后的标题>` 供播放列表显示；
             整表加 `--no-random` 保证顺序；起始项（url 命中项）非首项时加
             `--no-playlist-autostart` 并在后台握手 `pl_play&id=<id>` 定位。
             列表项 URL 为空则跳过该集。None → 与旧行为完全一致。
    classify_url 非空时用它做缓冲分类。惰性系列 URL 是
             http://127.0.0.1:PORT/e/... 分类不出 HLS，必须传**当前集真实流
             地址**，否则按连接限速的源（ikanpp 需 30000ms）缓冲退化卡顿。
    on_playlist_ready 握手线程拿到 {集下标: vlc_id} 映射后的回调（后台线程
             执行，调用方须自行跨线程）。失败/超时不调用。

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

        if _is_local_proxy_url(url):
            # 惰性系列：url 已是代理 URL，不能再包一层
            play_url, audio_url = url, ""
        else:
            play_url, audio_url = _resolve(url, audio)
        # 缓冲调优：按媒体类型给 VLC 设 network-caching（HLS 分片流网络抖动
        # 敏感，慢 CDN 每片 1-2s 时默认 300ms 缓冲会频繁卡顿/加载慢）。
        # 复用 media_tuner.classify 的缓冲画像，缺省 HLS 5000ms 抗慢 CDN。
        # 注意：类型要用**原始媒体 URL** 判定——play_url 走本地代理后是
        # http://127.0.0.1:/s/token，不含 .m3u8/.mp4 特征，会被判成 unknown
        # 拿 2500ms，走代理的慢 HLS 反而缓冲更小（播放卡顿的根因之一）。
        try:
            from .media_tuner import classify as _classify
            # 分类对象：系列路径必须用**真实流地址**（惰性 /e/ URL 判不出类型）
            _profile = _classify(classify_url or url)
            _caching = max(_profile.buffer_ms, 5000) if _profile.kind == "hls" else _profile.buffer_ms
            # 经本地代理转发（防盗链头）多一跳、更抖，缓冲再加大抗卡顿
            if play_url != url or _is_local_proxy_url(url):
                _caching = max(_caching, 8000)
        except Exception:  # noqa: BLE001
            _caching = 5000
        # 按源覆盖（sources/<id>.json 的 media.hls.network_caching_ms）：CDN
        # **按连接限速**的源（实测 ikanpp 单连接 33~190KB/s，而实时播放需
        # ≥136KB/s）用默认 8s 缓冲必然反复卡死，需给到 20~30s 扛住带宽缺口。
        # 只按源开（其余源仍是 8s），避免给本来正常的源白加启动延迟。
        # 取 max() 当下限用：配小了也不会把分类算出的更大值降级。
        if caching_ms and int(caching_ms) > 0:
            _caching = max(_caching, int(caching_ms))
        # 播放处理（调研 VLC 流播放调优）：除加大网络缓冲外，加 --no-drop-late-frames
        # 让 VLC 不丢晚到的帧（默认丢帧会表现为画面卡顿跳动）；不强制硬件解码——
        # DXVA2/D3D11VA 的 copy-back 开销在某些机器上反而更卡，交给 VLC 自动判断。
        # 组装顺序：全局选项在前 → 单集路径追加主 MRL（音频非空则紧跟其后
        # 的 :input-slave=）→ 系列路径改为全集按 1..N 顺序追加，不另加主 MRL。
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
        window: list[tuple[int, str]] = []
        start_idx = 0
        truncated = False
        if episodes:
            # 系列：全集按序入列（episodes[0] 也在列内），不另加主 MRL。
            # 起始项 = url 命中的集；取不到（url 不在列表内）按 0 处理。
            start_idx = next(
                (i for i, e in enumerate(episodes) if e and e[0] == url), 0)
            args.append("--no-random")
            if start_idx > 0:
                # 非首项开播：先禁止 autostart，再由握手 pl_play 定位
                args.append("--no-playlist-autostart")
            window, truncated = _fit_series(episodes, start_idx,
                                           lambda u, a: _resolve(u, a)[0])
            for _idx, mrl in window:
                args.append(mrl)
        else:
            args.append(play_url)
            if audio_url:
                args.append(f":input-slave={audio_url}")
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
            if episodes and window:
                # 后台握手：轮询 playlist.json 建 {集下标: vlc_id} 映射，
                # 非首项开播时顺带 pl_play 定位。必须在后台线程（VLC 建列表
                # 需时，主线程会卡 UI）。整个 window 传下去，映射才覆盖
                # 列表内每一集（App 侧切集依赖它）。
                _start_playlist_sync(start_idx, window, on_playlist_ready)
            if truncated:
                return f"已用外部播放器打开（列表已截断，共 {len(window)} 集）"
            return "已用外部播放器打开"
        except Exception:  # noqa: BLE001 —— VLC 启动失败降级系统默认
            pass
    webbrowser.open(url)
    return "已在浏览器中打开"


def player_command(command: str, val: str = "", item_id=None) -> bool:
    """向最近一次拉起的 VLC 发送 HTTP 控制命令。

    无控制会话（浏览器 fallback / 启动失败 / 播放器已关 / requests 缺失）
    或网络失败一律静默返回 False；成功（HTTP 2xx/3xx）返回 True。GUI 键盘
    事件直接转发成 VLC 命令（pl_play / pl_pause / pl_stop / pl_next /
    pl_previous / seek +30 / volume +10…），命令名或取值由调用方给出。

    item_id 用于需要播放列表项 id 的命令（pl_play / pl_delete）——VLC 的
    pl_play 取值是**播放列表项 id**（非下标），必须先读 playlist.json 建映射。
    """
    state = _control_state
    if not state or requests is None:
        return False
    params = {"command": command}
    if val:
        params["val"] = val
    if item_id is not None:
        params["id"] = item_id
    try:
        r = requests.get(
            f"http://127.0.0.1:{state['port']}/requests/status.xml",
            params=params, auth=("", state["password"]), timeout=2,
        )
        return r.status_code < 400
    except Exception:  # noqa: BLE001 —— 网络失败/端口未起：静默 False
        return False


def player_playlist_items(timeout: float = 2.0,
                          refresh: bool = False) -> list[dict]:
    """读最近一次 VLC 会话的完整播放列表（GET /requests/playlist.json）。

    返回 [{id, name, uri, current}, ...]；无控制会话 / 网络失败 / 状态码
    >=400 / 响应非 list 一律返回 []。**失败结果不缓存**（下次调用重试）。
    refresh=True 强制重新拉（握手轮询 VLC 尚未建好列表时需要）。
    """
    global _playlist_items
    if _playlist_items and not refresh:
        return _playlist_items
    state = _control_state
    if not state or requests is None:
        return []
    try:
        r = requests.get(
            f"http://127.0.0.1:{state['port']}/requests/playlist.json",
            auth=("", state["password"]), timeout=timeout,
        )
        if r.status_code >= 400:
            return []
        items = r.json()
    except Exception:  # noqa: BLE001 —— 网络失败/未就绪/非 JSON：静默 []
        return []
    if not isinstance(items, list):
        return []
    _playlist_items = [it for it in items if isinstance(it, dict)]
    return _playlist_items


def player_goto(item_id: int) -> bool:
    """跳播播放列表项 id（VLC pl_play 的 id 是项 id，非下标）。"""
    return player_command("pl_play", item_id=item_id)


def player_next() -> bool:
    """下一项（VLC 内置 N）。"""
    return player_command("pl_next")


def player_previous() -> bool:
    """上一项（VLC 内置 P）。命令名是 pl_previous——**没有 pl_prev**。"""
    return player_command("pl_previous")


def player_running() -> bool:
    """最近拉起的播放器进程是否仍在运行（App 据此决定能否指挥 VLC 切集）。"""
    proc = _last_proc
    if proc is None:
        return False
    try:
        return proc.poll() is None
    except Exception:  # noqa: BLE001 —— 句柄失效：视为未运行
        return False
