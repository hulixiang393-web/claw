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
    """ikanpp 传 30000 → VLC 用 30s 缓冲。"""
    procs = _install_vlc(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8", caching_ms=30000)
    assert _caching_of(procs[0].args) == 30000


def test_override_is_floor_not_cap(monkeypatch):
    """覆盖值当下限用：配小了也不该把分类算出的更大值降级。"""
    procs = _install_vlc(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8", caching_ms=1000)
    assert _caching_of(procs[0].args) == 8000


def test_ikanpp_source_declares_30s():
    """ikanpp 源配置里落 30s，否则上面的参数根本没人传。"""
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
    proxy = MediaProxy(cache=cache)
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
