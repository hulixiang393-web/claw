# -*- coding: utf-8 -*-
"""投屏设备历史记忆存储测试（test_cast_history.py）。

framework/cast_history.py：独立 JSON（data/cast_history.json）记录最近
投屏成功的设备，供「直投上次设备」与选择对话框置顶展示。

覆盖：序列化往返 / UDN 去重置顶 / 上限 10 条 / last / clear /
损坏与缺失容错 / 非 dict 条目容错 / 原子写入（无残留 tmp）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from framework.cast_history import (
    MAX_ENTRIES,
    CastHistory,
    renderer_from_entry,
)
from framework.dlna import DlnaRenderer


def _renderer(
    udn: str = "uuid:tv-1",
    name: str = "客厅电视",
    ip: str = "192.168.1.100",
    rc_url: str | None = "http://192.168.1.100:80/rc",
) -> DlnaRenderer:
    location = f"http://{ip}:80/root.xml"
    return DlnaRenderer(
        udn=udn,
        friendly_name=name,
        location=location,
        av_transport_control_url=f"{ip and 'http://' + ip}:80/avt",
        rendering_control_url=rc_url,
        service_type="urn:schemas-upnp-org:service:AVTransport:1",
    )


@pytest.fixture
def store(tmp_path):
    return CastHistory(tmp_path / "cast_history.json")


# ---------------------------------------------------------------------- #
# 序列化往返
# ---------------------------------------------------------------------- #
def test_record_serializes_all_renderer_fields(store, tmp_path):
    """record 写入 6 个 DlnaRenderer 字段 + 时间戳，可原样还原 renderer。"""
    renderer = _renderer()
    entry = store.record(renderer, now=1730000000.0)

    assert entry is not None
    assert entry["udn"] == renderer.udn
    assert entry["friendly_name"] == renderer.friendly_name
    assert entry["location"] == renderer.location
    assert entry["av_transport_control_url"] == renderer.av_transport_control_url
    assert entry["rendering_control_url"] == renderer.rendering_control_url
    assert entry["service_type"] == renderer.service_type
    assert entry["last_used"] == 1730000000.0

    # 落盘后可从文件重新读出同样的条目
    raw = json.loads((tmp_path / "cast_history.json").read_text(encoding="utf-8"))
    assert isinstance(raw, list) and len(raw) == 1
    assert raw[0]["udn"] == renderer.udn

    restored = renderer_from_entry(raw[0])
    assert restored == renderer


def test_record_accepts_renderer_without_rendering_control(store):
    """rendering_control_url 为 None 的设备同样能记录与还原。"""
    renderer = _renderer(rc_url=None)
    entry = store.record(renderer)
    assert entry["rendering_control_url"] is None
    assert renderer_from_entry(entry) == renderer


def test_entries_return_copies(store):
    """entries()/last() 返回副本：调用方改不动内部状态。"""
    store.record(_renderer())
    entries = store.entries()
    entries[0]["udn"] = "uuid:tampered"
    assert store.entries()[0]["udn"] == "uuid:tv-1"
    store.last()["udn"] = "uuid:tampered"
    assert store.last()["udn"] == "uuid:tv-1"


# ---------------------------------------------------------------------- #
# UDN 去重置顶
# ---------------------------------------------------------------------- #
def test_record_dedups_and_moves_udn_to_top(store):
    """重复 UDN 不新增条目，而是置顶并刷新时间戳。"""
    store.record(_renderer(udn="uuid:tv-1"), now=100.0)
    store.record(_renderer(udn="uuid:tv-2"), now=200.0)
    store.record(_renderer(udn="uuid:tv-1", ip="192.168.1.55"), now=300.0)

    entries = store.entries()
    assert [e["udn"] for e in entries] == ["uuid:tv-1", "uuid:tv-2"]
    assert entries[0]["last_used"] == 300.0
    assert entries[0]["location"] == "http://192.168.1.55:80/root.xml"


def test_last_returns_most_recent_entry(store):
    assert store.last() is None
    store.record(_renderer(udn="uuid:tv-1"), now=100.0)
    store.record(_renderer(udn="uuid:tv-2"), now=200.0)
    assert store.last()["udn"] == "uuid:tv-2"


# ---------------------------------------------------------------------- #
# 上限
# ---------------------------------------------------------------------- #
def test_history_caps_at_max_entries(store):
    """超过 MAX_ENTRIES：只留最近 10 条，最旧的被淘汰。"""
    for i in range(MAX_ENTRIES + 2):
        store.record(_renderer(udn=f"uuid:tv-{i}"), now=float(i))

    entries = store.entries()
    assert len(entries) == MAX_ENTRIES
    # 最新在最前，最旧（tv-0 / tv-1）被丢弃
    assert entries[0]["udn"] == f"uuid:tv-{MAX_ENTRIES + 1}"
    assert entries[-1]["udn"] == "uuid:tv-2"

    # 重新从磁盘读也遵守上限
    reloaded = CastHistory(store.path)
    assert len(reloaded.entries()) == MAX_ENTRIES


# ---------------------------------------------------------------------- #
# clear
# ---------------------------------------------------------------------- #
def test_clear_empties_memory_and_file(store, tmp_path):
    store.record(_renderer())
    assert store.last() is not None

    store.clear()

    assert store.entries() == []
    assert store.last() is None
    assert json.loads((tmp_path / "cast_history.json").read_text(encoding="utf-8")) == []
    # 清空后再次记录仍可用
    store.record(_renderer(udn="uuid:tv-9"))
    assert store.last()["udn"] == "uuid:tv-9"


# ---------------------------------------------------------------------- #
# 容错：损坏 / 缺失 / 非法条目
# ---------------------------------------------------------------------- #
def test_missing_file_returns_empty(tmp_path):
    store = CastHistory(tmp_path / "nope" / "cast_history.json")
    assert store.entries() == []
    assert store.last() is None


def test_corrupted_json_returns_empty_and_recovers(store, tmp_path):
    path = tmp_path / "cast_history.json"
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")

    assert store.entries() == []
    assert store.last() is None

    store.record(_renderer(udn="uuid:tv-1"))
    assert store.last()["udn"] == "uuid:tv-1"
    assert json.loads(path.read_text(encoding="utf-8"))[0]["udn"] == "uuid:tv-1"


def test_non_list_top_level_returns_empty(tmp_path):
    path = tmp_path / "cast_history.json"
    path.write_text(json.dumps({"udn": "uuid:tv-1"}), encoding="utf-8")
    assert CastHistory(path).entries() == []


def test_non_dict_and_invalid_entries_skipped(tmp_path):
    """非 dict / 缺 udn / 字段不全的条目在读取时被跳过，其余照常返回。"""
    path = tmp_path / "cast_history.json"
    valid = {
        "udn": "uuid:tv-ok",
        "friendly_name": "好电视",
        "location": "http://192.168.1.9/root.xml",
        "av_transport_control_url": "http://192.168.1.9/avt",
        "rendering_control_url": None,
        "service_type": "urn:schemas-upnp-org:service:AVTransport:1",
        "last_used": 1.0,
    }
    path.write_text(json.dumps([
        42,
        "not-a-dict",
        {"friendly_name": "没有 udn"},
        {"udn": ""},
        {"udn": "uuid:tv-noctl"},          # 缺必需字段
        valid,
    ]), encoding="utf-8")

    entries = CastHistory(path).entries()
    assert [e["udn"] for e in entries] == ["uuid:tv-ok"]
    assert renderer_from_entry(entries[0]).friendly_name == "好电视"


def test_renderer_from_entry_rejects_bad_input():
    assert renderer_from_entry(None) is None
    assert renderer_from_entry("uuid:tv-1") is None
    assert renderer_from_entry({"friendly_name": "无 udn"}) is None
    assert renderer_from_entry({"udn": "  "}) is None
    assert renderer_from_entry({"udn": "uuid:tv-1"}) is None  # 缺控制 URL


def test_record_skips_invalid_renderer(store, tmp_path):
    """record(非法对象) 不写盘、不改状态，返回 None。"""
    assert store.record(None) is None
    assert store.record(object()) is None
    assert store.record({"friendly_name": "缺 udn"}) is None
    assert store.entries() == []
    assert not (tmp_path / "cast_history.json").exists()


# ---------------------------------------------------------------------- #
# 原子写入
# ---------------------------------------------------------------------- #
def test_save_is_atomic_and_leaves_no_tmp_files(store, tmp_path):
    store.record(_renderer(udn="uuid:tv-1"))
    store.record(_renderer(udn="uuid:tv-2"))

    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    # 内容是完整 JSON（若非原子写，最后一次内容可能被截断）
    raw = json.loads((tmp_path / "cast_history.json").read_text(encoding="utf-8"))
    assert [e["udn"] for e in raw] == ["uuid:tv-2", "uuid:tv-1"]


def test_record_creates_missing_parent_directory(tmp_path):
    store = CastHistory(tmp_path / "nested" / "dir" / "cast_history.json")
    store.record(_renderer())
    assert store.path.exists()


def test_write_failure_keeps_memory_state(tmp_path, monkeypatch):
    """落盘失败（只读目录等）不抛异常：内存状态保持正确，供本次会话继续用。"""
    store = CastHistory(tmp_path / "cast_history.json")
    import framework.cast_history as ch

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)
    entry = store.record(_renderer())
    assert entry is not None
    assert store.last()["udn"] == "uuid:tv-1"
    assert not store.path.exists()
