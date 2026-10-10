from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import List

MAX_ITEMS = 20
HISTORY_TTL_SECONDS = 604800


class SearchHistory:
    def __init__(self, path: str | Path = "search_history.json", privacy=None):
        self.path = Path(path)
        self.privacy = privacy
        self._items = self._load()
        self._clean()

    def _load(self) -> list:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _clean(self) -> None:
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

    def _save(self) -> None:
        temp_path = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            temp_path = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._items, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def push(self, keyword: str, context_url: str = "", folder: str = "") -> None:
        self._clean()
        keyword = keyword.strip()
        if not keyword or self.hidden(keyword, context_url, folder):
            return
        self._items = [item for item in self._items if item["keyword"] != keyword]
        self._items.insert(0, {"keyword": keyword, "timestamp": time.time()})
        self._items = self._items[:MAX_ITEMS]
        self._save()

    def hidden(self, keyword: str, context_url: str = "", folder: str = "") -> bool:
        return bool(self.privacy and self.privacy.search_history_hidden(keyword, context_url, folder))

    def recent(self) -> List[str]:
        self._clean()
        return [item["keyword"] for item in self._items if not self.hidden(item["keyword"])]

    def clear(self) -> None:
        self._items = []
        self._save()
