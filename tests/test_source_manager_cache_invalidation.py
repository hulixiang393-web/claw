# -*- coding: utf-8 -*-
"""源健康→缓存失效 / 自动禁用持久化测试（test_source_manager_cache_invalidation.py）。

覆盖：
- D1：源迁入 broken 时同步失效该源缓存；手动禁用不触发；回调异常不影响状态机。
- D9：自动禁用集持久化到 sources_runtime.json，重启后恢复；损坏文件安全回退
  （不自动禁用任何源）；用户显式禁用的源不会被自动恢复路径打开。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.config import SourceConfig
from framework.source_manager import SourceManager


def _source(source_id: str, enabled: bool = True):
    raw = {
        "$schema_version": 2,
        "$id": source_id,
        "$name": source_id,
        "$type": "novel",
        "transports": {"base_url": "https://example.test"},
        "endpoints": {"search": {"base_url": "/search", "method": "GET"}},
    }
    if not enabled:
        raw["$enabled"] = False
    return SourceConfig.from_dict(raw, f"<{source_id}>")


def _manager(tmp_path, runtime=None, invalidator=None):
    manager = SourceManager(
        health_file=tmp_path / "health.json",
        runtime_settings={"sources_runtime": runtime} if runtime else {},
    )
    manager.add(_source("sa"))
    if invalidator is not None:
        manager.set_cache_invalidator(invalidator)
    return manager


# ---------------------------------------------------------------------- #
# D1：broken 迁移 → 失效缓存
# ---------------------------------------------------------------------- #
def test_broken_transition_invalidates_source_cache(tmp_path):
    calls = []
    manager = _manager(tmp_path, invalidator=calls.append)
    manager.update_health("sa", "broken", "boom")
    assert calls == ["sa"]


def test_repeated_broken_does_not_reinvalidate(tmp_path):
    calls = []
    # 阈值设高，避免触发自动禁用路径的二次清理
    manager = _manager(
        tmp_path, runtime={"auto_disable_after_failures": 99}, invalidator=calls.append
    )
    manager.update_health("sa", "broken", "boom1")
    manager.update_health("sa", "broken", "boom2")
    manager.update_health("sa", "broken", "boom3")
    assert calls == ["sa"]  # 仅迁入 broken 时清理一次


def test_auto_disable_invalidates_source_cache(tmp_path):
    calls = []
    manager = _manager(
        tmp_path,
        runtime={"auto_disable_after_failures": 2},
        invalidator=calls.append,
    )
    manager.update_health("sa", "broken", "b1")
    manager.update_health("sa", "broken", "b2")
    assert manager.get("sa").enabled is False
    assert calls[0] == "sa" and "sa" in calls


def test_broken_then_recover_re_enables_and_can_reinvalidate(tmp_path):
    calls = []
    # 阈值设高：仅验证「迁入 broken 失效 / 恢复 / 再次迁入再失效」的状态迁移语义
    manager = _manager(
        tmp_path,
        runtime={"auto_disable_after_failures": 99},
        invalidator=calls.append,
    )
    manager.update_health("sa", "broken", "b1")
    assert calls.count("sa") == 1  # 迁入 broken
    manager.update_health("sa", "ok")
    assert manager.get("sa").enabled is True
    manager.update_health("sa", "broken", "b2")
    assert calls.count("sa") == 2  # 恢复后再次迁入 broken 会再次失效


def test_invalidator_exception_does_not_break_state_machine(tmp_path):
    def _boom(_sid):
        raise RuntimeError("cleanup failed")

    manager = _manager(
        tmp_path,
        runtime={"auto_disable_after_failures": 2},
        invalidator=_boom,
    )
    manager.update_health("sa", "broken", "b1")
    manager.update_health("sa", "broken", "b2")
    # 回调抛异常被吞掉，状态机照常（源被禁用）
    assert manager.get("sa").enabled is False


def test_manual_disable_does_not_invalidate(tmp_path):
    calls = []
    manager = _manager(tmp_path, invalidator=calls.append)
    manager.set_enabled("sa", False)
    assert calls == []


# ---------------------------------------------------------------------- #
# D9：自动禁用集持久化
# ---------------------------------------------------------------------- #
def _write_source_dir(tmp_path, source_id="sa", enabled=True):
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir(exist_ok=True)
    raw = {
        "$schema_version": 2,
        "$id": source_id,
        "$name": source_id,
        "$type": "novel",
        "transports": {"base_url": "https://example.test"},
        "endpoints": {"search": {"base_url": "/search", "method": "GET"}},
    }
    if not enabled:
        raw["$enabled"] = False
    (sources_dir / f"{source_id}.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )
    return sources_dir


def test_auto_disabled_persisted_and_restored(tmp_path):
    rt = {"auto_disable_after_failures": 2}
    manager = _manager(tmp_path, runtime=rt)
    manager.update_health("sa", "broken", "b1")
    manager.update_health("sa", "broken", "b2")
    runtime_file = tmp_path / "sources_runtime.json"
    assert runtime_file.is_file()
    assert json.loads(runtime_file.read_text(encoding="utf-8"))["auto_disabled"] == ["sa"]

    # 重启：新 manager 经 sources_dir 加载后再应用自动禁用集 → 源仍禁用
    sources_dir = _write_source_dir(tmp_path)
    restarted = SourceManager(
        sources_dir=sources_dir,
        health_file=tmp_path / "health.json",
        runtime_settings={"sources_runtime": rt},
    )
    assert restarted.get("sa").enabled is False


def test_corrupt_runtime_file_is_safe_default(tmp_path):
    runtime_file = tmp_path / "sources_runtime.json"
    runtime_file.write_text("{ not valid json", encoding="utf-8")
    sources_dir = _write_source_dir(tmp_path)
    manager = SourceManager(
        sources_dir=sources_dir,
        health_file=tmp_path / "health.json",
        runtime_settings={"sources_runtime": {"auto_disable_after_failures": 2}},
    )
    assert manager.get("sa").enabled is True  # 损坏 → 不自动禁用任何源


def test_user_disabled_source_not_reopened_by_auto_path(tmp_path):
    # 用户显式 $enabled=false：即使集合里有它，也不应被自动恢复路径打开
    runtime_file = tmp_path / "sources_runtime.json"
    runtime_file.write_text(json.dumps({"auto_disabled": ["sa"]}), encoding="utf-8")
    sources_dir = _write_source_dir(tmp_path, enabled=False)
    manager = SourceManager(
        sources_dir=sources_dir,
        health_file=tmp_path / "health.json",
        runtime_settings={"sources_runtime": {"auto_disable_after_failures": 2}},
    )
    assert manager.get("sa").enabled is False
    assert "sa" not in manager._auto_disabled  # 从集合移除并持久化
    assert json.loads(runtime_file.read_text(encoding="utf-8"))["auto_disabled"] == []


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
