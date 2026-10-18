"""framework/atomic_io.py 与缓存落盘反序列化安全的针对性测试。"""
from __future__ import annotations

import gzip
import os
import pickle
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework import atomic_io
from framework.cache_service import RedisLikeStore


# ---------------------------------------------------------------------- #
# F1 原子写盘
# ---------------------------------------------------------------------- #
def test_atomic_write_text_roundtrip(tmp_path):
    path = tmp_path / "a.txt"
    atomic_io.atomic_write_text(path, "内容")
    assert path.read_text(encoding="utf-8") == "内容"


def test_atomic_write_failure_preserves_original_and_cleans_tmp(tmp_path, monkeypatch):
    path = tmp_path / "a.json"
    atomic_io.atomic_write_json(path, {"v": 1})
    original = path.read_bytes()

    def boom(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(atomic_io.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_io.atomic_write_json(path, {"v": 2})
    assert path.read_bytes() == original
    assert [q for q in tmp_path.iterdir() if q.suffix == ".tmp"] == []


def test_read_text_lenient_falls_back(tmp_path):
    assert atomic_io.read_text_lenient(tmp_path / "missing.txt", default="d") == "d"


# ---------------------------------------------------------------------- #
# D2 缓存反序列化安全
# ---------------------------------------------------------------------- #
def _write_malicious_cache(path: str) -> None:
    """构造一个 __reduce__ 指向 os.system 的恶意 pickle 载荷。"""

    class _Evil:
        def __reduce__(self):
            return (os.system, ("echo pwned",))

    payload = {
        "data": [("k", _Evil())],
        "ttl": {},
        "bytes": {},
        "bytes_used": 0,
    }
    with open(path, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb") as gz:
            pickle.dump(payload, gz, protocol=4)


def test_malicious_cache_degrades_to_empty(tmp_path):
    path = str(tmp_path / "redis.gz")
    _write_malicious_cache(path)
    store = RedisLikeStore(quota=1024, persist_path=path)
    store.load()  # 不执行未知代码、不抛出
    assert store.count() == 0
    assert store.get("k") is None


def test_corrupt_cache_degrades_to_empty(tmp_path):
    path = tmp_path / "redis.gz"
    path.write_bytes(b"not a gzip stream")
    store = RedisLikeStore(quota=1024, persist_path=str(path))
    store.load()
    assert store.count() == 0


def test_structured_but_invalid_payload_is_rejected(tmp_path):
    path = tmp_path / "redis.gz"
    with open(path, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb") as gz:
            pickle.dump({"data": "not-a-list"}, gz, protocol=4)
    store = RedisLikeStore(quota=1024, persist_path=str(path))
    store.load()
    assert store.count() == 0


def test_legitimate_project_snapshot_roundtrips(tmp_path):
    from framework.discovery import Work

    path = str(tmp_path / "redis.gz")
    store = RedisLikeStore(quota=1 << 20, persist_path=path)
    store.set("disc:x", {"works": [Work(title="t", url="u")], "cats": [("全部", None)]})
    store.save()

    restored = RedisLikeStore(quota=1 << 20, persist_path=path)
    restored.load()
    snapshot = restored.get("disc:x")
    assert isinstance(snapshot["works"][0], Work)
    assert snapshot["works"][0].title == "t"
