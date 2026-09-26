# -*- coding: utf-8 -*-
"""media_proxy 租约制令牌生命周期：VLC 存活期间看门狗不得回收代理。"""
import time

import framework.media_proxy as mp
from framework.media_proxy import MediaProxy


class _OffCache:
    enabled = False


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


def test_idle_expired_skipped_while_leased(monkeypatch):
    """有租约时即使远超空闲超时也不判过期（VLC 暂停超 10 分钟不断流）。"""
    monkeypatch.setattr(mp, "_IDLE_TIMEOUT", 0.01)
    proxy = MediaProxy(cache=_OffCache())
    proxy._ensure_server()
    proxy._last_use = time.time() - 999          # 早已空闲超时
    assert proxy._idle_expired() is True         # 无租约 → 判过期
    lid = proxy.acquire_lease()
    assert proxy._idle_expired() is False        # 有租约 → 不回收
    proxy.release_lease(lid)
    assert proxy._idle_expired() is True         # 释放后恢复回收


def test_idle_expired_false_without_server():
    """代理未起 → 不判过期（避免构造期误触发 stop）。"""
    proxy = MediaProxy(cache=_OffCache())
    proxy._last_use = time.time() - 999
    assert proxy._idle_expired() is False
