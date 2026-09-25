# -*- coding: utf-8 -*-
"""media_proxy force_proxy：强制走系统代理、跳过直连探测。

背景：hanime1 的 mp4 直连连到 vdownload.hembed.com，速度不稳（0.3~1MB/s
波动、偶发 ConnectionReset），但走系统代理稳定 1.8~2.6MB/s。旧逻辑「直连
优先、失败回退系统代理」在直连"半成功"（200 但慢）时不会回退 → 卡顿。

force_proxy=True 的行为：跳过直连探测与 _DIRECT_FAIL 读写，无条件把上游
请求交给系统代理会话（trust_env=True）。默认 False 行为与旧版完全一致。

覆盖：
- _fetch_upstream 单元：force 走代理、不碰直连、不写失败记忆；默认仍直连
  优先、直连失败回退并记记忆
- 写入点：build_url / proxy_url_for / _register_cache_ctx 存四元组
- 端到端（真实本地代理服务器 + 假上游会话）：/s/ 请求 force 时仅用系统
  代理；重写出的内部分片 token 同样带 force_proxy；老三元组 token 兼容
"""
import re

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


def test_default_still_direct_first(monkeypatch):
    dlog, plog = [], []
    _install_sessions(monkeypatch, dlog, plog)
    resp = mp._fetch_upstream("https://cdn.example.com/x.mp4", {})
    assert resp.status_code == 200
    assert len(dlog) == 1          # 直连优先
    assert plog == []
    assert mp._DIRECT_FAIL == {}


def test_default_fallback_on_direct_connect_failure(monkeypatch):
    dlog, plog = [], []
    _install_sessions(monkeypatch, dlog, plog, dex=requests.ConnectionError())
    resp = mp._fetch_upstream("https://cdn.example.com/x.mp4", {})
    assert resp.status_code == 200
    assert len(dlog) == 1          # 直连失败
    assert len(plog) == 1          # 回退系统代理
    assert mp._DIRECT_FAIL.get("cdn.example.com") is not None  # 记住失败


def test_default_fallback_on_direct_http_4xx(monkeypatch):
    dlog, plog = [], []
    _install_sessions(monkeypatch, dlog, plog, dresp=_FakeResp403())
    resp = mp._fetch_upstream("https://cdn.example.com/x.mp4", {})
    assert resp.status_code == 200
    assert len(dlog) == 1
    assert plog and len(plog) == 1  # 4xx 同样换代理出口
    assert mp._DIRECT_FAIL.get("cdn.example.com") is not None


class _FakeResp403:
    status_code = 403

    def close(self):
        pass


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
    assert len(dlog) == 1             # 默认直连优先
    assert plog == []


def test_e2e_legacy_3tuple_token_compat(force_e2e):
    """老三元组 token（硬编码写入模拟旧缓存）→ 兼容解包走默认直连。"""
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