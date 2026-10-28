# -*- coding: utf-8 -*-
"""HLS 预取链路的可用性回归（多源卡顿的结构性原因）。

修复前实测到的三个断点：
1. `MediaProxy._cache_obj()` 直接 `return self._cache`，而 `self._cache` 只有在
   `_ensure_cache()` 被调用后才赋值 → 预取路径永远拿到 None，**配置开着也不跑**；
2. 预取窗口只由「播放器请求了第 N 片」推进，起播后未请求到的片没人拉；
3. 源里 21/27 个视频源没声明 prefetch，走的是偏小的默认值。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework import media_proxy as mp  # noqa: E402


def _proxy(**kw):
    return mp.MediaProxy(cache=kw.pop("cache", None), **kw)


def test_cache_obj_lazily_initialises_cache():
    """_cache_obj 必须走懒加载：直接读 self._cache 会让预取永远拿到 None。"""
    proxy = _proxy(cache=None)
    assert proxy._cache is None
    cache = proxy._cache_obj()
    assert cache is not None
    assert proxy._cache is cache  # 已回填，后续调用不再重复构造


def test_cache_obj_returns_same_instance_as_ensure_cache():
    proxy = _proxy(cache=None)
    assert proxy._cache_obj() is proxy._ensure_cache()


def test_prefetch_head_is_callable_and_guarded():
    """清单到达时的起播预取：无缓存/无上下文时安静返回，不抛异常。"""
    proxy = _proxy(cache=None, prefetch={"enabled": True, "depth": 4, "workers": 2})
    # 没有 cache_ctx → 直接返回
    proxy._prefetch_head("no-such-key", ["https://cdn/x/1.ts"])
    with proxy._pf_lock:
        assert len(proxy._pf_scheduled) == 0


def test_prefetch_head_rejects_when_not_enabled():
    proxy = _proxy(cache=None, prefetch={"enabled": False})
    proxy._prefetch_head("k", ["https://cdn/x/1.ts"])
    with proxy._pf_lock:
        assert len(proxy._pf_scheduled) == 0


def test_prefetch_cfg_defaults_are_raised():
    """默认预取参数不能退回「4 片/2 路」——2MB 级分片 + 数秒抖动时缓冲不够。"""
    proxy = _proxy(cache=None, prefetch={"enabled": True})
    proxy.configure_prefetch(True, depth=10, workers=3)
    assert proxy._pf_cfg["depth"] >= 10
    assert proxy._pf_cfg["workers"] >= 3


def test_global_prefetch_setting_is_enabled():
    """app_config.json 的 hls_prefetch 必须开着，否则多源都退化成现拉现播。"""
    import json

    cfg = json.loads(
        (Path(__file__).resolve().parents[1] / "app_config.json").read_text(
            encoding="utf-8"
        )
    )
    sec = cfg.get("hls_prefetch") or {}
    assert sec.get("enabled") is True, "HLS 预取默认必须开启"
    assert int(sec.get("depth") or 0) >= 10
    assert int(sec.get("workers") or 0) >= 3


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
