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
        target, headers = entry
        try:
            proxy._forward(self, target, headers)
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
    def build_url(self, target_url: str, headers: dict | None = None) -> str:
        """把目标媒体 URL 打包成本地代理 URL（播放器直接播这个）。"""
        if not target_url:
            return ""
        self._ensure_server()
        token = uuid.uuid4().hex
        with self._lock:
            self._tokens[token] = (target_url, dict(headers or {}))
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
    def _new_token(self, target: str, headers: dict) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._tokens[token] = (target, headers)
        return token

    def _proxy_url(self, target: str, base: str, headers: dict) -> str:
        full = urljoin(base, target)
        token = self._new_token(full, headers)
        return f"http://127.0.0.1:{self._server.server_address[1]}/s/{token}"

    # ------------------------------------------------------------------ #
    def _forward(self, handler: "_ProxyHandler", target: str, headers: dict) -> None:
        """转发一次请求。响应是 m3u8 则重写内部 URL，否则流式转发。"""
        req_headers = dict(headers)
        # 透传客户端 Range（拖动进度 / 分片定位）
        rng = handler.headers.get("Range")
        if rng:
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
            # 先读一小块判断是不是 HLS 播放列表
            first = resp.raw.read(65536)
            is_m3u8 = first.startswith(b"#EXTM3U") or (resp.headers.get("Content-Type") or "").find("mpegurl") >= 0

            if is_m3u8:
                # 读完整文本，重写内部 URL（分片/KEY/变体）为本地代理
                rest = resp.raw.read()
                text = (first + rest).decode("utf-8", "replace")
                rewritten = self._rewrite_m3u8(text, target, headers)
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
    def _rewrite_m3u8(self, text: str, base: str, headers: dict) -> str:
        """重写 m3u8 内部所有媒体 URL（分片 / EXT-X-KEY / EXT-X-MEDIA / 变体）为本地代理 URL。"""
        out = []
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith(("#EXT-X-KEY", "#EXT-X-MEDIA", "#EXT-X-MAP", "#EXT-X-SESSION-KEY", "#EXT-X-PRELOAD-HINT", "#EXT-X-IMAGE-STREAM-INF", "#EXT-X-I-FRAME-STREAM-INF")):
                # 这些标签的 URI="..." 属性也要代理
                ln = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{self._proxy_url(m.group(1), base, headers)}"', ln)
            elif s and not s.startswith("#") and not s.startswith("<"):
                # 普通行 = 分片 / 变体 URL
                ln = self._proxy_url(ln, base, headers)
            out.append(ln)
        return "\n".join(out) + "\n"


# ------------------------------------------------------------------ #
def proxy_url_for(url: str, headers: dict | None = None) -> str:
    """便捷入口：把媒体 URL 转成本地代理 URL（带防盗链 headers）。"""
    if not url:
        return ""
    if headers:
        return MediaProxy.instance().build_url(url, headers)
    return url
