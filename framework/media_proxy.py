"""本地流媒体代理（media_proxy.py）。

解决「外部播放器（VLC）无法设置 UA / 复杂防盗链头」的问题：
VLC 命令行只能传 Referer/UA 两个简单选项，且实测 --http-user-agent 在
VLC 3.0.23 无效（恒发 VLC 默认 UA，多数 CDN 拒绝）。本模块起一个
本机回环 HTTP 代理，把「目标 URL + 完整请求头（Referer/UA/Cookie…）」
打包成本地 URL 交给播放器：

    本地代理 URL ──► 代理带源 headers 请求目标 ──► 流式回传

- /s/<token>   任意媒体（mp4/mp3/ts/audio…）：流式转发，透传 Range
- 若响应是 m3u8（HLS）：重写内部分片/KEY/变体 URL 为本地代理 URL，
  让播放器拉分片时也自动带上源 headers（AES-128 key 同样代理）
- 单例 + 空闲自动回收（无请求 60s 停掉，下次 open 重新起）

用法：
    proxy = MediaProxy.instance()
    local_url = proxy.build_url("https://cdn/xxx.m3u8", {"Referer": ..., "User-Agent": ...})
    # 把 local_url 交给外部播放器
"""

from __future__ import annotations

import atexit
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, unquote

import requests
from requests.adapters import HTTPAdapter

_IDLE_TIMEOUT = 60.0  # 无任何请求 N 秒后自动关闭（避免残留进程/端口）
_READ_CHUNK = 64 * 1024

# 转发到 CDN 的连接池单例：VLC 经本地代理逐个拉 m3u8 分片时复用 keep-alive
# 连接，避免每个分片都重新对 CDN 握手（urllib.urlopen 无连接池，几十个分片
# 几十次 TCP/TLS 握手是播放卡顿/加载慢的常见根因）。
_PROXY_SESSION = None
_PROXY_SESSION_LOCK = threading.Lock()


def _get_session() -> requests.Session:
    """模块级单例 requests.Session（连接复用，keep-alive 提速）。"""
    global _PROXY_SESSION
    if _PROXY_SESSION is None:
        with _PROXY_SESSION_LOCK:
            if _PROXY_SESSION is None:
                s = requests.Session()
                try:
                    s.mount(
                        "http://", HTTPAdapter(pool_connections=16, pool_maxsize=64)
                    )
                    s.mount(
                        "https://", HTTPAdapter(pool_connections=16, pool_maxsize=64)
                    )
                except Exception:  # noqa: BLE001
                    pass
                _PROXY_SESSION = s
    return _PROXY_SESSION


class _ProxyHandler(BaseHTTPRequestHandler):
    """单请求处理器：/s/<token> → 按 token 找目标 URL + headers 转发。"""

    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ #
    def do_GET(self):  # noqa: N802
        proxy: "MediaProxy" = self.server.proxy  # type: ignore[attr-defined]
        proxy._touch()
        token = unquote(self.path.split("?", 1)[0]).rsplit("/", 1)[-1]
        entry = proxy._tokens.get(token)
        if entry is None:
            self.send_error(404, "token not found")
            return
        target, headers, ad_block = entry
        try:
            proxy._forward(self, target, headers, ad_block)
        except Exception as exc:  # noqa: BLE001 —— 网络波动直接断流，播放器会提示
            try:
                self.send_error(502, f"proxy error: {exc}")
            except Exception:
                pass

    def log_message(self, *args):  # 静音访问日志
        pass


class MediaProxy:
    """本地流媒体代理（单例）。"""

    _instance: "MediaProxy | None" = None

    def __init__(self):
        self._tokens: dict[str, tuple[str, dict]] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._last_use = time.time()
        self._idle_watch: threading.Thread | None = None
        self._start_idle_watch()
        atexit.register(self.stop)

    # ------------------------------------------------------------------ #
    @classmethod
    def instance(cls) -> "MediaProxy":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------ #
    def _start_idle_watch(self) -> None:
        def _watch():
            while True:
                time.sleep(10)
                if self._server is not None and time.time() - self._last_use > _IDLE_TIMEOUT:
                    self.stop()
        self._idle_watch = threading.Thread(target=_watch, daemon=True)
        self._idle_watch.start()

    def _touch(self) -> None:
        self._last_use = time.time()

    # ------------------------------------------------------------------ #
    def build_url(self, target_url: str, headers: dict | None = None,
                  ad_block: dict | None = None) -> str:
        """把目标媒体 URL 打包成本地代理 URL（播放器直接播这个）。

        ad_block：可选源 ad_block 配置。存在时代理转发 m3u8 会剔除广告段
        （下载路径已有过滤；播放路径此前无过滤，广告分片会照播）。
        """
        if not target_url:
            return ""
        self._ensure_server()
        token = uuid.uuid4().hex
        with self._lock:
            self._tokens[token] = (target_url, dict(headers or {}), ad_block)
        return f"http://127.0.0.1:{self._server.server_address[1]}/s/{token}"

    def _ensure_server(self) -> None:
        if self._server is not None:
            return
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
        self._server.proxy = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        srv, self._server = self._server, None
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        with self._lock:
            self._tokens.clear()

    # ------------------------------------------------------------------ #
    def _new_token(self, target: str, headers: dict, ad_block: dict | None = None) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._tokens[token] = (target, headers, ad_block)
        return token

    def _proxy_url(self, target: str, base: str, headers: dict,
                   ad_block: dict | None = None) -> str:
        full = urljoin(base, target)
        token = self._new_token(full, headers, ad_block)
        return f"http://127.0.0.1:{self._server.server_address[1]}/s/{token}"

    # ------------------------------------------------------------------ #
    def _forward(self, handler: "_ProxyHandler", target: str, headers: dict,
                 ad_block: dict | None = None) -> None:
        """转发一次请求。响应是 m3u8 则重写内部 URL，否则流式转发。

        ad_block：源 ad_block 配置。非空时 m3u8 重写前先剔除广告段
        （播放路径广告过滤，与下载路径 filter_m3u8 一致的判定启发式）。
        """
        req_headers = dict(headers)
        # 透传客户端 Range（拖动进度 / 分片定位）。
        # 但 m3u8 播放列表必须整读：VLC 拉 m3u8 时常带 Range（如 bytes=0-1275
        # 探测大小），若透传，CDN 返回截断的 m3u8 → 只拿到部分分片 → 播放
        # 中断。仅对媒体分片/大文件透传 Range，m3u8 URL 一律不传。
        is_hls_url = target.split("?", 1)[0].lower().endswith(".m3u8")
        rng = handler.headers.get("Range")
        if rng and not is_hls_url:
            req_headers["Range"] = rng
        # 连接池复用：requests.Session 保持到 CDN 的 keep-alive 连接，
        # HLS 分片逐个转发时不再每次重新握手（见 _get_session 注释）。
        # stream=True：只读头，body 手动流式透传（避免整段载入内存/拖慢首帧）。
        resp = _get_session().get(
            target, headers=req_headers, timeout=30, stream=True
        )
        try:
            # 上游错误（403/404/5xx）不发 body 给播放器：原 urllib 会抛
            # HTTPError，这里等价处理（播放器收到 502 会提示换线路/重试，
            # 而不是把错误页当媒体流播放黑屏）。
            if resp.status_code >= 400:
                handler.send_error(resp.status_code, "upstream error")
                return
            # m3u8 播放列表必须整读且自动解压：stream=True 时 resp.raw 返回
            # gzip 原始字节（Content-Encoding: gzip 不解压），直接重写会乱码/
            # 截断（542B gzip vs 8871B 明文）。m3u8 是小文本，用 resp.content
            # 完整读取 + 自动解压；媒体流（mp4/ts 大文件）才用 resp.raw 流式。
            if is_hls_url:
                body = resp.content  # 自动解压 gzip + 完整内容
                is_m3u8 = body.lstrip().startswith(b"#EXTM3U") or (
                    resp.headers.get("Content-Type") or ""
                ).find("mpegurl") >= 0
                if not is_m3u8:
                    # URL 是 m3u8 但内容不是（可能重定向/错误页）→ 透传原始内容
                    self._send_body(handler, resp, body)
                    return
                text = body.decode("utf-8", "replace")
                # 播放路径广告过滤：源配了 ad_block → 剔除 m3u8 广告段再重写
                # （VLC 播放时不再插播广告分片；判定与下载路径 filter_m3u8 一致）
                if ad_block:
                    text = self._filter_ad_segments(text, target, ad_block)
                rewritten = self._rewrite_m3u8(text, target, headers, ad_block)
                body = rewritten.encode("utf-8")
                handler.send_response(200)
                handler.send_header("Content-Type", "application/vnd.apple.mpegurl")
                handler.send_header("Content-Length", str(len(body)))
                handler.send_header("Connection", "close")
                handler.end_headers()
                handler.wfile.write(body)
                return

            # 先读一小块判断是不是 HLS 播放列表（URL 未含 .m3u8 但内容是的，
            # 如短链接/参数化 m3u8）。上游 gzip 压缩时 raw 是压缩字节无法判断，
            # 此时整读 content（自动解压）判断；明文则用 raw 流式读小块。
            if (resp.headers.get("Content-Encoding") or "").lower() in ("gzip", "deflate", "br"):
                first = resp.content
            else:
                first = resp.raw.read(65536)
            is_m3u8 = first.startswith(b"#EXTM3U") or (resp.headers.get("Content-Type") or "").find("mpegurl") >= 0

            if is_m3u8:
                # 读完整文本，重写内部 URL（分片/KEY/变体）为本地代理。
                # gzip 压缩时 first 已是完整解压内容（上方分支），rest 为空。
                rest = resp.raw.read()
                text = (first + rest).decode("utf-8", "replace")
                if ad_block:
                    text = self._filter_ad_segments(text, target, ad_block)
                rewritten = self._rewrite_m3u8(text, target, headers, ad_block)
                body = rewritten.encode("utf-8")
                handler.send_response(200)
                handler.send_header("Content-Type", "application/vnd.apple.mpegurl")
                handler.send_header("Content-Length", str(len(body)))
                handler.send_header("Connection", "close")
                handler.end_headers()
                handler.wfile.write(body)
                return

            # 普通媒体：透传响应头 + 流式转发（已拦截 >=400，这里透传上游状态码）
            handler.send_response(resp.status_code)
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                v = resp.headers.get(h)
                if v:
                    handler.send_header(h, v)
            handler.send_header("Connection", "close")
            handler.end_headers()
            try:
                if first:
                    handler.wfile.write(first)
                while True:
                    chunk = resp.raw.read(_READ_CHUNK)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 播放器提前关闭连接（拖动/停止）属正常
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    def _send_body(self, handler, resp, body: bytes) -> None:
        """透传上游完整响应体给客户端（m3u8 URL 内容非 m3u8 时的兜底）。"""
        handler.send_response(resp.status_code)
        handler.send_header("Content-Type", resp.headers.get("Content-Type") or "application/octet-stream")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)

    # ------------------------------------------------------------------ #
    def _filter_ad_segments(self, m3u8_text: str, base_url: str, ad_block: dict) -> str:
        """按源 ad_block 配置剔除 m3u8 广告段（播放路径广告过滤）。

        复用 adblock 引擎的 filter_m3u8（URL 广告特征 + 重复段 + 孤立短块
        判定，与下载路径一致）。失败/异常返回原文（不过滤不阻断播放）。
        """
        try:
            from .adblock import AdblockEngine
            engine = AdblockEngine()
            # 用源 ad_block 配置构造引擎（enabled/block_domains/block_url_regex）
            engine.configure(type("S", (), {"raw": {"ad_block": ad_block}})())
            if engine.enabled:
                return engine.filter_m3u8(m3u8_text, base_url)
        except Exception:  # noqa: BLE001 —— 过滤失败不阻断播放
            pass
        return m3u8_text

    # ------------------------------------------------------------------ #
    def _rewrite_m3u8(self, text: str, base: str, headers: dict,
                      ad_block: dict | None = None) -> str:
        """重写 m3u8 内部所有媒体 URL（分片 / EXT-X-KEY / EXT-X-MEDIA / 变体）为本地代理 URL。

        ad_block 透传给分片 token：子清单/嵌套 m3u8 继续带过滤配置（保持
        do_GET 解包 3 元组一致，且嵌套清单也能过滤广告）。
        """
        out = []
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith(("#EXT-X-KEY", "#EXT-X-MEDIA", "#EXT-X-MAP", "#EXT-X-SESSION-KEY", "#EXT-X-PRELOAD-HINT", "#EXT-X-IMAGE-STREAM-INF", "#EXT-X-I-FRAME-STREAM-INF")):
                # 这些标签的 URI="..." 属性也要代理
                ln = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{self._proxy_url(m.group(1), base, headers, ad_block)}"', ln)
            elif s and not s.startswith("#") and not s.startswith("<"):
                # 普通行 = 分片 / 变体 URL
                ln = self._proxy_url(ln, base, headers, ad_block)
            out.append(ln)
        return "\n".join(out) + "\n"


# ------------------------------------------------------------------ #
def proxy_url_for(url: str, headers: dict | None = None,
                  ad_block: dict | None = None) -> str:
    """便捷入口：把媒体 URL 转成本地代理 URL（带防盗链 headers）。

    ad_block：可选源 ad_block 配置，非空时代理转发 m3u8 会剔除广告段。
    """
    if not url:
        return ""
    if headers:
        return MediaProxy.instance().build_url(url, headers, ad_block=ad_block)
    return url
