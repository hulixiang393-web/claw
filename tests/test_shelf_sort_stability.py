# -*- coding: utf-8 -*-
"""书架排序稳定性：非「最近阅读」模式下，阅读不得改变卡片位置。

背景（用户反馈）：书架里点开一本书阅读，卡片就换位置。批量加入的一批卡片
（加入时间相同）尤其明显。根因有两个：
1. 「最近添加」原来用 `(mtime, updated_at)` 排序——`updated_at` 是**阅读时间**，
   读完就变；收藏条目的 `mtime` 恒为 0，于是该模式实际退化成了「按阅读时间」。
2. 收藏条目的加入时间 `favorited_at` 没有被保存到 ShelfItem（被进度时间覆盖）。
"""
from __future__ import annotations

from framework.shelf_service import ShelfItem, ShelfService

_ADD = "2026-10-01T09:00:00"
_READ_LATER = "2026-10-24T20:00:00"


def _svc() -> ShelfService:
    return ShelfService(output_dir="unused", data_dir="unused")


def _item(title: str, *, added_at: str, updated_at: str = "", mtime: float = 0.0) -> ShelfItem:
    return ShelfItem(
        key=title, kind="favorite", title=title, content_type="novel",
        added_at=added_at, updated_at=updated_at, mtime=mtime,
    )


# --------------------------------------------------------------------------- #
# 「最近添加」：只看加入时间
# --------------------------------------------------------------------------- #
def test_added_sort_ignores_reading_time():
    """读完一本书（updated_at 变新）后，「最近添加」顺序必须不变。"""
    svc = _svc()
    before = [
        _item("书A", added_at="2026-10-03T10:00:00"),
        _item("书B", added_at="2026-10-02T10:00:00"),
        _item("书C", added_at="2026-10-01T10:00:00"),
    ]
    assert [i.title for i in svc._sort_items(before, "added")] == ["书A", "书B", "书C"]

    # 用户读了最旧的「书C」→ 它的 updated_at 变成最新
    after = [
        _item("书A", added_at="2026-10-03T10:00:00"),
        _item("书B", added_at="2026-10-02T10:00:00"),
        _item("书C", added_at="2026-10-01T10:00:00", updated_at=_READ_LATER),
    ]
    assert [i.title for i in svc._sort_items(after, "added")] == ["书A", "书B", "书C"]


def test_added_sort_is_stable_for_same_timestamp_batch():
    """同一时刻批量加入（加入时间相同）→ 阅读任何一本都不改变顺序。"""
    svc = _svc()
    batch = [_item(t, added_at=_ADD) for t in ("丙", "甲", "乙")]
    order = [i.title for i in svc._sort_items(batch, "added")]
    # 同批内按标题稳定排序
    assert order == sorted(order)

    batch[1].updated_at = _READ_LATER  # 读了「甲」
    assert [i.title for i in svc._sort_items(batch, "added")] == order


def test_added_sort_newest_first_and_local_uses_file_mtime():
    svc = _svc()
    fav = _item("收藏", added_at="2026-10-05T00:00:00")
    local = ShelfItem(key="p", kind="local", title="本地", content_type="novel",
                      mtime=1760000000.0)  # 2025-10 左右，早于收藏
    order = [i.title for i in svc._sort_items([local, fav], "added")]
    assert order == ["收藏", "本地"]


# --------------------------------------------------------------------------- #
# 「最近阅读」：允许按阅读时间重排（这是它的语义）
# --------------------------------------------------------------------------- #
def test_recent_sort_follows_reading_time():
    svc = _svc()
    items = [
        _item("书A", added_at=_ADD, updated_at="2026-10-02T10:00:00"),
        _item("书B", added_at=_ADD, updated_at=_READ_LATER),
    ]
    assert [i.title for i in svc._sort_items(items, "recent")] == ["书B", "书A"]


# --------------------------------------------------------------------------- #
# 「书名」：与阅读无关
# --------------------------------------------------------------------------- #
def test_name_sort_unaffected_by_reading():
    svc = _svc()
    items = [
        _item("B书", added_at=_ADD),
        _item("A书", added_at=_ADD, updated_at=_READ_LATER),
    ]
    assert [i.title for i in svc._sort_items(items, "name")] == ["A书", "B书"]


def test_added_epoch_falls_back_to_mtime_for_local_items():
    local = ShelfItem(key="p", kind="local", title="本地", content_type="novel",
                      mtime=1234567890.0)
    assert ShelfService._added_epoch(local) == 1234567890.0
    bad = ShelfItem(key="p", kind="local", title="x", content_type="novel",
                    added_at="不是时间", mtime=42.0)
    assert ShelfService._added_epoch(bad) == 42.0


# --------------------------------------------------------------------------- #
# 数据来源：收藏时间必须落到 added_at，阅读时间只落到 updated_at
# --------------------------------------------------------------------------- #
class _Store:
    def __init__(self, rows):
        self._rows = list(rows)

    def list_all(self):
        return [dict(r) for r in self._rows]

    def list_folders(self):
        return []


class _Progress:
    def __init__(self, summaries):
        self._summaries = dict(summaries)

    def summary(self, key):
        return self._summaries.get(key, {})


def test_favorites_keep_add_time_separate_from_reading_time():
    """收藏于 T1、读到 T2：added_at 保持 T1，updated_at 才是 T2。"""
    store = _Store([{
        "url": "https://example/book", "title": "书A", "content_type": "novel",
        "favorited_at": _ADD,
    }])
    progress = _Progress({"https://example/book": {
        "chapter_title": "第3章", "updated_at": _READ_LATER,
    }})
    svc = ShelfService(output_dir="unused", data_dir="unused",
                       library_store=store, reading_progress=progress)

    item = svc.favorites()[0]
    assert item.added_at == _ADD          # 加入时间不被阅读污染
    assert item.updated_at == _READ_LATER  # 阅读时间单独保留

    # 「最近添加」看加入时间；「最近阅读」看阅读时间
    other = _item("书B", added_at="2026-10-05T00:00:00")
    assert [i.title for i in svc._sort_items([other, item], "added")] == ["书B", "书A"]
    assert [i.title for i in svc._sort_items([other, item], "recent")] == ["书A", "书B"]
