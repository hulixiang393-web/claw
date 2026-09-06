"""Redis 风格缓存服务单元测试。"""
from __future__ import annotations

import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from framework.cache_service import RedisLikeStore


def make_store(tmp_path, quota=64 * 1024, persist=None):
    return RedisLikeStore(quota=quota, persist_path=persist)


def test_set_get(tmp_path):
    s = make_store(tmp_path)
    s.set("a", "hello")
    assert s.get("a") == "hello"
    assert s.get("missing") is None


def test_ttl_expiry(tmp_path):
    s = make_store(tmp_path)
    s.set("a", "x")
    s.expire("a", 0.001)
    time.sleep(0.05)
    assert s.get("a") is None
    s.set("b", "y", ttl=0.001)
    time.sleep(0.05)
    assert s.get("b") is None


def test_hset_hget(tmp_path):
    s = make_store(tmp_path)
    s.hset("book", "title", "A")
    s.hset("book", "author", "B")
    assert s.hget("book", "title") == "A"
    assert s.hgetall("book") == {"title": "A", "author": "B"}


def test_delete_and_clear(tmp_path):
    s = make_store(tmp_path)
    s.set("a", 1)
    s.set("b", 2)
    assert s.count() == 2
    s.delete("a")
    assert s.count() == 1
    s.clear()
    assert s.count() == 0


def test_scan_pattern(tmp_path):
    s = make_store(tmp_path)
    s.set("search:x:kw", 1)
    s.set("search:y:kw2", 2)
    s.set("cover:a:u", 3)
    keys = sorted(s.scan("search:*"))
    assert keys == ["search:x:kw", "search:y:kw2"]


def test_lru_eviction_and_bytes(tmp_path):
    s = RedisLikeStore(quota=5000)
    s.set("k1", "v" * 1000)
    s.set("k2", "v" * 1000)
    s.set("k3", "v" * 1000)
    assert s.bytes_used() > 0
    s.set("k4", "v" * 1000)
    assert s.get("k1") is None   # 最旧被驱逐
    assert s.get("k2") is not None or s.get("k3") is not None
    assert s.bytes_used() <= 5000


def test_persist_roundtrip(tmp_path):
    path = str(tmp_path / "r.gz")
    s = RedisLikeStore(quota=1024 * 1024, persist_path=path)
    s.set("a", {"x": [1, 2, 3]})
    s.hset("b", "f", "val")
    s.save()
    s2 = RedisLikeStore(quota=1024 * 1024, persist_path=path)
    s2.load()
    assert s2.get("a") == {"x": [1, 2, 3]}
    assert s2.hget("b", "f") == "val"


def test_flush_check_throttle(tmp_path):
    path = str(tmp_path / "r2.gz")
    s = RedisLikeStore(quota=1024 * 1024, persist_path=path)
    s._save_interval = 60.0
    s._save_write_threshold = 500
    s._last_save_ts = time.time()  # 最近刚存过 → 不触发超时
    s.set("a", "x")  # _writes = 1，低于阈值
    s.flush_checked()
    assert not Path(path).exists()  # 未落盘
    s._writes = 500
    s.flush_checked()  # 达阈值 → 落盘
    assert Path(path).exists()