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

与代理的两处协作：
- **租约**：VLC 存活期间持有 media_proxy 租约，空闲看门狗就不回收代理、
  不清 token（否则暂停过久恢复即 404，300 项的列表更是必然过期）。
- **启动握手**：非首集开播时带 --no-playlist-autostart，后台轮询
  playlist.json 建 {集下标: vlc_id} 映射并 pl_play 定位到起始集。
"""
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import webbrowser

try:
    import requests
except ImportError:  # pragma: no cover —— VLC 控制接口非必需，缺库时 player_command 静默 False
    requests = None

from .media_proxy import proxy_url_for

log = logging.getLogger(__name__)

# 系列播放列表的裁剪上限。Windows CreateProcess 命令行硬上限 32767 字符；
# 集数上千时一次性 enqueue 数千项既撑爆命令行也给 VLC 自身 playlist 增压。
# 正常路径仍按 1..N 全集入列（当前集在列内中段，由启动握手定位）；
# 仅当全集超这两项上限时，才降级为「当前集往后」的窗口。
_SERIES_MAX_MRL = 300
_SERIES_MAX_CMD = 30000

# 启动握手：等 VLC 建好播放列表（读 playlist.json）并按 uri 匹配起始项。
# 超时/解析失败即降级为 autostart 播第 1 集——不致命。
_HANDSHAKE_TIMEOUT = 3.0
_HANDSHAKE_INTERVAL = 0.1

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

# 当前 VLC 会话持有的 media_proxy 租约 id（"" = 无）。租约让空闲看门狗在
# VLC 存活期间不回收代理、不清 token（否则暂停超 _IDLE_TIMEOUT 后恢复即
# 404；全集列表一次预铸 300 个 token，用户几分钟后才点某一集更是必然过期）。
#
# 不变式：**任一时刻最多持有一个租约**。取在铸任何代理 URL 之前（open_with_player
# 内），放只有三条：① 拉起成功后由 _watch_proc 看着进程、进程退出即释放；
# ② 拉起失败（降级浏览器）当场释放；③ 铸 URL 阶段抛异常当场释放后上抛。
# 另有一条 _terminate_previous（换源/重开播放器）先释放旧租约再取新的。
_lease_id = ""


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
    播放列表 id 映射同样对旧实例失效；并释放旧实例的代理租约。
    """
    global _last_proc, _control_state, _playlist_items
    proc, _last_proc = _last_proc, None
    _control_state = None
    _playlist_items = []
    _drop_lease()      # 旧租约先释放：换源/重开不能让旧 id 残留成永久租约
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
    """带显示标题的 MRL（URL#[title]）。

    URL 里的字面 # 必须先砍掉：MRL 在第一个 # 处被 VLC 截断，余下片段会被当
    成 title/chapter 解析——**实际请求的资源随之改变**（VLC 拿片段当路径或时间
    偏移），表现为播错/播不出。标题由 _sanitize_title 追加在末尾。
    """
    base = (url or "").split("#", 1)[0]
    return f"{base}#{_sanitize_title(title, idx)}"


def _fit_series(episodes: list, start_idx: int, resolve,
                begin: int = 0) -> tuple[list[tuple[int, str]], bool]:
    """裁剪系列列表，返回 ([(集下标, MRL), ...], 是否截断)。

    **正常路径：全集按第 1 集 → 最后一集严格顺序入列。** 当前集往往位于
    列表中段——正因如此才需要启动握手：按 MRL 匹配到当前集的 vlc_id 后
    `pl_play&id` 定位。若这里只取「当前集往后」的窗口，当前集恒为首项，
    握手就失去意义，且第 1..start_idx-1 集在 VLC 里再也选不到。

    仅当全集超出 _SERIES_MAX_MRL 条数或 _SERIES_MAX_CMD 总字符数时，才降级为
    「从当前集往后」的窗口（此时当前集恒为窗口首项，握手依然找得到）。

    resolve(url) -> play_url：非本机代理 URL 才经它包一层代理
    （惰性 /e/ URL 原样使用）。协议里**没有 audio 形参**——系列路径丢弃各集
    音频轨（单集路径的 audio/input-slave 不受影响），留着它只会让人误以为
    音频被用上了。**先解析再计长**——代理 URL 比原 URL 长，先计长会低估
    命令行占用。空 URL 的集整条跳过。至少保留 1 条（单条超长也不丢，
    保证「当前集能播」优先于命令行长度）。

    begin episodes 在整表里的**起始集位**（降级重取窗口时传切片起点，默认 0）。
    返回的集下标 = begin + 切片内位置，使集号兜底标题与握手下标始终同源。
    """
    def _mrl_of(idx: int, entry) -> str:
        # idx 是**原始集位**（不是切片位置）：集号兜底标题「第{idx+1}集」与
        # items 传给握手的下标必须同源，否则降级重取窗口会重编号。
        ep_play = entry[0] if _is_local_proxy_url(entry[0]) else resolve(entry[0])
        return _mrl_with_title(ep_play,
                               entry[2] if len(entry) > 2 else "", idx)

    items: list[tuple[int, str]] = []
    used = 0
    truncated = False
    for idx, entry in enumerate(episodes, start=begin):
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
        # 窗口，否则当前集根本不在列表里、无法播放。begin=start_idx 让切片内
        # 的 idx 保持原始集位（集号兜底标题不重新从「第1集」起数），
        # 故返回的集下标已是绝对值，**不可再叠加 start_idx**。
        sub, _ = _fit_series(episodes[start_idx:], 0, resolve, begin=start_idx)
        return sub, True

    return items, truncated


def _locate_start(episodes: list, start_idx: int, url: str) -> int:
    """定开播集位（0-based）。

    三级优先：显式 start_idx（调用方已有下标，最可靠）→ url 命中项 → 记
    warning 退回第 1 集。每级失配都留 warning：从第 N 集开播却播第 1 集是
    用户可见的错位故障，静默降级过一次就没法排查了。
    """
    if 0 <= start_idx < len(episodes):
        return start_idx
    if start_idx != -1:
        log.warning("外部播放器：start_idx=%s 越界（共 %d 集）",
                    start_idx, len(episodes))
    hit = next((i for i, e in enumerate(episodes) if e and e[0] == url), -1)
    if hit >= 0:
        return hit
    log.warning("外部播放器：未能定位起始集（start_idx=%s，url 未命中列表），"
                "按第 1 集开播", start_idx)
    return 0


def open_with_player(url: str, audio: str = "", referer: str = "",
                     user_agent: str = "", headers: dict | None = None,
                     ad_block: dict | None = None,
                     force_proxy: bool = False,
                     episodes: list | None = None,
                     caching_ms: int = 0, classify_url: str = "",
                     start_idx: int = -1,
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
             整表加 `--no-random` 保证顺序；起始项非首项时加
             `--no-playlist-autostart` 并在后台握手 `pl_play&id=<id>` 定位。
             列表项 URL 为空则跳过该集；全部为空时退回单集路径。None → 与旧
             行为完全一致。
    start_idx 调用方已知的当前集下标（0-based）。**优先于** url 字符串匹配：
             调用方手里就有下标，比在列表里比对 URL 载荷可靠得多，也省掉
             「URL 被截断/签名变化就匹配不上」的脆弱环节。越界记 warning 并
             退回 url 命中项；两者都取不到时记 warning 退回第 1 集。
             单集路径忽略此参数。
    classify_url 非空时用它做缓冲分类。惰性系列 URL 是
             http://127.0.0.1:PORT/e/... 分类不出 HLS，必须传**当前集真实流
             地址**，否则按连接限速的源（ikanpp 需 30000ms）缓冲退化卡顿。
    on_playlist_ready 握手线程拿到 {集下标: vlc_id} 映射后的回调（后台线程
             执行，调用方须自行跨线程）。失败/超时不调用。

    附加能力：VLC 分支启动时附带 HTTP 控制接口（--extraintf=http），GUI
    键盘事件可经 player_command() 转成 VLC 控制命令（播放/暂停/进退等）。
    """
    global _last_proc, _control_state, _lease_id
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

        # 新开播放器前先关掉上一次拉起的实例（避免多 VLC 窗口堆积）——旧进程
        # 可能已自行退出，terminate 幂等包裹，不影响新实例拉起。放在铸 URL
        # **之前**：它顺带释放旧实例的租约，顺序必须是「先释放旧的、再取新的」，
        # 否则旧 id 被新 id 覆盖后无人释放 → 永久租约。
        _terminate_previous()
        # 代理租约必须在**铸造任何代理 URL 之前**取：acquire_lease() 不保证
        # 代理已起，不能当作「代理在线且受保护」的断言；反过来先铸 token 再取
        # 租约，中间那几毫秒里空闲看门狗可能正好判空闲 → stop() →
        # _tokens.clear() → 整列 MRL 全部 404（全集一次预铸 300 个 token）。
        _lease_id = _acquire_proxy_lease()
        try:
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
            start_pos = 0          # 实际开播集位：单集路径无列表可定位，恒为首项
            truncated = False
            if episodes:
                # 系列：全集按序入列（episodes[0] 也在列内），不另加主 MRL。
                # 起始项三级解析：显式 start_idx → url 命中 → 记 warning 退第 1 集。
                start_pos = _locate_start(episodes, start_idx, url)
                # 系列每集只解析**流地址**：系列路径丢弃 entry[1]（merged 流另挂
                # :input-slave 会黑屏，见设计 §7），resolve 协议里也就没有 audio
                # 形参；原先多解析一次就白铸一个没人用的代理 token。
                # 主路径的音频轨仍照常解析。
                window, truncated = _fit_series(
                    episodes, start_pos, lambda u: _resolve(u, "")[0])
                if window:
                    args.append("--no-random")
                    if start_pos > 0:
                        # 非首项开播：先禁止 autostart，再由握手 pl_play 定位
                        args.append("--no-playlist-autostart")
                    for _idx, mrl in window:
                        args.append(mrl)
                else:
                    # 每集都缺流地址（解析全空）→ 等同于没有系列，退回单集路径。
                    # 绝不能拿 0 条 MRL 拉起 VLC：那会开一个空播放器并回报成功。
                    args.append(play_url)
                    if audio_url:
                        args.append(f":input-slave={audio_url}")
            else:
                args.append(play_url)
                if audio_url:
                    args.append(f":input-slave={audio_url}")
        except Exception:  # noqa: BLE001
            # 铸 URL / 组装命令阶段就炸了：Popen 还没跑，没有进程会替我们释放
            # 租约 → 当场释放。异常**照旧上抛**（老行为如此）：不吞、不降级
            # 浏览器——那会把真实故障伪装成「已在浏览器中打开」。
            _drop_lease()
            raise
        try:
            from .subprocess_no_window import no_window_kwargs

            _last_proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, **no_window_kwargs()  # Windows 静默，不弹控制台窗口
            )
            _control_state = {
                "host": "127.0.0.1", "port": port, "password": http_password,
            }
        except Exception:  # noqa: BLE001 —— VLC 启动失败降级系统默认
            # 同样没有进程存活 → 租约当场释放，不留永久租约。
            _drop_lease()
        else:
            # 看守线程：阻塞等播放器进程退出（用户关窗口/播完）即释放租约。
            # 这是「用户手动关闭」这条路径唯一的释放机会，漏了就是永久租约。
            threading.Thread(target=_watch_proc, args=(_last_proc, _lease_id),
                             daemon=True).start()
            # 握手**故意在启动 try 之外**：VLC 已拉起并开播，握手失败只是
            # 少一次 pl_play 定位，**不得**降级成浏览器兜底（那会多弹一个
            # 浏览器窗口、返回「已在浏览器中打开」，把故障现象伪装成成功）。
            if episodes and window:
                # 后台握手：轮询 playlist.json 建 {集下标: vlc_id} 映射，
                # 非首项开播时顺带 pl_play 定位。必须在后台线程（VLC 建列表
                # 需时，主线程会卡 UI）。整个 window 传下去，映射才覆盖
                # 列表内每一集（App 侧切集依赖它）。
                try:
                    _start_playlist_sync(start_pos, window, on_playlist_ready)
                except Exception as exc:  # noqa: BLE001
                    # exc_info=True：这条 except 就是为诊断握手失败而存在的，
                    # 光有 str(exc) 丢掉堆栈——而「缺 Task 5 符号的 NameError」
                    # 「签名对不上」这类病因恰恰只在堆栈里。
                    log.warning("外部播放器：启动握手失败（不影响已拉起的 VLC）: %s",
                                exc, exc_info=True)
            if truncated:
                return f"已用外部播放器打开（列表已截断，共 {len(window)} 集）"
            return "已用外部播放器打开"
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


# ---------------------------------------------------------------------- #
# 代理租约：VLC 存活期间阻止空闲看门狗回收
# ---------------------------------------------------------------------- #
def _acquire_proxy_lease() -> str:
    """向 media_proxy 登记租约（代理不可用返回 ""，静默降级）。

    租约本身**不保证代理已起**（acquire_lease() 不碰 _ensure_server），
    只承诺「有租约期间空闲看门狗不会 stop()、不会清 token」。因此必须在
    铸造代理 URL **之前**调用，见 open_with_player。
    """
    try:
        from .media_proxy import MediaProxy

        return MediaProxy.instance().acquire_lease()
    except Exception:  # noqa: BLE001 —— 代理未起/异常：租约非必需
        return ""


def _release_proxy_lease(lease_id: str) -> None:
    """释放租约（幂等；空 id 直接返回）。"""
    if not lease_id:
        return
    try:
        from .media_proxy import MediaProxy

        MediaProxy.instance().release_lease(lease_id)
    except Exception:  # noqa: BLE001
        pass


def _drop_lease() -> None:
    """释放模块级当前租约并置空（幂等；本就没有租约时什么都不做）。

    「拉起失败」这类没有进程接手的路径必须用它收尾——租约没有 TTL、没有
    持有者存活检查，漏一次释放就让空闲看门狗在整个 App 会话内永久失效。
    """
    global _lease_id
    lid, _lease_id = _lease_id, ""
    if lid:                     # 空 id 压根不走进释放路径（无租约可放）
        _release_proxy_lease(lid)


def _watch_proc(proc, lease_id: str) -> None:
    """阻塞等播放器退出，退出即释放租约（用户手动关窗口的路径）。

    释放放在 wait 的异常处理**之外**：wait 一抛就跳过释放 = 永久租约。
    只在 _lease_id 仍是自己时才置空模块状态——旧实例的看守醒来时新会话可能
    已经登记了自己的租约，顺手清掉等于换源后立刻失去看门狗保护。
    """
    global _lease_id
    try:
        proc.wait()
    except Exception:  # noqa: BLE001 —— 句柄失效：仍需释放
        pass
    _release_proxy_lease(lease_id)
    if _lease_id == lease_id:
        _lease_id = ""


# ---------------------------------------------------------------------- #
# 启动握手：读 playlist.json 建 {集下标: vlc_id} 映射 + 定位起始集
# ---------------------------------------------------------------------- #
def _item_matches(item: dict, mrl: str) -> bool:
    """playlist.json 的一项是否对应我们给的 MRL。

    mrl 必须是**已剥离 # 标题片段**的 MRL（_handshake_worker 传进来之前就
    剥好了）：VLC 报告的 uri 可能带 #title fragment、也可能不带，带与不带
    两种都算命中。比对用 `mrl + "#"` 而不是裸 startswith，否则第 3 集
    （.../k/3）会连第 30 集（.../k/30）一起命中。
    """
    uri = str((item or {}).get("uri") or "")
    if not uri:
        return False
    return uri == mrl or uri.startswith(mrl + "#")


def _start_playlist_sync(start_idx: int, series: list[tuple[int, str]],
                         on_playlist_ready=None) -> None:
    """后台起线程做握手（绝不阻塞调用方——GUI 主线程会卡 UI）。"""
    threading.Thread(
        target=_handshake_worker,
        args=(start_idx, series, on_playlist_ready),
        daemon=True,
    ).start()


def _handshake_worker(start_idx: int, series: list[tuple[int, str]],
                      on_playlist_ready=None) -> None:
    """轮询 playlist.json 直到起始项出现：建映射 +（非首集）pl_play 定位。

    series 是入列的 [(集下标, MRL), ...]（全集 1..N，或降级后的当前集往后窗口）。
    映射**按 uri 匹配**得出，不做下标推算：VLC 列表顺序未必等于入列顺序，
    且下标推算只在「窗口从当前集起」时成立——全集入列时会整体错位。
    超时即降级：VLC 按 --no-playlist-autostart 行为播放，App 侧映射留空 →
    切集回落到重开 VLC。

    起始集**未必在 series 里**（显式 start_idx 指向的集 URL 为空时会被
    _fit_series 跳过）→ 匹配不到就不发 pl_play，静默降级 autostart。
    """
    deadline = time.monotonic() + _HANDSHAKE_TIMEOUT
    bases = [(idx, mrl.split("#", 1)[0]) for idx, mrl in (series or [])]
    while True:
        items = player_playlist_items(timeout=1.0, refresh=True)
        if items:
            mapping: dict[int, int] = {}
            start_id = None
            for item in items:
                iid = item.get("id")
                if not isinstance(iid, int):
                    continue        # 假 id 进映射 → App 切集拿着它 pl_play 静默失败
                for idx, base in bases:
                    if _item_matches(item, base):
                        mapping[idx] = iid
                        if idx == start_idx:
                            start_id = iid
                        break
            if on_playlist_ready is not None and mapping:
                try:
                    on_playlist_ready(mapping)
                except Exception:  # noqa: BLE001 —— 回调异常不阻断定位
                    pass
            if start_id is not None:
                if start_idx > 0:
                    player_goto(start_id)   # 失败即降级 autostart，不重试
                return                      # 首集：原生 autostart 已对
        if time.monotonic() >= deadline:
            return
        time.sleep(_HANDSHAKE_INTERVAL)
