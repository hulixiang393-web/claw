"""发现页换源会话快照纯逻辑（无 GUI 依赖，方便单测）。

不触碰 discovery/list_works/list_categories 与 Content._cache——
换源会话缓存是 GUI 层独立会话机制（退出应用即由进程回收自动清空，
不落盘、不阻塞关闭流程），与持久缓存决策（2026-09-08）解耦。
"""

from __future__ import annotations

from typing import Optional

from .cache_service import RedisLikeStore

SOURCE_CACHE_MAX = 5
SNAPSHOT_KEY_PREFIX = "disc:"


def snapshot_key(source_id: str) -> str:
    return f"{SNAPSHOT_KEY_PREFIX}{source_id}"


def cat_list_from_buttons(cat_buttons: list) -> list:
    """把 (btn, cat) 列表提取为可序列化 [(title, url|None)]。

    cat 为 None 表示「全部」按钮；title 取按钮 text()。
    """
    out = []
    for btn, cat in cat_buttons:
        title = getattr(btn, "text", lambda: "")()
        url = getattr(cat, "url", None) if cat is not None else None
        out.append((title, url))
    return out


def make_snapshot(
    cat_buttons,
    cat_url: str,
    works: list,
    pages: list,
    has_more: bool,
    current_page: int,
    scroll: float,
    ts: float,
) -> dict:
    return {
        "cats": cat_list_from_buttons(cat_buttons),
        "cat_url": cat_url,
        "works": list(works),
        "pages": sorted(set(pages)),
        "has_more": bool(has_more),
        "current_page": int(current_page),
        "scroll": round(float(scroll), 4),   # 0~1 滚动比例
        "ts": float(ts),
    }


def from_snapshot(snap: dict) -> Optional[dict]:
    """结构净化：缺关键字段返回 None（调用方按未命中处理）。"""
    if not isinstance(snap, dict):
        return None
    for k in ("cats", "cat_url", "works", "pages", "has_more", "current_page", "scroll", "ts"):
        if k not in snap:
            return None
    return snap


def register_source(store: Optional[RedisLikeStore], source_id: str) -> None:
    """登记源使用（LRU 上限控制）。超上限逐出最旧源的缓存键。"""
    if store is None or not source_id:
        return
    orig = store.get(SNAPSHOT_KEY_PREFIX + "_order")
    order = list(orig) if isinstance(orig, list) else []
    if source_id in order:
        order.remove(source_id)
    order.append(source_id)
    for key in prune_excess(order):
        store.delete(key)
    order = order[-SOURCE_CACHE_MAX:]  # 同步截断，与实际保留的快照一致
    store.set(SNAPSHOT_KEY_PREFIX + "_order", order)


def prune_excess(order: list) -> list:
    """超上限时返回应删除的快照 key 列表（纯函数）。"""
    order = list(order)
    to_delete = []
    while len(order) > SOURCE_CACHE_MAX:
        oldest = order.pop(0)
        to_delete.append(snapshot_key(oldest))
    return to_delete


def should_restore(store: Optional[RedisLikeStore], source_id: str) -> bool:
    """换回某源时判断是否应命中会话缓存（结构校验后）。"""
    if store is None or not source_id:
        return False
    return from_snapshot(store.get(snapshot_key(source_id))) is not None