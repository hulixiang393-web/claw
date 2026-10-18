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
            return raw if isinstance(raw, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    @_synchronized
    def _clean(self) -> None:
        if self._defer_cleanup:
            return
        now = time.time()
        valid = []
        for item in self._items:
            if not isinstance(item, dict):
                continue
            word, ts = item.get("keyword"), item.get("timestamp")
            if not isinstance(word, str) or not word.strip():
                continue
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                continue
            if not math.isfinite(ts) or not 0 <= now - ts < HISTORY_TTL_SECONDS:
                continue
            valid.append({"keyword": word.strip(), "timestamp": ts})
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
    def push(self, keyword: str, context_url: str = "", folder: str = "") -> None:
        self._clean()
        keyword = keyword.strip()
        if not keyword or self.hidden(keyword, context_url, folder):
            return
        self._items = [item for item in self._items if item["keyword"] != keyword]
        self._items.insert(0, {"keyword": keyword, "timestamp": time.time()})
        self._items = self._items[:MAX_ITEMS]
        self._save()

    @_synchronized
    def hidden(self, keyword: str, context_url: str = "", folder: str = "") -> bool:
        return bool(self.privacy and self.privacy.search_history_hidden(keyword, context_url, folder))

    @_synchronized
    def recent(self) -> List[str]:
        if self._defer_cleanup:
            now = time.time()
            items = []
            for item in self._items:
                if not isinstance(item, dict):
                    continue
                word, ts = item.get("keyword"), item.get("timestamp")
                if (not isinstance(word, str) or not word.strip()
                        or isinstance(ts, bool) or not isinstance(ts, (int, float))
                        or not math.isfinite(ts) or not 0 <= now - ts < HISTORY_TTL_SECONDS):
                    continue
                if not self.hidden(word, "", ""):
                    items.append(word.strip())
            return items[:MAX_ITEMS]
        self._clean()
        return [item["keyword"] for item in self._items if not self.hidden(item["keyword"])]

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
