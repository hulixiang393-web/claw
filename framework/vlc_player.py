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
_INSTANCE_LOCK = threading.Lock()  # 预热/首播可能并发创建，防竞态
# 本地代理转发用 requests Session（keep-alive）：HLS 分片逐段请求复用连接，
# 避免每段一次 TCP+TLS 握手（本地代理播放加速关键）。
_PROXY_SESSION = None
_PROXY_SESSION_LOCK = threading.Lock()
# 系统代理（Clash）探测结果缓存：命中后复用，避免每次播放都重复 socket 探测
_DETECTED_PROXY = None


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


def _detect_system_proxy():
    """探测系统代理（Clash 等）：环境变量优先，其次常见 Clash 默认端口。

    环境变量读 HTTPS_PROXY/https_proxy/HTTP_PROXY/http_proxy；未配置时
    快速 socket 探测 7890/7897/10809 常见 Clash 端口，取第一个开放的。
    探测不到返回 None。探测结果命中后缓存（_DETECTED_PROXY），避免每次
    播放都重复 socket 探测；未命中不缓存（Clash 可能在 app 启动后开启）。
    """
    global _DETECTED_PROXY
    if _DETECTED_PROXY is not None:
        return _DETECTED_PROXY
    result = None
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var)
        if val:
            result = val
            break
    if result is None:
        import socket

        for port in (7890, 7897, 10809):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                    result = f"http://127.0.0.1:{port}"
                    break
            except OSError:
                continue
    if result:
        _DETECTED_PROXY = result
    return result


def _get_instance():
    """模块级单例 vlc.Instance（内嵌去标题、网络缓冲）。

    加锁防竞态：App 启动预热（后台线程）与首次播放可能同时触发创建。
    注意：**不能加 --http-proxy**——VLC 会连本地代理（VlcStreamProxy 127.0.0.1）
    都走 Clash 转发，Clash 拒绝/无法代理 localhost → 看门狗救回路径全断
    （实测 avgood 报 `cannot connect to 127.0.0.1:7890`）。直连不通的源
    （YouTube 被墙等）由看门狗救回为本地代理转发（VlcStreamProxy 内按源
    探测走直连或 Clash），不再依赖 VLC 侧全局代理。
    """
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
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


def warmup_vlc() -> None:
    """App 启动预热 VLC：提前 import vlc + 创建共享 vlc.Instance。

    python-vlc 首次 import + 加载 libvlc + 建 Instance 是主要耗时（约 1-2s），
    预热后首次播放不再现场加载，秒开。失败静默（懒加载兜底不受影响）。
    调用方可放后台线程，不阻塞启动。
    """
    try:
        _get_instance()
    except Exception:  # noqa: BLE001
        pass


def shutdown_vlc() -> None:
    """App 退出时释放共享实例（必须先 close 所有 player）。"""
    global _INSTANCE
    if _INSTANCE is not None:
        try:
            _INSTANCE.release()
        except Exception:  # noqa: BLE001
            pass
        _INSTANCE = None


def _get_proxy_session():
    """模块级单例 requests.Session（本地代理转发复用连接，keep-alive 提速）。

    统一直连（trust_env=False，不依赖环境变量）：是否走系统代理（Clash）
    由 VlcStreamProxy 构造时按源探测决定（直连可达走直连，直连不通如 YouTube
    被墙才走 Clash）——避免对 avgood 等直连可达的 CDN 强制走 Clash 反而被
    代理出口 IP 拒绝（用户实测 avgood 强制走 Clash 后 VLC 出错）。
    """
    global _PROXY_SESSION
    if _PROXY_SESSION is None:
        with _PROXY_SESSION_LOCK:
            if _PROXY_SESSION is None:
                import requests

                _PROXY_SESSION = requests.Session()
                _PROXY_SESSION.trust_env = False  # 直连，代理由按源探测决定
                try:
                    from requests.adapters import HTTPAdapter

                    _PROXY_SESSION.mount(
                        "http://", HTTPAdapter(pool_connections=16, pool_maxsize=64)
                    )
                    _PROXY_SESSION.mount(
                        "https://", HTTPAdapter(pool_connections=16, pool_maxsize=64)
                    )
                except Exception:  # noqa: BLE001
                    pass
    return _PROXY_SESSION


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
        headers = dict(t["headers"])
        # 透传客户端 Range 头：VLC 渐进播放 MP4 依赖字节范围请求（moov/索引），
        # 不带 Range 上游回 200 全量 → VLC 无法建 chunks 索引（mp4 demux 失败）
        range_h = self.headers.get("Range")
        if range_h:
            headers["Range"] = range_h
        kw = {}
        if t.get("use_clash"):
            # 直连不通的源（如 YouTube 被墙）：转发时走系统代理（Clash）
            p = _detect_system_proxy()
            if p:
                kw["proxies"] = {"http": p, "https": p}
        try:
            r = _get_proxy_session().get(real, headers=headers, timeout=120, stream=True, **kw)
        except Exception:  # noqa: BLE001
            self.send_response(502)
            self.end_headers()
            return
        ctype = (r.headers.get("Content-Type") or "").lower()
        first = next(r.iter_content(65536), b"")
        is_hls = (
            "m3u8" in ctype
            or real.split("?", 1)[0].lower().endswith(".m3u8")
            or first[:8] == b"#EXTM3U"
        )
        if is_hls:
            # HLS 小清单：整读 → 重写分片为本地代理路径（VLC 只连 127.0.0.1）
            parts = [first]
            for chunk in r.iter_content(65536):
                if not chunk:
                    break
                parts.append(chunk)
            body = b"".join(parts)
            r.close()
            if r.status_code != 200:
                self.send_response(r.status_code)
                self.end_headers()
                return
            text = body.decode("utf-8", "replace")
            if text.lstrip().startswith("#EXTM3U"):
                text = self._rewrite(text, t, real)
                body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:  # noqa: BLE001
                pass
            return
        # 音视频媒体（mp4/m4s/webm）：流式转发，透传状态码与范围响应头
        # （206 + Content-Range/Accept-Ranges，支持 VLC 拖动/渐进播放）
        self.send_response(r.status_code)
        for hk in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            hv = r.headers.get(hk)
            if hv:
                self.send_header(hk, hv)
        self.end_headers()
        try:
            if first:
                self.wfile.write(first)
            for chunk in r.iter_content(65536):
                if not chunk:
                    break
                self.wfile.write(chunk)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass

    def _rewrite(self, text: str, t: dict, real: str = "") -> str:
        """m3u8 里非注释行（分片/子清单 URL）全部改写为本地代理路径。

        real 为当前 m3u8 的真实上游 URL：相对分片以它做基准拼接
        （urljoin 取到 m3u8 所在目录）。不能直接用 t["base"]——那是站点根
        scheme://netloc，深目录 m3u8（avgood /remote_m3u8/.../ts/index.m3u8、
        missav 360p/video.m3u8 子清单等）的相对分片会拼到站点根假路径 → 404。
        绝对分片 URL（http 开头）urljoin 原样返回，不受影响；根相对 `/...`
        仍落在站点根，行为不变。
        """
        pb = t["proxy"]
        base = real or t["base"]
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                out.append(line)
                continue
            out.append(pb + "/p/" + urljoin(base, line))
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
        # 按源探测：直连 base 可达则走直连；直连不通（被墙/拒绝）才走系统代理
        # （Clash）。avgood 等直连可达的 CDN 强制走 Clash 会被代理出口 IP 拒绝
        # → VLC 出错；YouTube 直连被墙则必须走 Clash。
        # 探测放后台线程：慢 CDN（如 hanime1 的 vdownload）TCP/HTTP 探测可达数秒，
        # 同步探测会阻塞主线程（看门狗救回路径冻结 6-8s）并推迟代理开播。
        # 先按直连（use_clash=False）启动，探测完成后由 _probe_direct_async 更新。
        self._srv.target = {
            "base": self._base,
            "headers": self._headers,
            "proxy": self._proxy_base,
            "use_clash": False,
        }
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        threading.Thread(
            target=self._probe_direct_async,
            args=(self._srv.target, self._base, ua),
            daemon=True,
        ).start()

    @staticmethod
    def _probe_direct(base_url: str, ua: str) -> bool:
        """直连 base 根路径是否可达（连接层成功即算可达，403/404 也算站点层可达）。"""
        try:
            r = _get_proxy_session().get(
                base_url.rstrip("/") + "/",
                headers={"User-Agent": ua},
                timeout=3,
                allow_redirects=False,
                stream=True,
            )
            r.close()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _probe_direct_async(self, target: dict, base_url: str, ua: str) -> None:
        """后台完成直连探测并更新 use_clash（不阻塞构造/播放启动）。

        _probe_direct 对慢 CDN（如 hanime1 的 vdownload）可能耗时数秒；
        构造时先按直连开播，探测完成后按结果翻转——直连不通的源
        （被墙/拒绝）后续请求自动切系统代理（Clash）。
        """
        use_clash = not self._probe_direct(base_url, ua)
        target["use_clash"] = use_clash

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
    buffering = Signal(int)  # 0~100；100 表示缓冲完成恢复播放


class VlcPlayer(QObject):
    """VLC 内嵌播放器（可内嵌到任意 QWidget）。"""

    # 供 VideoView 连接的信号（由 _bridge 转发，libvlc 线程安全）
    ended = Signal()
    error = Signal(str)
    time_changed = Signal(int)
    length_changed = Signal(int)
    buffering = Signal(int)  # 缓冲百分比 0~100（100=恢复播放）
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
        self._current_audio = ""  # 卡顿升级重播需还原 DASH 音频轨
        self._attached = False
        self._proxy = None  # 本地代理（TLS 兼容问题自动降级用）
        # 事件桥接（持引用防 GC，回调只 emit 信号）
        self._bridge = _VlcBridge()
        self._bridge.ended.connect(self.ended)
        self._bridge.error.connect(self.error)
        self._bridge.time_changed.connect(self.time_changed)
        self._bridge.length_changed.connect(self.length_changed)
        self._bridge.buffering.connect(self.buffering)
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
        if not w.isVisible():
            # 未显示时 winId() 会强制创建原生窗口，隐藏态可阻塞主线程（切页卡死）。
            # 由 _VideoFrame.showEvent → rehook 在可见时重挂。
            self._attached = False
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
        """播放。单流直接 set_mrl；DASH 双流用 input-slave 挂音频轨。

        直连优先，看门狗（_start_play_watchdog）在 VLC 直连卡 Opening/Error
        时救回为本地代理转发（_play_via_proxy，requests 拉流）。不做"主动
        代理"：对 avgood 等直连可达的 CDN 直接播最稳（强制走 Clash 会被代理
        出口 IP 拒绝），YouTube 等直连被墙的靠看门狗救回（按源探测走 Clash）。

        缓冲调优：按媒体类型（HLS/DASH/MP4/直播）经 MediaTuner.classify
        选择初始 network-caching（media 级覆盖实例级），见 media_tuner.py。
        """
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
        self._current_audio = audio_url

        from framework.media_tuner import classify

        profile = classify(video_url)
        # media 级网络缓存：对分片流（HLS/DASH）抗网络抖动，卡顿自愈时逐级提高
        opts = [f"network-caching={profile.buffer_ms}"]
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
                # HLS/MP4/单流：统一 media 路径（options 才能带 network-caching）
                self._media = _get_instance().media_new(video_url)
                for o in opts:
                    self._media.add_option(o)
                self._player.set_media(self._media)
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

            _t.sleep(2)
            try:
                s1 = self._player.get_state()
            except Exception:  # noqa: BLE001
                return
            _t.sleep(2)
            try:
                p = self._player
                if p is None or self._proxy is not None:
                    return
                if self._current_url != url:
                    return  # 已切换/释放到其他播放，旧看门狗失效（防切走后复活）
                s2 = p.get_state()
                # VLC state: 0=Nothing 1=Opening 2=Buffering 3=Playing
                # 4=Paused 5=Stopped 6=Ended 7=Error
                # 两次采样都"未进入播放进程"（非 Buffering/Playing/Paused/Ended）才触发。
                # 比原来的"两次都在 Opening/Error"更稳：hanime1 等 TLS 直连失败的
                # mp4 可能在 4s 内走到 Error(7) 后回落 Nothing(0)，s2 采样到 0 会被
                # 漏救回；仍在 Buffering/Playing 说明启动正常，不误杀慢启动的 DASH 双流。
                if s1 not in (2, 3, 4, 6) and s2 not in (2, 3, 4, 6):
                    # 调度回主线程：winId/set_hwnd 等 Qt 操作必须主线程，否则内嵌黑屏
                    self._proxy_retry.emit(url, audio_url)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_watch, daemon=True).start()

    def _play_via_proxy(self, url: str, audio_url: str) -> None:
        """用本地代理播放：requests/OpenSSL 拉流，VLC 播本地直连。

        代理用 VlcStreamProxy 转发（带源 Referer/UA；m3u8 分片重写相对路径；
        DASH 音频也走代理），绕开 libVLC 直连 CDN 的 gnutls TLS 不兼容 /
        被墙 / 海外镜像不可达。play() 主动路径与看门狗救回共用。
        """
        proxy = None
        try:
            # 重复降级/重试时先释放旧代理（防端口/线程泄漏）
            if self._proxy is not None:
                try:
                    self._proxy.close()
                except Exception:  # noqa: BLE001
                    pass
                self._proxy = None
            from urllib.parse import urlsplit

            parts = urlsplit(url)
            base = f"{parts.scheme}://{parts.netloc}"
            proxy = VlcStreamProxy(
                base, referer=self._referer, user_agent=self._user_agent
            )
            local = proxy.local(url)
            # 与 play() 相同的 media 级选项：referer/UA 对 input-slave 同样生效
            from framework.media_tuner import classify

            opts = [f"network-caching={classify(url).buffer_ms}"]
            if self._referer:
                opts.append("http-referrer=" + self._referer)
            if self._user_agent:
                opts.append("http-user-agent=" + self._user_agent)
            self.stop()
            self._proxy = proxy
            if audio_url:
                # DASH 双流：音频也走本地代理（带 referer/UA，防 CDN 403 卡 Opening）
                local_a = proxy.local(audio_url)
                self._media = _get_instance().media_new(local)
                self._media.add_option("input-slave=" + local_a)
                for o in opts:
                    self._media.add_option(o)
                self._player.set_media(self._media)
            else:
                self._player.set_mrl(local, *opts)
            self._attach_window()
            self._player.play()
        except Exception:  # noqa: BLE001
            if proxy is not None:
                try:
                    proxy.close()
                except Exception:  # noqa: BLE001
                    pass
            self._proxy = None

    def _retry_with_proxy(self, url: str, audio_url: str) -> None:
        """看门狗救回路径：直连卡 Opening/Error 后用本地代理重播。"""
        self._play_via_proxy(url, audio_url)

    def increase_buffer(self) -> bool:
        """卡顿自愈：按 MediaTuner 阶梯加大网络缓存并重播当前流。

        返回 True 表示已升级重播；False 表示已达缓存上限（或当前无流），
        调用方（UI 层）据此决定是否提示用户/换源。升级阶梯与封顶见
        framework/media_tuner.py。
        """
        url = self._current_url
        if not url:
            return False
        from framework.media_tuner import MediaTuner

        tuner = MediaTuner()
        ms = tuner.next_buffer_ms(url)
        if ms is None:
            return False
        self._bridge.buffering.emit(100)  # 重播前复位缓冲状态
        self.play(url, self._current_audio)
        return True

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

    def release(self) -> None:
        """停止播放并释放当前媒体/本地代理（保留 MediaPlayer 实例可复用）。

        切走视图/换视频时调用：停网络拉流、断开代理、释放解码资源，
        避免旧视频在后台继续播放/占用资源堆积。
        """
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
        try:
            self._player.stop()
        except Exception:  # noqa: BLE001
            pass
        self._current_url = ""
        self._current_audio = ""

    def set_volume(self, v: int) -> None:
        try:
            self._player.audio_set_volume(int(max(0, min(100, v))))
        except Exception:  # noqa: BLE001
            pass

    def toggle_mute(self) -> None:
        """静音/取消静音切换（VLC audio_toggle_mute）。"""
        try:
            self._player.audio_toggle_mute()
        except Exception:  # noqa: BLE001
            pass

    def is_muted(self) -> bool:
        try:
            return bool(self._player.audio_get_mute())
        except Exception:  # noqa: BLE001
            return False

    def seek_relative(self, seconds: float) -> None:
        """相对当前进度跳转 ±seconds 秒（set_position 网络流会阻塞 → 后台线程）。"""
        try:
            pos = self._player.get_position() or 0.0
            length = self._player.get_length()
            if length > 0:
                target = max(0.0, min(1.0, pos + seconds / length))
                threading.Thread(
                    target=lambda: self._player.set_position(target), daemon=True
                ).start()
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
        em.event_attach(vlc.EventType.MediaPlayerBuffering, self._cb_buffering)

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

    def _cb_buffering(self, event):
        """缓冲百分比事件（0~100）：UI 显示缓冲浮层/驱动卡顿自愈。"""
        try:
            self._bridge.buffering.emit(int(event.u.new_cache))
        except Exception:  # noqa: BLE001
            pass
