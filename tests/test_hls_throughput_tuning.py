# -*- coding: utf-8 -*-
"""HLS 吞吐瓶颈调优：按源 VLC 缓冲 / 磁盘缓存预算 / 有界预取（默认关闭）。

背景（ikanpp 实测，非猜测）：
- 单连接被 CDN **按连接限速**：单测 85~190KB/s，长测掉到 33~68KB/s，
  而实时播放需 >=136KB/s（1.33MB/片 ÷ 10s）。
- 4 并发总带宽 280.2KB/s vs 顺序 133.2KB/s ≈ **2.10x** → 并发能绕开单连接上限。
- 该片 1060 片 / 约 1.4GB。

对应三项改动：
1. **按源**提高 VLC --network-caching：只有 ikanpp 需要大缓冲（它的带宽撑不住
   8s 默认值），其他源保持 8000ms 不受影响。
2. 磁盘缓存预算抬到能装下 1.4GB，消除边写边 LRU 淘汰导致的抖动。
3. **有界预取**：lookahead N 片 / 并发 W。**默认关闭**——它会改变对源站的请求
   模式，需实测确认不触发风控后再手动开启。
"""
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

import framework.external_player as ep
import framework.media_cache as mc
import framework.media_proxy as mp
from framework.media_cache import MediaCache
from framework.media_proxy import MediaProxy

_VLC = "C:/fake/vlc.exe"


class _DisabledCache:
    enabled = False


# --------------------------------------------------------------------------- #
# 1. 按源 VLC network-caching
# --------------------------------------------------------------------------- #
def _install_vlc(monkeypatch):
    """假装 VLC 存在并捕获启动参数。"""
    procs = []

    class _P:
        def __init__(self, args):
            self.args = args
            self.pid = 1
            self.returncode = None

        def poll(self):
            return None

    def _popen(args, *a, **kw):
        procs.append(_P(list(args)))
        return procs[-1]

    monkeypatch.setattr(ep, "_locate_vlc", lambda: _VLC)
    monkeypatch.setattr(ep.subprocess, "Popen", _popen)
    monkeypatch.setattr(ep.secrets, "token_hex", lambda n: "TESTPWD")
    monkeypatch.setattr(ep, "_last_proc", None)
    monkeypatch.setattr(ep, "_control_state", None)
    monkeypatch.setattr(ep, "_pick_http_port", lambda: 9)
    # 模拟真实代理路径（ikanpp 源有 transports.headers → 必走 media_proxy）：
    # play_url != url 才会触发 8000ms 的代理加码。
    monkeypatch.setattr(
        ep, "proxy_url_for",
        lambda u, h=None, ad_block=None, force_proxy=False:
            "http://127.0.0.1:1/c/k" + u)
    return procs


def _caching_of(args):
    for a in args:
        if a.startswith("--network-caching="):
            return int(a.split("=", 1)[1])
    return None


def test_default_hls_buffer_unchanged(monkeypatch):
    """不传覆盖时，走代理的 HLS 仍是 8000ms —— 不给其他源添启动延迟。"""
    procs = _install_vlc(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    assert _caching_of(procs[0].args) == 8000


def test_source_override_raises_buffer(monkeypatch):
    """ikanpp 传 45000 → VLC 用 45s 缓冲。"""
    procs = _install_vlc(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8", caching_ms=45000)
    assert _caching_of(procs[0].args) == 45000


def test_override_is_floor_not_cap(monkeypatch):
    """覆盖值当下限用：配小了也不该把分类算出的更大值降级。"""
    procs = _install_vlc(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8", caching_ms=1000)
    assert _caching_of(procs[0].args) == 8000


def test_ikanpp_source_declares_30s():
    """ikanpp 源配置里落 30s，否则源级缓冲不会生效。"""
    p = Path(__file__).resolve().parent.parent / "sources" / "ikanpp.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    hls = (raw.get("media") or {}).get("hls") or {}
    assert hls.get("network_caching_ms") == 30000


# --------------------------------------------------------------------------- #
# 2. 磁盘缓存预算
# --------------------------------------------------------------------------- #
def test_cache_default_budget_fits_long_episode(tmp_path):
    """默认预算 8GB：1.4GB 的片子 + 同量其他几集都能留住，不边写边淘汰。"""
    c = MediaCache(root=tmp_path / "v")
    assert c.max_bytes == 8 * 1024 ** 3
    assert c.max_videos == 5


def test_settings_defaults_8gb(monkeypatch, tmp_path):
    """app_config 缺 video_cache 段时，兜底默认也是 8GB/5 集。"""
    (tmp_path / "app_config.json").write_text("{}", encoding="utf-8")

    class _SM:
        def __init__(self, *a, **kw):
            pass

        def get_section(self, name):
            return None

    import framework.settings_manager as smod
    monkeypatch.setattr(smod, "SettingsManager", _SM)
    monkeypatch.setattr(mc, "_base_dir", lambda: tmp_path)
    got = mc._settings_defaults()
    assert got["max_bytes"] == 8 * 1024 ** 3
    assert got["max_videos"] == 5


# --------------------------------------------------------------------------- #
# 3. 有界预取（默认关闭）
# --------------------------------------------------------------------------- #
_SEG = b"\x47" + b"\x00" * 4095
_M3U8 = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
for _i in range(6):
    _M3U8 += f"#EXTINF:10.0,\n{_i:03d}.ts\n"
_M3U8 += "#EXT-X-ENDLIST\n"


class _Upstream(BaseHTTPRequestHandler):
    """假 CDN：记录每个分片被请求了几次。"""

    hits: dict = {}
    lock = threading.Lock()

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
        with _Upstream.lock:
            _Upstream.hits[p] = _Upstream.hits.get(p, 0) + 1
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(len(_SEG)))
        self.end_headers()
        self.wfile.write(_SEG)


def _direct_session():
    s = requests.Session()
    s.trust_env = False
    return s


def _split(url):
    rest = url.split("://", 1)[1]
    hostport, path = rest.split("/", 1)
    host, port = hostport.split(":")
    return host, int(port), "/" + path


def _get(url, timeout=10):
    host, port, path = _split(url)
    c = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


@pytest.fixture
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Upstream.hits = {}
    _Upstream.lock = threading.Lock()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def pf_proxy(monkeypatch, upstream, tmp_path):
    """真实磁盘缓存 + 可控预取的代理。"""
    monkeypatch.setattr(mp, "_get_direct_session", _direct_session)
    monkeypatch.setattr(mp, "_get_session", _direct_session)
    cache = MediaCache(root=tmp_path / "v", enabled=True)
    proxies = []

    def _make(prefetch):
        p = MediaProxy(cache=cache, prefetch=prefetch)
        p._ensure_server()
        proxies.append(p)
        return p

    yield _make
    for p in proxies:
        p.stop()


def _prime_playlist(proxy, base):
    """经代理取一次 master 清单，返回 (cache_key, 有序分片 URL 列表)。"""
    # 用实例的 build_url（proxy_url_for 走的是 MediaProxy.instance() 单例，
    # 与测试注入的 cache/prefetch 实例不是同一个）
    url = proxy.build_url(f"{base}/index.m3u8", {"User-Agent": "t"}, None)
    st, body = _get(url)
    assert st == 200, (st, body[:200])
    key = list(proxy._seg_order)[0]
    return key, proxy._seg_order[key]


def test_prefetch_off_by_default(monkeypatch, upstream, tmp_path):
    """没配 hls_prefetch → 预取关：不产生任何额外上游请求（不增加风控面）。"""
    monkeypatch.setattr(mp, "_get_direct_session", _direct_session)
    monkeypatch.setattr(mp, "_get_session", _direct_session)
    cache = MediaCache(root=tmp_path / "v", enabled=True)
    proxy = MediaProxy(cache=cache, prefetch={})
    proxy._ensure_server()
    try:
        assert proxy._prefetch_enabled() is False
        key, order = _prime_playlist(proxy, upstream)
        proxy._maybe_prefetch(key, order[0])
        proxy._prefetch_drain()
        assert _Upstream.hits == {}  # 一个分片都没多拉
    finally:
        proxy.stop()


def test_prefetch_schedules_lookahead(pf_proxy, upstream):
    """开预取：请求第 0 片 → 后续 depth 片自动落盘（默认 4）。"""
    proxy = pf_proxy({"enabled": True, "depth": 4, "workers": 3})
    key, order = _prime_playlist(proxy, upstream)
    proxy._maybe_prefetch(key, order[0])
    proxy._prefetch_drain()
    cache = proxy._cache
    for i in (1, 2, 3, 4):
        assert cache.hls_segment_final(key, f"{i:03d}.ts").is_file(), i
    # 前瞻边界：第 5 片不在窗口内
    assert not cache.hls_segment_final(key, "005.ts").is_file()


def test_prefetch_respects_depth(pf_proxy, upstream):
    """depth=2 → 只预取 2 片（lookahead 有界，不扫完整清单）。"""
    proxy = pf_proxy({"enabled": True, "depth": 2, "workers": 2})
    key, order = _prime_playlist(proxy, upstream)
    proxy._maybe_prefetch(key, order[0])
    proxy._prefetch_drain()
    cache = proxy._cache
    assert cache.hls_segment_final(key, "001.ts").is_file()
    assert cache.hls_segment_final(key, "002.ts").is_file()
    assert not cache.hls_segment_final(key, "003.ts").is_file()


def test_prefetch_stops_at_playlist_end(pf_proxy, upstream):
    """清单尾部不越界（不 404 风暴）。"""
    proxy = pf_proxy({"enabled": True, "depth": 4, "workers": 3})
    key, order = _prime_playlist(proxy, upstream)
    proxy._maybe_prefetch(key, order[-1])
    proxy._prefetch_drain()
    assert _Upstream.hits == {}


def test_prefetch_dedupes(pf_proxy, upstream):
    """同一片重复触发只拉一次（VLC 反复请求首片/重试时不放大流量）。"""
    proxy = pf_proxy({"enabled": True, "depth": 4, "workers": 3})
    key, order = _prime_playlist(proxy, upstream)
    proxy._maybe_prefetch(key, order[0])
    proxy._prefetch_drain()
    proxy._maybe_prefetch(key, order[0])
    proxy._prefetch_drain()
    assert _Upstream.hits.get("/001.ts", 0) == 1


def test_prefetch_uses_direct_session(pf_proxy, upstream):
    """预取走 trust_env=False 直连：系统代理实测慢 2.1x，不能用它预取。"""
    proxy = pf_proxy({"enabled": True, "depth": 2, "workers": 2})
    key, order = _prime_playlist(proxy, upstream)
    assert mp._get_direct_session().trust_env is False
    proxy._maybe_prefetch(key, order[0])
    proxy._prefetch_drain()
    assert _Upstream.hits


# --------------------------------------------------------------------------- #
# 端到端：真实 /c/ 分片请求触发预取（钩子装在 _serve_cache 里）
# --------------------------------------------------------------------------- #
def _playlist_seg_urls(proxy, base):
    _, body = _get(proxy.build_url(f"{base}/index.m3u8", {"User-Agent": "t"}, None))
    return [l for l in body.decode().splitlines() if l.endswith(".ts")]


def test_prefetch_triggers_on_real_segment_request(pf_proxy, upstream):
    """VLC 取 000.ts（真实 /c/ 请求）即触发预取，后续片自动落盘。"""
    proxy = pf_proxy({"enabled": True, "depth": 4, "workers": 3})
    segs = _playlist_seg_urls(proxy, upstream)
    assert segs[0].startswith("http://127.0.0.1:")
    st, data = _get(segs[0])
    assert st == 200 and len(data) == len(_SEG)
    assert proxy._prefetch_drain()
    key = list(proxy._seg_order)[0]
    for i in (1, 2, 3, 4):
        assert proxy._cache.hls_segment_final(key, f"{i:03d}.ts").is_file(), i


def test_prefetched_segment_served_from_disk(pf_proxy, upstream):
    """预取过的片再请求 → 命中本地文件、**不再回落上游**（省的就是这段带宽）。"""
    proxy = pf_proxy({"enabled": True, "depth": 4, "workers": 3})
    segs = _playlist_seg_urls(proxy, upstream)
    _get(segs[0])                       # 触发预取 001..004
    assert proxy._prefetch_drain()
    assert _Upstream.hits.get("/001.ts", 0) == 1     # 预取已拉过一次
    st, data = _get(segs[1])            # VLC 随后消费 001.ts
    assert st == 200 and len(data) == len(_SEG)
    assert _Upstream.hits.get("/001.ts", 0) == 1     # 命中本地，未再回源


def test_prefetch_limits_are_hard_clamped():
    proxy = MediaProxy(cache=_DisabledCache(), prefetch={
        "enabled": True, "depth": 100, "workers": 100,
    })
    try:
        # 上限以常量为准（历史上是 16，后按实测放宽到 64，别再把数字写死）
        assert proxy._pf_cfg["depth"] == mp._PREFETCH_MAX_DEPTH
        assert proxy._pf_cfg["workers"] == mp._PREFETCH_MAX_WORKERS
    finally:
        proxy.stop()


def test_prefetch_keeps_distinct_signed_targets_separate(pf_proxy):
    proxy = pf_proxy({"enabled": True, "depth": 4, "workers": 2})
    cache = proxy._cache
    key = cache.key_of("http://cdn.test/index.m3u8")
    with proxy._lock:
        proxy._cache_ctx[key] = ("http://cdn.test/index.m3u8", {}, None, False)
        proxy._seg_pos[key] = {
            "http://cdn.test/000.ts": 0,
        }
        proxy._seg_order[key] = [
            "http://cdn.test/000.ts",
            "http://cdn.test/001.ts?token=a",
            "http://cdn.test/001.ts?token=b",
        ]
    proxy._maybe_prefetch(key, "http://cdn.test/000.ts")
    assert len(proxy._pf_scheduled) == 2
    proxy._prefetch_drain()


def test_player_request_cancels_queued_prefetch(pf_proxy):
    proxy = pf_proxy({"enabled": True, "depth": 1, "workers": 1})
    url = "http://cdn.test/001.ts"
    canonical = proxy._cache.key_of(url)
    with proxy._pf_lock:
        proxy._pf_scheduled.add(canonical)
        proxy._pf_cancelled_urls[canonical] = threading.Event()
    proxy._cancel_prefetch_url(url)
    with proxy._pf_lock:
        assert proxy._pf_cancelled_urls[canonical].is_set()


def test_prefetch_circuit_breaker_disables_after_connection_failures(
        monkeypatch, pf_proxy):
    proxy = pf_proxy({"enabled": True, "depth": 1, "workers": 1})
    monkeypatch.setattr(mp, "_fetch_upstream",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            requests.ConnectionError("down")))
    for _ in range(3):
        proxy._prefetch_one("key", "http://cdn.test/001.ts", {}, False)
    assert proxy._pf_disabled is True


class _BlockingRaw:
    def __init__(self):
        self.closed = threading.Event()

    def read(self, _size):
        self.closed.wait(2)
        if self.closed.is_set():
            raise OSError("closed")
        return b""


class _FakeResponse:
    def __init__(self, status=200, raw=None):
        self.status_code = status
        self.headers = {"Content-Length": "1"}
        self.raw = raw or type("Raw", (), {"read": lambda self, size: b""})()
        self.closed = False

    def close(self):
        self.closed = True
        if hasattr(self.raw, "closed") and isinstance(self.raw.closed, threading.Event):
            self.raw.closed.set()


@pytest.mark.parametrize("status", [403, 429])
def test_prefetch_http_statuses_open_circuit(monkeypatch, pf_proxy, status):
    proxy = pf_proxy({"enabled": True, "depth": 1, "workers": 1})
    response = _FakeResponse(status=status)
    monkeypatch.setattr(mp, "_fetch_upstream",
                        lambda *args, **kwargs: response)
    proxy._prefetch_one("key", "http://cdn.test/status.ts", {}, False)
    assert proxy._pf_disabled is True
    assert response.closed is True


def test_prefetch_passes_actual_bounded_timeout(monkeypatch, pf_proxy):
    proxy = pf_proxy({"enabled": True, "depth": 1, "workers": 1})
    seen = {}

    def _fetch(*args, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        return _FakeResponse()

    monkeypatch.setattr(mp, "_fetch_upstream", _fetch)
    proxy._prefetch_one("key", "http://cdn.test/timeout.ts", {}, False)
    assert all(0 < value <= mp._PREFETCH_TIMEOUT for value in seen["timeout"])


def test_stop_closes_active_prefetch_and_cleans_marker(monkeypatch, pf_proxy):
    proxy = pf_proxy({"enabled": True, "depth": 1, "workers": 1})
    raw = _BlockingRaw()
    response = _FakeResponse(raw=raw)
    monkeypatch.setattr(mp, "_fetch_upstream",
                        lambda *args, **kwargs: response)
    url = "http://cdn.test/active.ts"
    canonical = proxy._cache.key_of(url)
    with proxy._pf_lock:
        proxy._pf_scheduled.add(canonical)
        proxy._pf_cancelled_urls[canonical] = threading.Event()
    future = proxy._ensure_pf_pool().submit(
        proxy._prefetch_one, "key", url, {}, False)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with proxy._pf_lock:
            if canonical in proxy._pf_responses:
                break
        time.sleep(0.01)
    proxy.stop()
    assert future.result(timeout=2) is None
    assert response.closed is True
    assert raw.closed.is_set()
    assert not proxy._cache.hls_segment_part("key", url).exists()
    assert canonical not in proxy._cache._inflight
    with proxy._pf_lock:
        assert canonical not in proxy._pf_scheduled


def test_prefetch_timeout_cleans_response_and_marker(monkeypatch, pf_proxy):
    proxy = pf_proxy({"enabled": True, "depth": 1, "workers": 1})
    monkeypatch.setattr(mp, "_PREFETCH_TIMEOUT", 0.1)
    monkeypatch.setattr(mp, "_get_direct_session", _direct_session)
    monkeypatch.setattr(mp, "_get_session", _direct_session)

    class _StalledBody(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", "1024")
            self.end_headers()
            self.wfile.write(b"x")
            self.wfile.flush()
            time.sleep(1)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _StalledBody)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/stalled.ts"
        cache_key = "stalled-cache"
        canonical = proxy._cache.key_of(url)
        with proxy._pf_lock:
            proxy._pf_scheduled.add(canonical)
            proxy._pf_cancelled_urls[canonical] = threading.Event()
        started = time.monotonic()
        future = proxy._ensure_pf_pool().submit(
            proxy._prefetch_one, cache_key, url, {}, False)
        assert future.result(timeout=1) is None
        elapsed = time.monotonic() - started
        assert elapsed < 0.75
        part = proxy._cache.hls_segment_part(cache_key, url)
        assert not part.exists()
        with proxy._pf_lock:
            assert canonical not in proxy._pf_scheduled
            assert canonical not in proxy._pf_responses
        assert canonical not in proxy._cache._inflight
    finally:
        server.shutdown()
        server.server_close()


def test_active_prefetch_does_not_block_player_http_request(monkeypatch, pf_proxy):
    proxy = pf_proxy({"enabled": True, "depth": 1, "workers": 1})
    cache = proxy._cache
    cache_key = "player-priority"
    target = "http://cdn.test/priority.ts"
    blocked = _BlockingRaw()
    prefetch_response = _FakeResponse(raw=blocked)
    class _PlayerRaw:
        def __init__(self):
            self.done = False

        def read(self, _size):
            if self.done:
                return b""
            self.done = True
            return _SEG

    player_response = _FakeResponse(raw=_PlayerRaw())
    player_response.headers["Content-Length"] = str(len(_SEG))
    calls = []

    def _fetch(*args, **kwargs):
        calls.append(kwargs)
        return prefetch_response if len(calls) == 1 else player_response

    monkeypatch.setattr(mp, "_fetch_upstream", _fetch)
    proxy._register_cache_ctx(cache_key, target, {}, None)
    canonical = cache.key_of(target)
    with proxy._pf_lock:
        proxy._pf_scheduled.add(canonical)
        proxy._pf_cancelled_urls[canonical] = threading.Event()
    future = proxy._ensure_pf_pool().submit(
        proxy._prefetch_one, cache_key, target, {}, False)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with proxy._pf_lock:
            if canonical in proxy._pf_responses:
                break
        time.sleep(0.01)
    port = proxy._server.server_address[1]
    started = time.monotonic()
    response = requests.get(
        f"http://127.0.0.1:{port}/c/{cache_key}/priority.ts", timeout=1)
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert response.content == _SEG
    assert elapsed < 0.5
    assert blocked.closed.is_set()
    assert future.result(timeout=1) is None


def test_prefetch_path_lock_does_not_deadlock_player(monkeypatch, pf_proxy):
    proxy = pf_proxy({"enabled": True, "depth": 1, "workers": 1})
    response = _FakeResponse()
    monkeypatch.setattr(mp, "_fetch_upstream",
                        lambda *args, **kwargs: response)
    cache = proxy._cache
    url = "http://cdn.test/lock.ts"
    canonical = cache.key_of(url)
    with proxy._pf_lock:
        proxy._pf_scheduled.add(canonical)
        proxy._pf_cancelled_urls[canonical] = threading.Event()
    lock = cache.path_lock(url)
    lock.acquire()
    try:
        future = proxy._ensure_pf_pool().submit(
            proxy._prefetch_one, "key", url, {}, False)
        proxy._cancel_prefetch_url(url)
    finally:
        lock.release()
    assert future.result(timeout=2) is None


# --------------------------------------------------------------------------- #
# 4. 慢速 CDN 的预取预算（agedm / 18av 实测）
#
# 实测（真实缓存分片到达时间 + 清单时长对齐）：
# - CDN 单连接 ~100KB/s，分片 0.8~3.7MB；2.5MB 的片子要 25s 才下完。
# - 18av 一集里 61/128 次交付**慢于该分片自身的时长**，最差 23.9s 下 10.4s
#   的内容；同一 CDN 也有 4.66× 实时的时段 → 抖动来自节点，不是总量不足。
# - agedm（AGE动漫）走 iframe 解析出的 `3000k/hls/mixed.m3u8`，落盘间隔
#   7/8/8.2s，中途还出现 20.5s 空档。
#
# 旧实现给预取分片**固定 10s 总预算**：这些分片必然超时 → 中止 → 播放器
# 重下（多等一个 10s）→ 连续 3 次把整场播放的预取熔断，之后只剩单连接
# 顺序拉片，表现为持续卡顿和掉帧。
# --------------------------------------------------------------------------- #
def test_prefetch_budget_scales_with_segment_size():
    """预算按 Content-Length 换算：小片维持 10s，大分片放宽，封顶 180s。"""
    assert mp._prefetch_budget(None) == mp._PREFETCH_TIMEOUT
    assert mp._prefetch_budget(0) == mp._PREFETCH_TIMEOUT
    small = mp._prefetch_budget(100 * 1024)
    assert mp._PREFETCH_TIMEOUT < small < 20
    # 2500KB ÷ 32KB/s ≈ 78s：固定 10s 会把它判超时
    big = mp._prefetch_budget(2500 * 1024)
    assert 80 <= big <= 95
    assert mp._prefetch_budget(50 * 1024 * 1024) == mp._PREFETCH_MAX_TIMEOUT


class _ProgressThenStallRaw:
    """先正常吐一块，随后卡死：慢速 CDN 上「在动但超出预算」的真实形态。"""

    def __init__(self):
        self.reads = 0

    def read(self, _size):
        self.reads += 1
        if self.reads == 1:
            return b"x" * 4096
        raise requests.Timeout("stalled after progress")


def test_slow_progressing_prefetch_does_not_open_circuit(monkeypatch, pf_proxy):
    """慢但在动的分片不能记失败——否则 3 次就把整场预取熔断。"""
    proxy = pf_proxy({"enabled": True, "depth": 1, "workers": 1})
    response = _FakeResponse()
    response.headers = {"Content-Length": str(100 * 1024)}
    response.raw = _ProgressThenStallRaw()
    monkeypatch.setattr(mp, "_fetch_upstream", lambda *a, **k: response)
    for _ in range(5):
        proxy._prefetch_one("key", "http://cdn.test/slow.ts", {}, False)
    assert proxy._pf_failures == 0
    assert proxy._pf_disabled is False
    assert proxy._prefetch_enabled() is True


def test_no_bytes_prefetch_still_counts_as_failure(monkeypatch, pf_proxy):
    """一个字节都没到 = 连接坏了，仍然要计数并熔断（别放过真故障）。"""
    proxy = pf_proxy({"enabled": True, "depth": 1, "workers": 1})

    class _StallRaw:
        def read(self, _size):
            raise requests.Timeout("no bytes")

    response = _FakeResponse()
    response.headers = {"Content-Length": str(100 * 1024)}
    response.raw = _StallRaw()
    monkeypatch.setattr(mp, "_fetch_upstream", lambda *a, **k: response)
    for _ in range(3):
        proxy._prefetch_one("key", "http://cdn.test/dead.ts", {}, False)
    assert proxy._pf_disabled is True


def test_truncated_prefetch_body_still_counts_as_failure(monkeypatch, pf_proxy):
    """上游提前收尾（体不全）不是「慢」，仍然要计数并熔断。"""
    proxy = pf_proxy({"enabled": True, "depth": 1, "workers": 1})

    class _ShortRaw:
        def __init__(self):
            self.reads = 0

        def read(self, _size):
            self.reads += 1
            return b"x" * 4096 if self.reads == 1 else b""

    response = _FakeResponse()
    response.headers = {"Content-Length": str(100 * 1024)}
    response.raw = _ShortRaw()
    monkeypatch.setattr(mp, "_fetch_upstream", lambda *a, **k: response)
    for _ in range(3):
        proxy._prefetch_one("key", "http://cdn.test/short.ts", {}, False)
    assert proxy._pf_disabled is True


@pytest.mark.parametrize("sid,min_depth,min_workers", [
    ("agedm", 8, 3),
    ("18av", 8, 3),
    ("omofuna", 6, 2),
])
def test_slow_cdn_sources_declare_playback_tuning(sid, min_depth, min_workers):
    """实测单连接 ~100KB/s 的源必须落盘抗抖参数，否则退回 8s 缓冲 + 4/2 浅预取。

    三者都在这台机器上被实测卡过：agedm（3000k 变体 ÷ 40~250KB/s）、
    18av（1.05× 实时、61/128 次交付慢于该片时长）、omofuna（分片 4~8s，
    峰值间隙 12.8s）。
    """
    p = Path(__file__).resolve().parent.parent / "sources" / f"{sid}.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    hls = (raw.get("media") or {}).get("hls") or {}
    prefetch = hls.get("prefetch") or {}
    assert prefetch.get("enabled") is True, f"{sid} 未开启播放预取"
    assert min_workers <= int(prefetch.get("workers", 0)) <= mp._PREFETCH_MAX_WORKERS
    assert min_depth <= int(prefetch.get("depth", 0)) <= mp._PREFETCH_MAX_DEPTH
    assert int(hls.get("network_caching_ms") or 0) >= 10000, f"{sid} 缓冲太小"


def test_hls_cache_floor_skips_mp4_direct(monkeypatch):
    """源级缓冲键叫 media.hls.* → mp4 直链配了也不该加（白等起播）。"""
    procs = _install_vlc(monkeypatch)
    ep.open_with_player("https://cdn.example.com/movie.mp4", caching_ms=15000)
    assert _caching_of(procs[0].args) == 1500


def test_hls_cache_floor_applies_when_format_lies(monkeypatch):
    """format=mp4 但真实地址是 m3u8（agedm）→ 仍按 HLS 加缓冲。"""
    procs = _install_vlc(monkeypatch)
    ep.open_with_player("https://cdn.example.com/3000k/hls/mixed.m3u8",
                        caching_ms=15000)
    assert _caching_of(procs[0].args) >= 15000


# --------------------------------------------------------------------------- #
# 4. HLS 分片转发必须跟随上游发包速率（不得出现 64KB 台阶停顿）
# --------------------------------------------------------------------------- #
# 真实根因回归：/c/ 分片转发循环此前用阻塞式的 raw.read(64KB)，要把 64KB
# 攒满才返回。慢 CDN 按 4KB 小包推流时，视频数据以 64KB 为台阶批量到达
# （每台阶约 300ms 停顿），视频轨道被周期性抽干 → 表现为「声音继续、画面
# 卡住」。mp4 路径早已改用 _read_available（read1 语义）。此测试固化该修复：
# 慢上游小包推流时，经代理转发的分片必须增量到达，单个 64KB 块内部的子包
# 交付间隔不应出现近 64KB 量级的台阶停顿。
_SLOW_PUSH = 4 * 1024       # 上游每次只推 4KB（模拟慢 CDN 小包）
_SLOW_SEG = 256 * 1024      # 一片 256KB
_SLOW_DELAY = 0.02          # 每 4KB 之间 20ms → 单连接 ~200KB/s


class _SlowCDN(BaseHTTPRequestHandler):
    """按 4KB/20ms 小包推流，复现慢 CDN 的发包节奏。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        if self.path.endswith(".m3u8"):
            body = (
                "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
                "#EXTINF:10.0,\n000.ts\n#EXT-X-ENDLIST\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(_SLOW_SEG))
        self.end_headers()
        sent = 0
        while sent < _SLOW_SEG:
            self.wfile.write(b"\x47" * _SLOW_PUSH)
            self.wfile.flush()
            sent += _SLOW_PUSH
            time.sleep(_SLOW_DELAY)


@pytest.fixture
def slow_upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _SlowCDN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_hls_segment_delivery_follows_upstream_rate(monkeypatch, slow_upstream,
                                                    tmp_path):
    """慢上游下经代理转发的分片必须增量到达，不应出现 ~64KB 台阶停顿。

    read(n) 会凑满 64KB 才返回 → 交付曲线是 64KB 一级的台阶（每级等约 16 个
    4KB 包 ≈ 320ms）；read1 语义下曲线跟随上游发包速率平滑上升，子包间隔接近
    上游的 20ms。这里断言：转发后的字节块里，相邻 4KB 子块的交付间隔中位值
    远小于「攒满 64KB 所需的 16 包时间」（= 16 × 20ms = 320ms），即没有出现
    台阶停顿。
    """
    monkeypatch.setattr(mp, "_get_direct_session", _direct_session)
    monkeypatch.setattr(mp, "_get_session", _direct_session)
    cache = MediaCache(root=tmp_path / "v", enabled=True)
    proxy = MediaProxy(cache=cache, prefetch={"enabled": False})
    proxy._ensure_server()
    try:
        seg_url = [l for l in _get(proxy.build_url(
            f"{slow_upstream}/index.m3u8", {"User-Agent": "t"}, None))[1]
            .decode().splitlines() if l.endswith(".ts")][0]

        events = []      # (到达时刻秒, 累积字节)
        t0 = time.perf_counter()
        got = 0
        with requests.get(seg_url, stream=True, timeout=30) as r:
            for chunk in r.iter_content(_SLOW_PUSH):
                got += len(chunk)
                events.append((time.perf_counter() - t0, got))
                if got >= _SLOW_SEG:
                    break
        assert got == _SLOW_SEG, got

        # 相邻子包（4KB）的交付间隔
        gaps = [(events[i][0] - events[i - 1][0]) * 1000
                for i in range(1, len(events))]
        median_gap = sorted(gaps)[len(gaps) // 2]
        # 阶梯停顿阈值：攒满 64KB 所需时间 ≈ 64/4 × 20ms = 320ms。
        # 修复后中位间隔应远小于此值；给 5x 余量（64ms）稳妥判定「无台阶」。
        assert median_gap < 64, (
            f"分片交付出现台阶停顿：中位子包间隔 {median_gap:.1f}ms，"
            f"接近 64KB 攒满所需的 ~320ms，视频轨道会被周期性抽干")
        # 反向确认：最大间隔也不应逼近整段 64KB（保守上限 300ms）
        assert max(gaps) < 300, f"最大子包间隔 {max(gaps):.1f}ms 疑似台阶停顿"
    finally:
        proxy.stop()

