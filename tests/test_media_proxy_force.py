# -*- coding: utf-8 -*-
"""media_proxy force_proxy：强制走系统代理、跳过直连探测。

默认行为：直连优先，失败后按 host 记忆并回退系统代理。force_proxy=True
始终只走系统代理；_PROXY_ONLY=True 保留为可测试的兼容开关。

覆盖：
- _fetch_upstream 单元：默认直连优先、失败记忆与代理回退、TTL 跳过直连
- force_proxy / _PROXY_ONLY：只走代理、不读写直连失败记忆
- 写入点：build_url / proxy_url_for / _register_cache_ctx 存四元组
- 端到端：默认直连，force 请求走代理；老三元组 token 兼容
"""
import re
import threading

import requests

import pytest

import framework.media_proxy as mp
from framework.media_proxy import MediaProxy

_FORCE_M3U8 = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:10\n"
    "#EXTINF:10.0,\n/seg/001.ts\n"
    "#EXTINF:10.0,\n/seg/002.ts\n"
    "#EXT-X-ENDLIST\n"
)


class _FakeResp:
    """最简上游响应：200 常量，close 幂等。"""

    status_code = 200

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _RespM3U8:
    """m3u8 播放列表响应（_forward_m3u8 走 resp.content + resp.headers.get）。"""

    status_code = 200
    headers = {
        "Content-Type": "application/vnd.apple.mpegurl",
        "Content-Length": str(len(_FORCE_M3U8.encode())),
    }
    content = _FORCE_M3U8.encode()

    def close(self):
        pass


class _FakeSession:
    """记录 get 调用的假会话：可选抛异常 / 固定响应。"""

    def __init__(self, log, resp=_FakeResp(), exc=None):
        self.log = log
        self.resp = resp
        self.exc = exc

    def get(self, *a, **k):
        self.log.append((a, k))
        if self.exc is not None:
            raise self.exc
        return self.resp


class _OffCache:
    """注入的关闭缓存：让代理跳过落盘/命中路径，聚焦转发行为。"""

    enabled = False


@pytest.fixture(autouse=True)
def _clean_direct_fail():
    with mp._DIRECT_FAIL_LOCK:
        mp._DIRECT_FAIL.clear()
    yield
    with mp._DIRECT_FAIL_LOCK:
        mp._DIRECT_FAIL.clear()


def _install_sessions(monkeypatch, dlog, plog, *, dresp=_FakeResp(),
                      presp=_FakeResp(), dex=None, pex=None):
    monkeypatch.setattr(mp, "_get_direct_session",
                        lambda: _FakeSession(dlog, dresp, dex))
    monkeypatch.setattr(mp, "_get_session",
                        lambda: _FakeSession(plog, presp, pex))


# --------------------------------------------------------------------- #
# _fetch_upstream 单元
# --------------------------------------------------------------------- #
def test_force_proxy_skips_direct(monkeypatch):
    dlog, plog = [], []
    _install_sessions(monkeypatch, dlog, plog)
    resp = mp._fetch_upstream("https://cdn.example.com/x.mp4",
                              {"Referer": "r"}, force_proxy=True)
    assert resp.status_code == 200
    assert dlog == []              # 不做直连探测
    assert len(plog) == 1          # 直接系统代理会话
    assert mp._DIRECT_FAIL == {}   # 不写失败记忆


def test_force_proxy_no_fail_memory_even_if_direct_poisoned(monkeypatch):
    """即使直连会话一碰就炸，force 也只走代理、不读不写 _DIRECT_FAIL。"""
    dlog, plog = [], []
    _install_sessions(monkeypatch, dlog, plog, dex=requests.ConnectionError())
    mp._fetch_upstream("https://cdn.example.com/x.mp4", {},
                       force_proxy=True)
    assert dlog == []
    assert len(plog) == 1
    assert mp._DIRECT_FAIL == {}


def test_default_direct_success_does_not_call_proxy(monkeypatch):
    dlog, plog = [], []
    _install_sessions(monkeypatch, dlog, plog)
    resp = mp._fetch_upstream("https://cdn.example.com/x.mp4", {})
    assert resp.status_code == 200
    assert len(dlog) == 1
    assert plog == []
    assert mp._DIRECT_FAIL == {}


@pytest.mark.parametrize("exc", [
    requests.ConnectionError(),
    requests.Timeout(),
    requests.exceptions.SSLError(),
])
def test_default_direct_request_exception_records_failure_and_uses_proxy(
        monkeypatch, exc):
    dlog, plog = [], []
    _install_sessions(monkeypatch, dlog, plog, dex=exc)
    resp = mp._fetch_upstream("https://cdn.example.com/x.mp4", {})
    assert resp.status_code == 200
    assert len(dlog) == 1
    assert len(plog) == 1
    assert mp._DIRECT_FAIL.get("cdn.example.com") is not None


def test_default_direct_http_error_closes_records_and_uses_proxy(monkeypatch):
    dlog, plog = [], []
    direct = _FakeResp403()
    _install_sessions(monkeypatch, dlog, plog, dresp=direct)
    resp = mp._fetch_upstream("https://cdn.example.com/x.mp4", {})
    assert resp.status_code == 200
    assert direct.closed is True
    assert len(dlog) == 1
    assert len(plog) == 1
    assert mp._DIRECT_FAIL.get("cdn.example.com") is not None


def test_direct_failure_ttl_skips_direct_and_uses_proxy(monkeypatch):
    dlog, plog = [], []
    _install_sessions(monkeypatch, dlog, plog)
    mp._DIRECT_FAIL["cdn.example.com"] = mp.time.time()
    resp = mp._fetch_upstream("https://cdn.example.com/x.mp4", {})
    assert resp.status_code == 200
    assert dlog == []
    assert len(plog) == 1


def test_proxy_only_compatibility_switch_skips_direct(monkeypatch):
    dlog, plog = [], []
    _install_sessions(monkeypatch, dlog, plog)
    monkeypatch.setattr(mp, "_PROXY_ONLY", True)
    resp = mp._fetch_upstream("https://cdn.example.com/x.mp4", {})
    assert resp.status_code == 200
    assert dlog == []
    assert len(plog) == 1
    assert mp._DIRECT_FAIL == {}


def test_force_proxy_never_falls_back_to_direct(monkeypatch):
    """显式 force_proxy=True 的源：代理不通就报错，不悄悄走直连换出口。"""
    dlog, plog = [], []
    _install_sessions(monkeypatch, dlog, plog,
                      pex=requests.ConnectionError())
    with pytest.raises(requests.RequestException):
        mp._fetch_upstream("https://cdn.example.com/x.mp4", {},
                           force_proxy=True)
    assert dlog == []
    assert len(plog) == 2
    assert mp._DIRECT_FAIL == {}


class _FakeResp403:
    status_code = 403

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


# --------------------------------------------------------------------- #
# 上游路由诊断
# --------------------------------------------------------------------- #
def test_route_diagnostics_records_direct_success_without_sensitive_data(monkeypatch):
    dlog, plog = [], []
    _install_sessions(monkeypatch, dlog, plog)
    mp._reset_upstream_route_diagnostics()

    mp._fetch_upstream(
        "https://user:secret@cdn.example.com/video/index.m3u8?token=secret",
        {"Authorization": "Bearer secret"},
    )

    records = mp._read_upstream_route_diagnostics()
    assert len(records) == 1
    assert set(records[0]) == {
        "host", "request_kind", "route", "status_code", "elapsed_ms",
        "first_byte_ms", "throughput_bps", "failure_category",
    }
    assert records[0]["host"] == "cdn.example.com"
    assert records[0]["request_kind"] == "manifest"
    assert records[0]["route"] == "direct"
    assert records[0]["status_code"] == 200
    assert records[0]["failure_category"] is None
    assert all("secret" not in repr(value) for value in records[0].values())


def test_route_diagnostics_records_direct_failure_and_proxy_fallback(monkeypatch):
    dlog, plog = [], []
    _install_sessions(monkeypatch, dlog, plog, dex=requests.ConnectionError())
    mp._reset_upstream_route_diagnostics()

    mp._fetch_upstream("https://cdn.example.com/seg/001.ts", {})

    records = mp._read_upstream_route_diagnostics()
    assert [record["route"] for record in records] == ["direct", "proxy"]
    assert records[0]["request_kind"] == "segment"
    assert records[0]["status_code"] is None
    assert records[0]["failure_category"] == "direct_connection"
    assert records[1]["status_code"] == 200
    assert records[1]["failure_category"] is None


def test_route_diagnostics_classifies_request_kinds():
    assert mp._classify_upstream_request_kind("https://x/a.m3u8", {}) == "manifest"
    assert mp._classify_upstream_request_kind("https://x/key.bin", {"Accept": "*/*"}) == "key"
    assert mp._classify_upstream_request_kind("https://x/movie.mp4", {}) == "mp4"
    assert mp._classify_upstream_request_kind("https://x/segment.ts", {}) == "segment"
    assert mp._classify_upstream_request_kind("https://x/video", {"Range": "bytes=0-1"}) == "range"


def test_route_diagnostics_is_bounded_and_thread_safe(monkeypatch):
    dlog, plog = [], []
    _install_sessions(monkeypatch, dlog, plog)
    mp._reset_upstream_route_diagnostics()
    monkeypatch.setattr(mp, "_UPSTREAM_DIAGNOSTICS_MAX", 2)

    def fetch():
        mp._fetch_upstream("https://cdn.example.com/a.mp4", {})

    threads = [threading.Thread(target=fetch) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    records = mp._read_upstream_route_diagnostics()
    assert len(records) == 2
    mp._reset_upstream_route_diagnostics()
    assert mp._read_upstream_route_diagnostics() == []


def test_route_diagnostics_records_forced_proxy_failure(monkeypatch):
    dlog, plog = [], []
    _install_sessions(monkeypatch, dlog, plog, pex=requests.Timeout())
    mp._reset_upstream_route_diagnostics()

    with pytest.raises(requests.RequestException):
        mp._fetch_upstream("https://cdn.example.com/key", {}, force_proxy=True)

    records = mp._read_upstream_route_diagnostics()
    assert len(records) == 1
    assert records[0]["route"] == "proxy"
    assert records[0]["request_kind"] == "key"
    assert records[0]["failure_category"] == "proxy_timeout"


# --------------------------------------------------------------------- #
# 写入点：token / cache_ctx 四元组
# --------------------------------------------------------------------- #
def test_build_url_registers_force_proxy():
    proxy = MediaProxy(cache=_OffCache())
    try:
        local = proxy.build_url("https://cdn.example.com/x.mp4",
                                {"Referer": "r"}, force_proxy=True)
        token = local.rsplit("/", 1)[-1]
        entry = proxy._tokens[token]
        assert entry[0] == "https://cdn.example.com/x.mp4"
        assert entry[1] == {"Referer": "r"}
        assert entry[2] is None
        assert entry[3] is True      # 四元组最后一位带上 force_proxy
    finally:
        proxy.stop()


def test_build_url_default_force_proxy_false():
    proxy = MediaProxy(cache=_OffCache())
    try:
        local = proxy.build_url("https://cdn.example.com/x.mp4", {"Referer": "r"})
        token = local.rsplit("/", 1)[-1]
        assert proxy._tokens[token][3] is False  # 默认 False，向后兼容
    finally:
        proxy.stop()


def test_proxy_url_for_forwards_force_proxy(monkeypatch):
    calls = []

    class _FakeProxy:
        def build_url(self, url, headers=None, ad_block=None, force_proxy=False):
            calls.append((url, headers, ad_block, force_proxy))
            return "http://127.0.0.1:0/s/x"

    monkeypatch.setattr(mp.MediaProxy, "instance", classmethod(lambda cls: _FakeProxy()))
    out = mp.proxy_url_for("https://cdn.example.com/x.mp4", {"Referer": "r"},
                           ad_block={"enabled": True}, force_proxy=True)
    assert calls == [(
        "https://cdn.example.com/x.mp4", {"Referer": "r"},
        {"enabled": True}, True,
    )]
    assert out == "http://127.0.0.1:0/s/x"


def test_proxy_url_for_no_headers_no_proxy(monkeypatch):
    called = []

    class _FakeProxy:
        def build_url(self, *a, **k):
            called.append(a)
            return "x"

    monkeypatch.setattr(mp.MediaProxy, "instance", classmethod(lambda cls: _FakeProxy()))
    out = mp.proxy_url_for("https://cdn.example.com/x.mp4", None, force_proxy=True)
    assert out == "https://cdn.example.com/x.mp4"  # 无头 → 原 URL，不建代理
    assert called == []


def test_register_cache_ctx_stores_force_proxy():
    proxy = MediaProxy(cache=_OffCache())
    try:
        key = "k" * 40
        proxy._register_cache_ctx(key, "http://up.example/hls/i.m3u8",
                                  {"Referer": "r"}, {"enabled": False},
                                  force_proxy=True)
        with proxy._lock:
            ctx = proxy._cache_ctx[key]
        assert ctx[:3] == ("http://up.example/hls/i.m3u8",
                           {"Referer": "r"}, {"enabled": False})
        assert ctx[3] is True  # /c/<key> 回落上游沿用 force_proxy
    finally:
        proxy.stop()


# --------------------------------------------------------------------- #
# 端到端：真实本地代理服务器 + 假上游会话
# --------------------------------------------------------------------- #
@pytest.fixture
def force_e2e(monkeypatch):
    """MediaProxy + 假上游会话（记录直连/代理各被调用次数）。"""
    dlog, plog = [], []
    monkeypatch.setattr(mp, "_get_direct_session",
                        lambda: _FakeSession(dlog, _RespM3U8()))
    monkeypatch.setattr(mp, "_get_session",
                        lambda: _FakeSession(plog, _RespM3U8()))
    proxy = MediaProxy(cache=_OffCache())
    proxy._ensure_server()
    yield proxy, dlog, plog
    proxy.stop()


def test_e2e_force_proxy_request_uses_proxy_only(force_e2e):
    proxy, dlog, plog = force_e2e
    local = proxy.build_url("http://up.example/hls/i.m3u8",
                            {"Referer": "https://fake/"}, force_proxy=True)
    r = requests.get(local, timeout=10)
    assert r.status_code == 200
    assert "#EXTM3U" in r.text
    assert dlog == []                 # 未做任何直连探测
    assert len(plog) == 1             # 上游取回走系统代理
    assert mp._DIRECT_FAIL == {}      # 不写失败记忆
    # 重写后的内部分片 URL 也是 /s/<token>，token 同样带 force_proxy
    for t in re.findall(r"/s/([0-9a-f]+)", r.text):
        entry = proxy._tokens[t]
        assert len(entry) == 4 and entry[3] is True


def test_e2e_default_request_direct_first(force_e2e):
    proxy, dlog, plog = force_e2e
    local = proxy.build_url("http://up.example/hls/i.m3u8",
                            {"Referer": "https://fake/"})
    r = requests.get(local, timeout=10)
    assert r.status_code == 200
    assert len(dlog) == 1
    assert plog == []


def test_e2e_legacy_3tuple_token_compat(force_e2e):
    """老三元组 token 兼容解包，force_proxy 默认 False，按默认直连。"""
    proxy, dlog, plog = force_e2e
    token = "a" * 32
    with proxy._lock:
        proxy._tokens[token] = ("http://up.example/hls/i.m3u8",
                                {"Referer": "f"}, None)
    port = proxy._server.server_port
    r = requests.get(f"http://127.0.0.1:{port}/s/{token}", timeout=10)
    assert r.status_code == 200
    assert len(dlog) == 1   # 三元组 → _tuple_force_proxy 为 False → 直连优先
    assert plog == []


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))