# -*- coding: utf-8 -*-
"""media_proxy 租约制令牌生命周期：VLC 存活期间看门狗不得回收代理。"""
import time

import pytest

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
