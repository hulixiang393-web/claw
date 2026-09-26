# -*- coding: utf-8 -*-
"""media_proxy 租约制令牌生命周期 + 惰性系列注册表 /e/<key>/<idx>。"""
import time

import pytest
import requests

import framework.media_proxy as mp
from framework.media_proxy import MediaProxy


class _OffCache:
    enabled = False


@pytest.fixture
def proxy_ctx():
    """起真实代理的用例必须在收尾停掉它。

    _ensure_server() 会绑 ThreadingHTTPServer + serve_forever daemon 线程；
    不停就留到会话末尾（只靠 atexit 兜底），端口和线程会越积越多。
    与 tests/test_media_cache.py 的 proxy_ctx 同一写法。
    """
    proxy = MediaProxy(cache=_OffCache())
    yield proxy
    proxy.stop()
    proxy._leases.clear()


def test_lease_ids_unique_and_releasable():
    proxy = MediaProxy(cache=_OffCache())
    a = proxy.acquire_lease()
    b = proxy.acquire_lease()
    assert a and b and a != b
    proxy.release_lease(a)
    assert a not in proxy._leases
    assert b in proxy._leases
    # 幂等：重复释放不抛异常
    proxy.release_lease(a)
    assert b in proxy._leases


def test_idle_expired_skipped_while_leased(proxy_ctx, monkeypatch):
    """有租约时即使远超空闲超时也不判过期（VLC 暂停超 10 分钟不断流）。

    用 monkeypatch 把 _IDLE_TIMEOUT 调小并把 _last_use 设在 patched 值之内，
    这样断言真的证明了「判据在调用时读全局 _IDLE_TIMEOUT」，
    而不是靠 999 秒这个魔法数在 600s 默认值下也能过。
    """
    monkeypatch.setattr(mp, "_IDLE_TIMEOUT", 30.0)
    proxy = proxy_ctx
    proxy._ensure_server()
    proxy._last_use = time.time() - mp._IDLE_TIMEOUT - 1   # 刚好越过阈值
    assert proxy._idle_expired() is True         # 无租约 → 判过期
    lid = proxy.acquire_lease()
    assert proxy._idle_expired() is False        # 有租约 → 不回收
    proxy.release_lease(lid)
    assert proxy._idle_expired() is True         # 释放后恢复回收


def test_release_lease_with_unknown_id_is_noop():
    """释放从未获取过的 id：静默无副作用（discard 语义，不抛异常）。"""
    proxy = MediaProxy(cache=_OffCache())
    proxy.release_lease("never-acquired")
    assert not proxy._leases


def test_stop_works_while_leased(proxy_ctx):
    """**整个设计依赖的属性**：持租约时 stop() 仍必须关闭代理。

    stop() 是唯一清 _tokens 的地方；若它日后变成租约感知，App 退出就会挂住。
    """
    proxy = proxy_ctx
    proxy.acquire_lease()
    proxy._ensure_server()
    assert proxy._server is not None
    proxy.stop()
    assert proxy._server is None


def test_idle_expired_false_without_server():
    """代理未起 → 不判过期（避免构造期误触发 stop）。"""
    proxy = MediaProxy(cache=_OffCache())
    proxy._last_use = time.time() - 999
    assert proxy._idle_expired() is False


# ---------------------------------------------------------------------- #
# 惰性系列：/e/<key>/<idx> 按集解析 + memo + 302 到 /s/<token>
# ---------------------------------------------------------------------- #
def _series(proxy, resolver, on_play=None, count=3, force_proxy=False):
    """在 proxy_ctx 起的代理上注册一支系列，返回 (proxy, key, 逐集 URL)。

    代理由 proxy_ctx 收尾 stop()，不用在本辅助里另起实例。
    """
    key = proxy.register_series(resolver, on_play, count=count,
                                force_proxy=force_proxy)
    urls = [proxy.series_episode_url(key, i) for i in range(count)]
    return proxy, key, urls


def test_series_url_shape(proxy_ctx):
    proxy, key, urls = _series(proxy_ctx, lambda i: ("v", "a", {}, None), count=2)
    port = proxy._server.server_address[1]
    assert urls[0] == f"http://127.0.0.1:{port}/e/{key}/0"
    assert urls[1] == f"http://127.0.0.1:{port}/e/{key}/1"
    assert len(key) == 12


def test_episode_redirects_to_token_url(proxy_ctx):
    calls = []

    def _resolve(idx):
        calls.append(idx)
        return f"https://cdn.example.com/e{idx}.m3u8", "", {"Referer": "r"}, None

    proxy, _key, urls = _series(proxy_ctx, _resolve, count=3)
    r = requests.get(urls[1], allow_redirects=False, timeout=5)
    assert r.status_code == 302
    assert r.headers["Cache-Control"] == "no-store"
    # Location 必须是 build_url 产出的 /s/<token>，不是 /e/ 本身
    assert "/s/" in r.headers["Location"]
    assert calls == [1]


def test_episode_memoized(proxy_ctx):
    calls = []

    def _resolve(idx):
        calls.append(idx)
        return f"https://cdn.example.com/e{idx}.m3u8", "", {}, None

    proxy, _key, urls = _series(proxy_ctx, _resolve, count=2)
    first = requests.get(urls[0], allow_redirects=False, timeout=5)
    second = requests.get(urls[0], allow_redirects=False, timeout=5)
    assert first.headers["Location"] == second.headers["Location"]
    assert calls == [0]          # 重复请求不再回源


def test_resolver_failure_502_and_not_memoized(proxy_ctx):
    state = {"n": 0}

    def _resolve(idx):
        state["n"] += 1
        if state["n"] == 1:
            raise ValueError("boom")
        return "https://cdn.example.com/ok.m3u8", "", {}, None

    proxy, _key, urls = _series(proxy_ctx, _resolve, count=1)
    r1 = requests.get(urls[0], allow_redirects=False, timeout=5)
    assert r1.status_code == 502
    r2 = requests.get(urls[0], allow_redirects=False, timeout=5)
    assert r2.status_code == 302  # 失败不写 memo → 允许重试
    assert state["n"] == 2


def test_bad_index_and_key_404_without_resolving(proxy_ctx):
    calls = []

    def _resolve(idx):
        calls.append(idx)
        return "https://cdn.example.com/x.m3u8", "", {}, None

    proxy, key, urls = _series(proxy_ctx, _resolve, count=3)
    port = proxy._server.server_address[1]
    for path in (f"/e/{key}/3", f"/e/{key}/-1", f"/e/{key}/abc",
                 f"/e/{key}/1/2", "/e/deadbeef/0", "/e//0", f"/e/{key}"):
        r = requests.get(f"http://127.0.0.1:{port}{path}",
                         allow_redirects=False, timeout=5)
        assert r.status_code == 404, path
    assert calls == []


def test_on_play_called_every_request(proxy_ctx):
    seen = []
    proxy, _key, urls = _series(
        proxy_ctx,
        lambda i: (f"https://cdn.example.com/{i}.m3u8", "", {}, None),
        on_play=lambda idx, v, a: seen.append((idx, v, a)), count=2)
    requests.get(urls[0], allow_redirects=False, timeout=5)
    requests.get(urls[0], allow_redirects=False, timeout=5)
    requests.get(urls[1], allow_redirects=False, timeout=5)
    assert [s[0] for s in seen] == [0, 0, 1]
    assert seen[0][1] == "https://cdn.example.com/0.m3u8"


def test_unregister_series_404_after(proxy_ctx):
    calls = []
    proxy, key, urls = _series(
        proxy_ctx,
        lambda i: (calls.append(i) or "https://cdn.example.com/x.m3u8", "", {}, None),
        count=1)
    proxy.unregister_series(key)
    r = requests.get(urls[0], allow_redirects=False, timeout=5)
    assert r.status_code == 404
    assert calls == []
