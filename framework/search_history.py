from __future__ import annotations

import json
import math
import threading
import time
from functools import wraps
from pathlib import Path
from typing import List

from .atomic_io import atomic_write_json

MAX_ITEMS = 20
HISTORY_TTL_SECONDS = 604800


def _clamp_item(item: dict) -> dict | None:
    """校验并归一化一条历史条目；非法则丢弃。

    兼容旧格式（仅 {"keyword", "timestamp"}）：补 source_ids/type 默认空。
    """
    if not isinstance(item, dict):
        return None
    word, ts = item.get("keyword"), item.get("timestamp")
    if not isinstance(word, str) or not word.strip():
        return None
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    if not math.isfinite(ts):
        return None
    source_ids = item.get("source_ids")
    if source_ids is not None and (
        not isinstance(source_ids, list)
        or not all(isinstance(s, str) for s in source_ids)
    ):
        source_ids = None
    t = item.get("type")
    if t is not None and not isinstance(t, str):
        t = None
    return {
        "keyword": word.strip(),
        "timestamp": ts,
        "source_ids": list(source_ids) if source_ids else [],
        "type": t or "",
    }


def _synchronized(method):
    """同一实例内的读写串行化：防止后台清理线程与 UI 线程并发损坏 _items。"""

    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return call


class SearchHistory:
    def __init__(self, path: str | Path = "search_history.json", privacy=None, defer_cleanup=False):
        self.path = Path(path)
        self.privacy = privacy
        self._lock = threading.RLock()
        self._items = self._load()
        self._defer_cleanup = defer_cleanup
        if not defer_cleanup:
            self._clean()

    @_synchronized
    def run_deferred_cleanup(self) -> None:
        self._defer_cleanup = False
        self._clean()

    @_synchronized
    def _load(self) -> list:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(raw, list):
            return []
        valid = []
        for item in raw:
            clamped = _clamp_item(item)
            if clamped is not None:
                valid.append(clamped)
        return valid

    @_synchronized
    def _clean(self) -> None:
        if self._defer_cleanup:
            return
        now = time.time()
        valid = []
        for item in self._items:
            clamped = _clamp_item(item)
            if clamped is None:
                continue
            if not 0 <= now - clamped["timestamp"] < HISTORY_TTL_SECONDS:
                continue
            valid.append(clamped)
        valid.sort(key=lambda item: item["timestamp"], reverse=True)
        cleaned, seen = [], set()
        for item in valid:
            if item["keyword"] not in seen:
                cleaned.append(item)
                seen.add(item["keyword"])
        cleaned = cleaned[:MAX_ITEMS]
        if cleaned != self._items:
            previous = self._items
            self._items = cleaned
            try:
                self._save()
            except OSError:
                self._items = previous
                raise

    @_synchronized
    def _save(self) -> None:
        atomic_write_json(self.path, self._items)

    @_synchronized
    def push(self, keyword: str, context_url: str = "", folder: str = "",
             source_ids: list | None = None, type: str = "") -> None:
        self._clean()
        keyword = keyword.strip()
        if not keyword or self.hidden(keyword, context_url, folder):
            return
        # 去重时保留旧条目的来源/类型（来源变更不应抹掉上次搜索记录的源）。
        old = next((it for it in self._items if it["keyword"] == keyword), None)
        if old is not None:
            self._items = [it for it in self._items if it["keyword"] != keyword]
        # 仅当调用方显式给出来源时才覆盖（普通 push 不传 → 沿用旧记录）。
        if source_ids is None and old is not None:
            source_ids = old.get("source_ids")
        if not type and old is not None:
            type = old.get("type") or ""
        self._items.insert(0, {
            "keyword": keyword,
            "timestamp": time.time(),
            "source_ids": list(source_ids) if source_ids else [],
            "type": type or "",
        })
        self._items = self._items[:MAX_ITEMS]
        self._save()

    @_synchronized
    def hidden(self, keyword: str, context_url: str = "", folder: str = "") -> bool:
        return bool(self.privacy and self.privacy.search_history_hidden(keyword, context_url, folder))

    @_synchronized
    def recent(self, with_meta: bool = False) -> List:
        """最近搜索词（默认只返回关键词列表，兼容旧调用）。

        with_meta=True 时返回完整条目列表
        [{"keyword", "timestamp", "source_ids", "type"}, ...]，供重搜时
        精确还原上一次使用的源与类型（避免重搜混入未使用的源）。
        """
        if self._defer_cleanup:
            now = time.time()
            records = []
            for item in self._items:
                if not isinstance(item, dict):
                    continue
                word, ts = item.get("keyword"), item.get("timestamp")
                if (not isinstance(word, str) or not word.strip()
                        or isinstance(ts, bool) or not isinstance(ts, (int, float))
                        or not math.isfinite(ts) or not 0 <= now - ts < HISTORY_TTL_SECONDS):
                    continue
                if not self.hidden(word, "", ""):
                    if with_meta:
                        records.append(_clamp_item(item))
                    else:
                        records.append(word.strip())
            return records[:MAX_ITEMS]
        self._clean()
        out = []
        for item in self._items:
            if self.hidden(item["keyword"]):
                continue
            out.append(_clamp_item(item) if with_meta else item["keyword"])
        return out

    @_synchronized
    def clear(self) -> None:
        self._items = []
        self._save()

    @_synchronized
    def remove(self, keyword: str) -> bool:
        """Remove one search term and report whether it existed."""
        keyword = str(keyword or "").strip()
        old_len = len(self._items)
        self._items = [
            item for item in self._items
            if not isinstance(item, dict) or item.get("keyword") != keyword
        ]
        if len(self._items) == old_len:
            return False
        self._save()
        return True
