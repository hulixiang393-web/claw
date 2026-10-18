"""发现页换源会话快照纯逻辑测试（无 GUI 依赖）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.discovery import Work
from framework.discover_session import (
    SOURCE_CACHE_MAX,
    SNAPSHOT_KEY_PREFIX,
    snapshot_key,
    make_snapshot,
    cat_list_from_buttons,
    prune_excess,
    register_source,
)
from framework.cache_service import RedisLikeStore


def test_snapshot_key():
    assert snapshot_key("srcA") == "disc:srcA"


def test_make_snapshot_and_works_roundtrip_via_pickle():
    """list[Work] 经 RedisLikeStore pickle 往返后字段完整（禁用 as_dict 往返）。"""
    ws = [Work(title="书一", url="/b1", cover="/c1.jpg", source_id="srcA", source_name="源A")]
    snap = make_snapshot(
        cat_buttons=[],
        cat_url="/cat/x",
        works=ws,
        pages=[1, 2],
        has_more=True,
        current_page=2,
        scroll=0.6,
        ts=123.0,
    )
    assert snap["cat_url"] == "/cat/x"
    assert snap["pages"] == [1, 2]
    assert snap["scroll"] == 0.6
    assert snap["has_more"] is True
    assert snap["current_page"] == 2

def test_cat_list_from_buttons_text():
    # 空按钮 → 空 cats
    snap = make_snapshot(
        cat_buttons=[], cat_url="", works=[], pages=[],
        has_more=False, current_page=0, scroll=0.0, ts=1.0,
    )
    assert snap["cats"] == []

    class _TBtn:
        def __init__(self, text):
            self._t = text

        def text(self):
            return self._t

    class _Cat:
        def __init__(self, url):
            self.url = url

    snap = make_snapshot(
        cat_buttons=[(_TBtn("全部"), None), (_TBtn("玄幻"), _Cat("/cat/x"))],
        cat_url="/cat/x", works=[], pages=[],
        has_more=False, current_page=0, scroll=0.0, ts=1.0,
    )
    assert snap["cats"] == [("全部", None), ("玄幻", "/cat/x")]


def test_works_pickle_roundtrip_fields():
    ws = [Work(title="书一", url="/b1", cover="/c1.jpg", source_id="srcA", source_name="源A", update="连载")]
    store = RedisLikeStore(quota=1024 * 1024)
    snap = make_snapshot(cat_buttons=[], cat_url="/cat/x",
                         works=ws, pages=[1], has_more=True,
                         current_page=1, scroll=0.5, ts=1.0)
    store.set(snapshot_key("srcA"), snap)
    got = store.get(snapshot_key("srcA"))
    assert got["works"][0].title == "书一"
    assert got["works"][0].url == "/b1"
    assert got["works"][0].update == "连载"


def test_prune_excess_keeps_five():
    order = ["s1", "s2", "s3", "s4", "s5", "s6"]
    assert prune_excess(order) == ["disc:s1"]          # 只删最旧一个
    order = ["s1", "s2", "s3", "s4", "s5"]
    assert prune_excess(order) == []


def test_register_source_lru():
    store = RedisLikeStore(quota=1024 * 1024)
    # 真实用法：每源写入快照前先 register_source（登记 LRU + 逐出最旧）
    for sid in ["s1", "s2", "s3", "s4", "s5", "s6"]:
        register_source(store, sid)
        store.set(snapshot_key(sid), make_snapshot(
            cat_buttons=[], cat_url="/all", works=[], pages=[],
            has_more=False, current_page=0, scroll=0.0, ts=1.0,
        ))
    assert store.get(snapshot_key("s1")) is None   # 最旧被逐出
    assert store.get(snapshot_key("s6")) is not None  # 最新保留
    assert store.get(SNAPSHOT_KEY_PREFIX + "_order") == ["s2", "s3", "s4", "s5", "s6"]
    assert SOURCE_CACHE_MAX == 5