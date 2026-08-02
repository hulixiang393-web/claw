"""最近搜索历史（search_history.py）。

首页与搜索页共享。最多保留 20 条，超出滚动清掉最旧。
对应 ui-home.md「最近搜索」+ ui-search.md「搜索历史」。

用法：
    hist = SearchHistory("search_history.json")
    hist.push("凡人修仙传")        # 追加到最前，去重
    hist.recent()                  # -> ["凡人修仙传", ...] 最多20条
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

MAX_ITEMS = 20


class SearchHistory:
    def __init__(self, path: str | Path = "search_history.json"):
        self.path = Path(path)
        self._items: List[str] = self._load()

    def _load(self) -> List[str]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [str(x) for x in raw if isinstance(x, str)][:MAX_ITEMS]
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._items, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def push(self, keyword: str) -> None:
        """加入历史（去重、移到最前、最多20条）。"""
        keyword = keyword.strip()
        if not keyword:
            return
        if keyword in self._items:
            self._items.remove(keyword)
        self._items.insert(0, keyword)
        if len(self._items) > MAX_ITEMS:
            self._items = self._items[:MAX_ITEMS]
        self._save()

    def recent(self) -> List[str]:
        return list(self._items)

    def clear(self) -> None:
        self._items = []
        self._save()
