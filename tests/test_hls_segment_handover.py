# -*- coding: utf-8 -*-
"""分片"撞车"回归（2026-11-01）：预取与播放请求别再下两遍同一片。

现场（tools/_probe_startup.py 实测，慢速上游 150KB/s）：
- 旧行为：播放器来要第 0 片 → 无条件取消在途预取并 `abort()` 掉 `.part` →
  同一片从 0 重下一遍。300KB 的片在慢 CDN 上等于多等 2 秒，正好是"点开一集"
  的那段时间；3.8MB 的 1080p 片更明显。
- 现行为：先给预取一次收尾机会（`_PREFETCH_HANDOVER_WAIT`），写完了直接命中
  本地；等不到才打断它，并把已下载字节留在 `.part` 里，本请求带 `Range`
  续取剩余部分。

顺带锁住另一条实测结论：**mpv/ffmpeg 对每个分片都发 `Range: bytes=0-`**
（语义是"要整片"）。旧代码把它当拖动处理，于是分片锁、预取握手、续传、
落盘四件事全部绕开——同一片回源两次、播放路径压根不写缓存。
"""
from __future__ import annotations

import http.client
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from framework import media_proxy as mp  # noqa: E402
from framework.media_cache import MediaCache  # noqa: E402

SEG = b"\x47" + bytes(range(256)) * 1024          # 262145B，够大才有续传意义
M3U8 = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
        "#EXTINF:10.0,\n000.ts\n#EXTINF:10.0,\n001.ts\n#EXT-X-ENDLIST\n")


class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits: dict = {}
    ranges: dict = {}
    lock = threading.Lock()
    started = None       # threading.Event：收到第一个分片请求时置位
    release = None       # threading.Event：放开分片响应
    block_after = None   # None=不等；0=先等；N=先推 N 字节再等

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.endswith(".m3u8"):
            body = M3U8.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        with _Upstream.lock:
            _Upstream.hits[path] = _Upstream.hits.get(path, 0) + 1
            _Upstream.ranges.setdefault(path, []).append(self.headers.get("Range"))
        if _Upstream.started is not None:
            _Upstream.started.set()
        rng = self.headers.get("Range") or ""
        start = 0
        if rng.startswith("bytes=") and rng[6:].split("-")[0].isdigit():
            start = int(rng[6:].split("-")[0])
        body = SEG[start:]
        if start:
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{len(SEG) - 1}/{len(SEG)}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if _Upstream.release is not None:
            n = min(int(_Upstream.block_after or 0), len(body))
            if n:
                self.wfile.write(body[:n])
                self.wfile.flush()
            _Upstream.release.wait(10)
            body = body[n:]
        self.wfile.write(body)


@pytest.fixture
def upstream():
    _Upstream.hits = {}
    _Upstream.ranges = {}
    _Upstream.started = None
    _Upstream.release = None
    _Upstream.block_after = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _direct_session():
    import requests

    s = requests.Session()
    s.trust_env = False
    return s


@pytest.fixture
def proxy(upstream, tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "_get_direct_session", _direct_session)
    monkeypatch.setattr(mp, "_get_session", _direct_session)
    monkeypatch.setattr(mp, "_single_variant_enabled", lambda: True)
    cache = MediaCache(root=tmp_path / "v", max_videos=5, enabled=True)
    p = mp.MediaProxy(cache=cache, prefetch={"enabled": False})
    p._ensure_server()
    try:
        yield p
    finally:
        p.stop()


def _split(url):
    rest = url.split("://", 1)[1]
    hostport, path = rest.split("/", 1)
    host, port = hostport.split(":")
    return host, int(port), "/" + path


def _get(url, headers=None, timeout=15):
    host, port, path = _split(url)
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _segment_urls(proxy, base):
    url = proxy.build_url(f"{base}/index.m3u8", {"User-Agent": "t"}, None)
    status, body = _get(url)
    assert status == 200, body[:200]
    return [ln.strip() for ln in body.decode().splitlines()
            if ln.strip() and not ln.startswith("#")]


# --------------------------------------------------------------------------- #
# 1. `Range: bytes=0-` 按"整片"处理（mpv 的真实请求形状）
# --------------------------------------------------------------------------- #
def test_bytes_zero_range_hits_local_cache(proxy, upstream):
    seg_url = _segment_urls(proxy, upstream)[0]
    assert _get(seg_url, {"Range": "bytes=0-"})[0] in (200, 206)
    hits_after_first = dict(_Upstream.hits)
    assert hits_after_first, "首片必须真的回源一次"

    status, body = _get(seg_url, {"Range": "bytes=0-"})
    assert status in (200, 206)
    assert len(body) == len(SEG)
    assert _Upstream.hits == hits_after_first, "重播必须命中本地，不再回源"


def test_bytes_zero_range_resumes_partial_download(proxy, upstream):
    """磁盘上已有半截 .part → 上游只要剩余字节，客户端仍拿到完整一片。"""
    seg_url = _segment_urls(proxy, upstream)[0]
    cache = proxy._cache
    key = list(proxy._seg_order)[0]
    upstream_url = proxy._seg_order[key][0]
    part = cache.hls_segment_part(key, upstream_url)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(SEG[:100_000])

    status, body = _get(seg_url, {"Range": "bytes=0-"})
    assert status in (200, 206)
    assert len(body) == len(SEG)
    assert body == SEG, "续传拼出来的必须是完整且正确的字节"
    assert _Upstream.ranges.get("/000.ts") == ["bytes=100000-"]
    final = cache.hls_segment_final(key, upstream_url)
    # 落盘提交发生在最后一个字节交给播放器之后（同一线程里紧接着 finish），
    # 所以这里要等一小会儿，别拿"客户端收完"当"磁盘写完了"。
    for _ in range(100):
        if final.is_file() and not part.exists():
            break
        time.sleep(0.02)
    assert final.is_file() and final.read_bytes() == SEG
    assert not part.exists(), "写满后 .part 必须提交成正式文件"


# --------------------------------------------------------------------------- #
# 2. 撞车：给预取一次收尾机会，别再下两遍
# --------------------------------------------------------------------------- #
def test_client_waits_for_inflight_prefetch_instead_of_downloading_twice(
        proxy, upstream):
    seg_urls = _segment_urls(proxy, upstream)
    key = list(proxy._seg_order)[0]
    upstream_url = proxy._seg_order[key][0]
    # 预取第 0 片，上游先卡住；此时播放器也来要这一片
    _Upstream.started = threading.Event()
    _Upstream.release = threading.Event()
    worker = threading.Thread(
        target=proxy._prefetch_one,
        args=(key, upstream_url, {"User-Agent": "t"}, False),
        daemon=True,
    )
    worker.start()
    assert _Upstream.started.wait(5), "预取没有真的发请求"

    response: dict = {}

    def client():
        response["status"], response["body"] = _get(seg_urls[0], {"Range": "bytes=0-"})

    reader = threading.Thread(target=client, daemon=True)
    reader.start()
    time.sleep(0.02)                     # 让播放请求先排队等锁（握手窗口 0.15s）
    _Upstream.release.set()              # 预取此刻写完
    worker.join(10)
    reader.join(10)

    assert _Upstream.hits.get("/000.ts") == 1, "同一片只该回源一次"
    assert response["status"] in (200, 206)
    assert response["body"] == SEG


def test_slow_prefetch_is_taken_over_with_bounded_wait(proxy, upstream):
    """慢 CDN 上的撞车：预取迟迟写不完，播放器只能等一个**有界**窗口。

    窗口内没写完 → 标记"播放器接管"、打断预取（保留 .part 给续传）、立刻自己取。
    这里用一个只占锁不下载的替身来把"预取很慢"做成确定性的，避免依赖真实
    上游的分块时序。
    """
    seg_urls = _segment_urls(proxy, upstream)
    key = list(proxy._seg_order)[0]
    upstream_url = proxy._seg_order[key][0]
    canonical = proxy._cache.key_of(upstream_url)
    lock = proxy._cache.path_lock(upstream_url)
    lock.acquire()
    holder_done = threading.Event()

    def holder():
        with proxy._pf_lock:
            proxy._pf_scheduled.add((key, upstream_url))
            proxy._pf_cancelled_urls[canonical] = threading.Event()
        time.sleep(mp.MediaProxy._PREFETCH_HANDOVER_WAIT * 4)
        holder_done.set()
        lock.release()

    threading.Thread(target=holder, daemon=True).start()
    started_at = time.monotonic()
    status, body = _get(seg_urls[0], {"Range": "bytes=0-"}, timeout=15)
    elapsed = time.monotonic() - started_at

    assert holder_done.is_set(), "不该无限等预取"
    assert mp.MediaProxy._PREFETCH_HANDOVER_WAIT <= elapsed < 2.0, elapsed
    assert status in (200, 206)
    assert body == SEG
    with proxy._pf_lock:
        took_over = canonical in proxy._pf_takeover
        cancelled = proxy._pf_cancelled_urls[canonical].is_set()
    assert took_over, "必须标记接管，否则预取收尾时会丢掉已下载字节"
    assert cancelled, "超过握手窗口后必须打断预取"


# --------------------------------------------------------------------------- #
# 3. `_FileTee.pause()` 保留已下载字节（旧 abort 会删掉 .part）
# --------------------------------------------------------------------------- #
def test_pause_keeps_partial_bytes(tmp_path):
    cache = MediaCache(root=tmp_path / "v", max_videos=5, enabled=True)
    part = tmp_path / "v" / "k" / "000.ts.part"
    final = tmp_path / "v" / "k" / "000.ts"
    part.parent.mkdir(parents=True, exist_ok=True)
    tee = mp._FileTee(cache, "k", part, final, len(SEG), is_mp4=False)
    assert tee.start()
    tee.write(SEG[:4096])
    tee.pause()
    assert part.is_file() and part.stat().st_size == 4096
    assert not final.exists()


def test_abort_still_discards_partial_bytes(tmp_path):
    cache = MediaCache(root=tmp_path / "v", max_videos=5, enabled=True)
    part = tmp_path / "v" / "k" / "001.ts.part"
    final = tmp_path / "v" / "k" / "001.ts"
    part.parent.mkdir(parents=True, exist_ok=True)
    tee = mp._FileTee(cache, "k", part, final, len(SEG), is_mp4=False)
    assert tee.start()
    tee.write(SEG[:4096])
    tee.abort()
    assert not part.exists()


# --------------------------------------------------------------------------- #
# 4. 起播剖面：清单→首片有日志（诊断"载入慢"唯一可信的尺子）
# --------------------------------------------------------------------------- #
def test_start_marks_written_for_hls_chain(proxy, upstream, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="framework.media_proxy"):
        seg_url = _segment_urls(proxy, upstream)[0]
        _get(seg_url)
    assert any("清单→首片" in rec.getMessage() for rec in caplog.records), \
        [r.getMessage() for r in caplog.records]
