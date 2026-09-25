# -*- coding: utf-8 -*-
"""视频磁盘缓存 + 代理看门狗（test_media_cache.py）。

覆盖：
- MediaCache：mp4 完整落盘复用、HLS 分片落盘复用、LRU 部数约束、
  签名时效参数剔除（key 稳定）、.part 原子写不留垃圾
- MediaProxy 集成：/s/ m3u8 重写带 /c/ 路由；mp4 缓存命中后 Range 本地 serve
- Part 1 看门狗：空闲回收；活动请求 ≥1 时 stop 不执行（暂停/拖动不误杀）；
  流式循环内的 _touch 持续刷新
- 使用 127.0.0.1 真实本地 HTTP 服务器（同 test_http_retry.py 风格）。

运行：python -m pytest tests/test_media_cache.py -q
"""
from __future__ import annotations

import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
import requests  # noqa: E402

from framework.media_cache import MediaCache  # noqa: E402
from framework.media_proxy import MediaProxy, _IDLE_TIMEOUT, _WATCH_INTERVAL  # noqa: E402


# --------------------------------------------------------------------- #
# 本地假源站
# --------------------------------------------------------------------- #
class _FakeSource(BaseHTTPRequestHandler):
    """假 CDN：/mp4/<name> 大文件（支持 Range）、/hls/<name>.m3u8、/hls/seg<N>.ts。"""

    hits = None  # dict[path] = count
    mp4 = b"X" * 200_000
    segs = {f"seg{i}": bytes(range(0x30 + i, 0x30 + i + 200)) * 20 for i in range(4)}

    def _log_hit(self):
        key = self.path.split("?")[0]
        self.hits[key] = self.hits.get(key, 0) + 1

    def do_GET(self):  # noqa: N802
        self._log_hit()
        path = self.path.split("?")[0]
        if path == "/mp4/movie.mp4":
            self._send_file(self.mp4, "video/mp4")
            return
        if path == "/hls/index.m3u8":
            body = ("#EXTM3U\n"
                    "#EXT-X-VERSION:3\n"
                    "#EXT-X-TARGETDURATION:10\n"
                    "#EXT-X-KEY:METHOD=AES-128,URI=\"/hls/key.bin\"\n"
                    + "".join(f"#EXTINF:10.0,\n/hls/seg{i}.ts\n" for i in range(4))
                    + "#EXT-X-ENDLIST\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/hls/key.bin":
            body = b"\x00" * 16
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        m = re.fullmatch(r"/hls/(seg\d)\.ts", path)
        if m:
            self._send_file(self.segs[m.group(1)], "video/mp2t")
            return
        self.send_response(404)
        self.end_headers()

    def _send_file(self, data: bytes, ctype: str):
        size = len(data)
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            start = int(m.group(1)) if m and m.group(1) else 0
            end = int(m.group(2)) if m and m.group(2) else size - 1
            end = min(end, size - 1)
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            body = data[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
        else:
            body = data
            self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: A002
        pass


@pytest.fixture
def source():
    _FakeSource.hits = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSource)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def _url(server, path):
    return f"http://127.0.0.1:{server.server_port}{path}"


@pytest.fixture
def proxy_ctx(tmp_path):
    """构造带注入缓存实例的 MediaProxy（不碰单例），返回 (proxy, cache, teardown)。"""
    cache = MediaCache(root=tmp_path / "vc", max_videos=3, max_bytes=512 * 1024 ** 2)
    proxy = MediaProxy(cache=cache)
    yield proxy, cache
    proxy.stop()


def _wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------------------- #
# MediaCache：签名参数 key 稳定
# --------------------------------------------------------------------- #
def test_key_stable_under_signed_params():
    a = MediaCache.key_of("https://cdn/a/b.ts?token=abc&expires=999")
    b = MediaCache.key_of("https://cdn/a/b.ts?token=xyz&expires=1000")
    c = MediaCache.key_of("https://cdn/a/b.ts?hd=1")
    assert a == b
    assert a != c


# --------------------------------------------------------------------- #
# MP4：完整落盘 → 二次请求本地 serve（Range 不回源）
# --------------------------------------------------------------------- #
def test_mp4_cached_then_local_range(source, proxy_ctx):
    proxy, cache = proxy_ctx
    target = _url(source, "/mp4/movie.mp4")
    hdrs = {"Referer": "https://fake.example/"}
    local = proxy.build_url(target, hdrs)

    r = requests.get(local, timeout=10)
    assert r.status_code == 200
    assert r.content == _FakeSource.mp4

    key = cache.key_of(target)
    assert _wait_until(lambda: cache.entry(key) is not None)
    e = cache.entry(key)
    assert e["kind"] == "mp4" and e["complete"] is True
    assert cache.mp4_final(key).is_file()
    assert _wait_until(lambda: not list(cache.root.glob("*.part")))  # 无 .part 残留

    hits_before = _FakeSource.hits["/mp4/movie.mp4"]
    r2 = requests.get(local, headers={"Range": "bytes=100-199"}, timeout=10)
    assert r2.status_code == 206
    assert r2.content == _FakeSource.mp4[100:200]
    assert _FakeSource.hits["/mp4/movie.mp4"] == hits_before  # 命中本地，未回源


# --------------------------------------------------------------------- #
# HLS：m3u8 走 /s/ 且重写出 /c/；分片逐片落盘，.slf 复用不再回源
# --------------------------------------------------------------------- #
def test_hls_segments_cached(source, proxy_ctx):
    proxy, cache = proxy_ctx
    target = _url(source, "/hls/index.m3u8")
    local = proxy.build_url(target, {"Referer": "https://fake.example/"})

    r = requests.get(local, timeout=10)
    assert r.status_code == 200
    text = r.text
    assert "#EXT-X-KEY" in text
    # KEY 走 /s/（签名时效资源不落盘），分片走 /c/
    key_uri = re.search(r'URI="(http://127\.0\.0\.1:\d+/s/[0-9a-f]+)"', text)
    assert key_uri is not None
    c_urls = re.findall(r"(http://127\.0\.0\.1:\d+/c/[0-9a-f]{40}/[^\"\n]+)", text)
    assert len(c_urls) == 4

    # 拉取全部 /c/ 分片（首播：经上游 + tee 落盘）
    seg_bodies = [_FakeSource.segs[f"seg{i}"] for i in range(4)]
    for cu in c_urls:
        rr = requests.get(cu, timeout=10)
        assert rr.status_code == 200

    key = cache.key_of(target)
    assert _wait_until(lambda: cache.entry(key) is not None)
    assert cache.is_complete(key)
    assert cache.hls_dir(key).is_dir()
    assert _wait_until(lambda: not list(cache.hls_dir(key).glob("*.part")))  # 无 .part 残留
    # 分片已落盘
    for i in range(4):
        f = cache.hls_segment_final(key, f"seg{i}.ts")
        assert f.is_file()

    # 二次 m3u8 请求：分片全部本地命中（mp4/segs 源站命中数不增）
    c_hits = {f"/hls/seg{i}.ts": _FakeSource.hits.get(f"/hls/seg{i}.ts", 0)
              for i in range(4)}
    _ = requests.get(local, timeout=10)
    r2_c_urls = re.findall(r"(/c/[0-9a-f]{40}/[^\"\n]+)", requests.get(local, timeout=10).text)
    for cu in r2_c_urls:
        requests.get(_abs(source, cu), timeout=10)
    for i in range(4):
        assert _FakeSource.hits.get(f"/hls/seg{i}.ts", 0) == c_hits[f"/hls/seg{i}.ts"]


def _abs(server, path):
    if path.startswith("http"):
        return path
    return f"http://127.0.0.1:{server.server_port}{path}"


# --------------------------------------------------------------------- #
# LRU：只保留最近 N 部
# --------------------------------------------------------------------- #
def test_lru_keeps_recent(tmp_path):
    cache = MediaCache(root=tmp_path / "vc", max_videos=3,
                       max_bytes=1_000_000_000, enabled=True,
                       )
    for i in range(5):
        key = f"{i:040x}"
        p = cache.mp4_final(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 100)
        cache.mark_mp4(key, 100, complete=True)
        time.sleep(0.01)
    cache.prune()
    remaining = {k for k, e in cache.index_snapshot().items() if e.get("complete")}
    assert len(remaining) <= 3
    assert f"{3:040x}" in remaining  # 最新访问的保留
    assert f"{0:040x}" not in remaining  # 最旧淘汰


# --------------------------------------------------------------------- #
# Part 1 看门狗：空闲回收 / 活动请求保护 / _touch 刷新
# --------------------------------------------------------------------- #
def test_watchdog_stops_when_idle(tmp_path, monkeypatch):
    monkeypatch.setattr("framework.media_proxy._IDLE_TIMEOUT", 0.2)
    monkeypatch.setattr("framework.media_proxy._WATCH_INTERVAL", 0.05)
    cache = MediaCache(root=tmp_path / "vc", max_videos=3)
    proxy = MediaProxy(cache=cache)
    local = proxy.build_url("http://127.0.0.1:9/nope.mp4", {"Referer": "x"})
    time.sleep(0.6)
    assert proxy._server is None  # 空闲回收已触发（stop 已清 token/server）


def test_watchdog_stop_skipped_while_active(tmp_path, monkeypatch):
    monkeypatch.setattr("framework.media_proxy._IDLE_TIMEOUT", 0.05)
    monkeypatch.setattr("framework.media_proxy._WATCH_INTERVAL", 0.02)
    cache = MediaCache(root=tmp_path / "vc", max_videos=3)
    proxy = MediaProxy(cache=cache)
    local = proxy.build_url("http://127.0.0.1:9/nope.mp4", {"Referer": "x"})
    proxy._begin()  # 模拟进行中的流式请求
    time.sleep(0.3)
    assert proxy._server is not None  # 活动期间看门狗不应停服
    proxy._end()
    proxy.stop()
    assert proxy._server is None


def test_streaming_touch_keeps_alive(tmp_path, monkeypatch):
    monkeypatch.setattr("framework.media_proxy._IDLE_TIMEOUT", 0.2)
    monkeypatch.setattr("framework.media_proxy._WATCH_INTERVAL", 0.05)
    cache = MediaCache(root=tmp_path / "vc", max_videos=3)
    proxy = MediaProxy(cache=cache)
    local = proxy.build_url("http://127.0.0.1:9/nope.mp4", {"Referer": "x"})

    stop_at = time.time() + 0.45
    while time.time() < stop_at:
        proxy._touch()
        time.sleep(0.05)
    assert proxy._server is not None  # 持续 _touch 不被看门狗误杀
    proxy.stop()