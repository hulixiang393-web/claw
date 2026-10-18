"""换源恢复 0 请求：should_restore / LRU / 缺失分支。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.discover_session import (
    should_restore,
    register_source,
    snapshot_key,
    make_snapshot,
    prune_excess,
)
from framework.cache_service import RedisLikeStore


def test_should_restore_true_with_valid_snapshot():
    store = RedisLikeStore(quota=1024 * 1024)
    register_source(store, "srcA")
    store.set(snapshot_key("srcA"), make_snapshot(
        cat_buttons=[], cat_url="/all", works=[], pages=[1, 2],
        has_more=True, current_page=2, scroll=0.5, ts=1.0,
    ))
    assert should_restore(store, "srcA") is True


def test_should_restore_false_when_missing_or_broken():
    store = RedisLikeStore(quota=1024 * 1024)
    assert should_restore(store, "srcZ") is False      # 无快照
    store.set(snapshot_key("srcZ"), {"partial": True})  # 结构残缺
    assert should_restore(store, "srcZ") is False
    assert should_restore(None, "srcA") is False       # store 为 None


def test_should_restore_false_after_prune_excess():
    store = RedisLikeStore(quota=1024 * 1024)
    for i in range(1, 7):          # 6 个源 → s1 被逐出
        register_source(store, f"s{i}")
        store.set(snapshot_key(f"s{i}"), make_snapshot(
            cat_buttons=[], cat_url="/all", works=[], pages=[],
            has_more=False, current_page=0, scroll=0.0, ts=1.0,
        ))
    assert should_restore(store, "s1") is False       # 最旧被逐出
    assert should_restore(store, "s5") is True        # 仍保留
    assert should_restore(store, "s6") is True        # 最新保留