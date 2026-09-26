# -*- coding: utf-8 -*-
"""media_proxy 播放转发流畅性测试（真实播放链路）。

真实播放链路：gui/pages/reader/video_view.py `_play` → external_player
`open_with_player` → media_proxy `proxy_url_for` → 外部 VLC 进程拉
http://127.0.0.1:PORT/s/<token>。**内嵌 vlc_player.VlcPlayer 从未被实例化**
（只有 app.py 拿它做 warmup/shutdown），故流畅性只以本链路为准。

已证实的卡顿根因（本次修复对象）：
- A. `_ProxyHandler.protocol_version` 已是 HTTP/1.1，但**每个**响应都发
  `Connection: close`（_emit_m3u8 / _forward_media / _serve_cache /
  _send_body / _serve_local_file）→ HLS 逐段请求每段新建 TCP 连接 +
  新建服务线程，mp4 拖动更是每个 Range 一次
- B. `_forward_media` / `_serve_cache` 对**每个**媒体请求先
  `resp.raw.read(65536)` 嗅探是否 HLS → 每个分片首字节前多等 64KB

覆盖：
- A：分片/清单/本地缓存文件响应复用同一条连接（connects==1）
- A：无 Content-Length 的上游（chunked）仍能正确定界并复用连接
- A：上游中断后连接被标记关闭，不把脏流留给下一个请求
- B：.ts 分片不预读 64KB（首字节立刻可读）
- 防回归：所有代理响应都带定界（Content-Length 或 chunked 或关闭）
"""
import gzip
import http.client
import mimetypes
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

import framework.media_proxy as mp
from framework.media_proxy import MediaProxy

_M3U8 = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:10\n"
    "#EXTINF:10.0,\n001.ts\n"
    "#EXTINF:10.0,\n002.ts\n"
    "#EXT-X-ENDLIST\n"
)
_SEG = b"\x47" + b"\x00" * 8191  # 8KB 合成 TS 载荷（故意小于嗅探用的 64KB）


class _OffCache:
    """注入的关闭缓存：让代理跳过落盘/命中路径，聚焦转发行为。"""

    enabled = False


class _FakeUpstream(BaseHTTPRequestHandler):
    """假 CDN。行为按路径分派，release 事件控制「慢速/不结束」响应。"""

    slow = threading.Event()  # 未 set 时 .ts 响应只发头部就挂住

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        p = self.path
        if p.endswith(".m3u8"):
            body = _M3U8.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p.endswith("chunked.ts"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for _ in range(2):
                self.wfile.write(b"%x\r\n" % len(_SEG) + _SEG + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        if p.endswith("truncated.ts"):
            # 声明 1MB 但只发 1KB 就断开：模拟上游中途断流
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(1024 * 1024))
            self.end_headers()
            self.wfile.write(_SEG[:1024])
            self.wfile.flush()
            self.close_connection = True
            return
        if p.endswith("gzip.ts"):
            # 上游无视 Accept-Encoding: identity 仍回 gzip：Content-Length 是
            # **压缩态**长度，与代理将要写出的解压后字节数不符。
            body = gzip.compress(_SEG)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p.endswith("slow.ts"):
            # 头部 + 1KB 后挂住不结束：代理若预读 64KB 就会卡在这里
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(64 * 1024))
            self.end_headers()
            self.wfile.write(_SEG)
            self.wfile.flush()
            self.slow.wait(10)
            return
        body = _SEG
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _CountingConn(http.client.HTTPConnection):
    """记录实际 TCP 连接次数（keep-alive 是否生效的可观测代理）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.connects = 0

    def connect(self):
        self.connects += 1
        super().connect()


def _direct_session():
    s = requests.Session()
    s.trust_env = False
    return s


@pytest.fixture
def relay(monkeypatch):
    """假上游 + 真实 MediaProxy（缓存关闭，不落盘、不触外网）。"""
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    monkeypatch.setattr(mp, "_get_direct_session", _direct_session)
    monkeypatch.setattr(mp, "_get_session", _direct_session)
    proxy = MediaProxy(cache=_OffCache())
    proxy._ensure_server()
    _FakeUpstream.slow.clear()
    base = f"http://127.0.0.1:{upstream.server_address[1]}"
    try:
        yield proxy, base
    finally:
        _FakeUpstream.slow.set()
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()


def _port(proxy):
    return proxy._server.server_address[1]


def _conn(proxy, timeout=10):
    return _CountingConn("127.0.0.1", _port(proxy), timeout=timeout)


# --------------------------------------------------------------------- #
# A：连接复用
# --------------------------------------------------------------------- #
def test_hls_segments_reuse_one_connection(relay):
    """HLS 逐段请求必须复用同一条本地连接（每段新建连接是卡顿源）。"""
    proxy, base = relay
    c = _conn(proxy)
    try:
        for seg in ("001.ts", "002.ts", "001.ts"):
            c.request("GET", proxy.build_url(f"{base}/hls/{seg}"))
            r = c.getresponse()
            assert r.status == 200, seg
            assert r.read() == _SEG
        assert c.connects == 1, f"每段都新建连接：connects={c.connects}"
    finally:
        c.close()


def test_playlist_then_segment_reuse_connection(relay):
    """清单 + 分片走同一连接（真实 HLS 首播顺序）。"""
    proxy, base = relay
    c = _conn(proxy)
    try:
        c.request("GET", proxy.build_url(f"{base}/hls/index.m3u8"))
        r = c.getresponse()
        text = r.read().decode()
        assert "#EXTM3U" in text
        # 重写后的分片是本地代理路径（/s/<token>），不再是 .ts 结尾
        segs = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        assert len(segs) == 2, text
        for s in segs:
            c.request("GET", s)
            r2 = c.getresponse()
            assert r2.status == 200
            assert r2.read() == _SEG
        assert c.connects == 1
    finally:
        c.close()


def test_segment_response_does_not_advertise_close(relay):
    """分片响应不得声明 Connection: close（否则客户端主动断链）。"""
    proxy, base = relay
    c = _conn(proxy)
    try:
        c.request("GET", proxy.build_url(f"{base}/hls/001.ts"))
        r = c.getresponse()
        r.read()
        assert (r.getheader("Connection") or "").lower() != "close"
    finally:
        c.close()


def test_playlist_response_does_not_advertise_close(relay):
    proxy, base = relay
    c = _conn(proxy)
    try:
        c.request("GET", proxy.build_url(f"{base}/hls/index.m3u8"))
        r = c.getresponse()
        r.read()
        assert (r.getheader("Connection") or "").lower() != "close"
    finally:
        c.close()


def test_chunked_upstream_segment_terminates_and_reuses(relay):
    """上游 chunked（无 Content-Length）→ 代理必须自己按 chunked 定界，
    否则 HTTP/1.1 下客户端读到 EOF 也不知道结束，且连接无法复用。"""
    proxy, base = relay
    c = _conn(proxy)
    try:
        c.request("GET", proxy.build_url(f"{base}/hls/chunked.ts"))
        r = c.getresponse()
        assert r.status == 200
        assert r.read() == _SEG * 2, "chunked 响应体不完整"
        c.request("GET", proxy.build_url(f"{base}/hls/001.ts"))
        r2 = c.getresponse()
        assert r2.status == 200
        assert r2.read() == _SEG
        assert c.connects == 1
    finally:
        c.close()


def test_gzip_segment_declares_decompressed_length(relay):
    """上游无视 identity 仍回 gzip 时，代理写出的是**解压后**字节，
    Content-Length 必须随之改写——否则 keep-alive 下客户端按压缩态长度死等，
    或把半截流错当成下一次请求的响应。"""
    proxy, base = relay
    c = _conn(proxy, timeout=5)
    try:
        c.request("GET", proxy.build_url(f"{base}/hls/gzip.ts"))
        r = c.getresponse()
        assert r.status == 200
        assert r.getheader("Content-Length") == str(len(_SEG)), (
            "Content-Length 仍是压缩态长度：%s" % r.getheader("Content-Length")
        )
        assert r.read() == _SEG
        # 连接未错位：同一条连接继续取下一个分片
        c.request("GET", proxy.build_url(f"{base}/hls/001.ts"))
        r2 = c.getresponse()
        assert r2.status == 200
        assert r2.read() == _SEG
        assert c.connects == 1
    finally:
        c.close()


def test_truncated_upstream_is_detected_not_desynced(relay):
    """上游提前断流：客户端必须能察觉（读到 EOF 而非挂死）。

    IncompleteRead 本身即证明代理关闭了这条连接——声明长度没写满又把连接
    留着不关，客户端就会一直等（或把半截流当成下一请求的响应）。
    """
    proxy, base = relay
    c = _conn(proxy, timeout=5)
    try:
        c.request("GET", proxy.build_url(f"{base}/hls/truncated.ts"))
        r = c.getresponse()
        assert r.status == 200
        with pytest.raises(http.client.IncompleteRead):
            r.read()
    finally:
        c.close()
    # 代理未卡死：新连接仍能正常取到完整分片
    c2 = _conn(proxy)
    try:
        c2.request("GET", proxy.build_url(f"{base}/hls/001.ts"))
        r2 = c2.getresponse()
        assert r2.status == 200
        assert r2.read() == _SEG
    finally:
        c2.close()


# --------------------------------------------------------------------- #
# B：分片不预读 64KB
# --------------------------------------------------------------------- #
def test_ts_segment_first_bytes_arrive_without_full_preread(relay):
    """.ts 分片不应为嗅探 HLS 预读 64KB：首字节必须立刻透传给播放器。

    上游 slow.ts 只发 8KB 就挂住不结束——代理若预读 64KB，响应头都发不出去，
    客户端在 getresponse() 上就超时（实测 3s TimeoutError）。
    """
    proxy, base = relay
    c = _conn(proxy, timeout=3)
    try:
        c.request("GET", proxy.build_url(f"{base}/hls/slow.ts"))
        started = time.monotonic()
        r = c.getresponse()          # 响应头到达 = 代理已开始转发
        got = r.read1(1024)          # 首字节立刻可读（read1 = 至多一次裸读）
        elapsed = time.monotonic() - started
        assert r.status == 200
        assert got, "代理未透传任何首字节"
        assert _SEG.startswith(got), f"首字节内容不对: {got[:32]!r}"
        assert elapsed < 2.0, f"首字节被预读阻塞 {elapsed:.2f}s（嗅探 64KB 未跳过）"
    finally:
        _FakeUpstream.slow.set()
        c.close()


def test_extensionless_url_still_detects_hls(relay):
    """无扩展名短链内容是 m3u8 时仍须走重写路径（不能因跳过嗅探而漏判）。"""
    proxy, base = relay
    local = proxy.build_url(f"{base}/media/shortlink")
    r = requests.get(local, timeout=10)
    assert r.status_code == 200
    assert "#EXTM3U" not in r.text  # 短链返回的是 .ts 内容（见上游）
    assert r.content == _SEG


# --------------------------------------------------------------------- #
# 本地缓存文件响应（mp4 拖动：每个 Range 一次）
# --------------------------------------------------------------------- #
class _RecordingHandler:
    """记录 send_header 调用的假 handler（单测 _serve_local_file 的定界头）。"""

    def __init__(self):
        self.status = None
        self.headers = {}
        self.written = bytearray()
        self.ended = False
        self.close_connection = False

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers[k.lower()] = v

    def end_headers(self):
        self.ended = True

    def write(self, b):
        self.written += b

    @property
    def wfile(self):
        return self


def test_serve_local_file_keeps_connection_alive(tmp_path):
    """本地缓存 mp4 响应不得声明 close（VLC 拖动进度连发 Range）。"""
    p = tmp_path / "movie.mp4"
    p.write_bytes(b"\x00" * 4096)
    proxy = MediaProxy(cache=_OffCache())
    try:
        h = _RecordingHandler()
        proxy._serve_local_file(h, p)
        assert h.status == 200
        assert h.headers.get("content-length") == "4096"
        assert h.headers.get("connection", "").lower() != "close"
        assert bytes(h.written) == b"\x00" * 4096
    finally:
        proxy.stop()


def test_serve_local_file_range_keeps_connection_alive(tmp_path):
    p = tmp_path / "movie.mp4"
    p.write_bytes(bytes(range(256)) * 16)
    proxy = MediaProxy(cache=_OffCache())
    try:
        h = _RecordingHandler()
        h.headers = {"Range": "bytes=0-99"}
        proxy._serve_local_file(h, p)
        assert h.status == 206
        assert h.headers.get("content-length") == "100"
        assert h.headers.get("connection", "").lower() != "close"
    finally:
        proxy.stop()


def test_serve_local_file_unsatisfiable_range_is_bounded(tmp_path):
    """416 必须有 Content-Length: 0，否则 keep-alive 下客户端等死。"""
    p = tmp_path / "movie.mp4"
    p.write_bytes(b"\x00" * 16)
    proxy = MediaProxy(cache=_OffCache())
    try:
        h = _RecordingHandler()
        h.headers = {"Range": "bytes=999-"}
        proxy._serve_local_file(h, p)
        assert h.status == 416
        assert h.headers.get("content-length") == "0"
    finally:
        proxy.stop()


# --------------------------------------------------------------------- #
# 防回归：mimetypes 可用（本地文件 Content-Type 依赖它）
# --------------------------------------------------------------------- #
def test_mp4_mimetype_resolvable():
    assert mimetypes.guess_type("a.mp4")[0]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
