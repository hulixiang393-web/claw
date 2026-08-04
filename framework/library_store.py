"""收藏库（library_store.py）。

手动收藏的在线作品元数据存储（JSON 文件）。收藏只存元数据快照，
不依赖文件是否还在（文件删了收藏仍在，可重新下载）。

对应 ui-library.md 功能点 #2 收藏。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional


class LibraryStore:
    """收藏存储：JSON 文件读写，线程安全。"""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        try:
            if self._path.is_file():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = raw
        except (OSError, json.JSONDecodeError):
            self._data = {}  # 损坏文件 → 空收藏，不崩溃

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    # ------------------------------------------------------------------ #
    def add(self, source_id: str, url: str, title: str,
            content_type: str = "", cover: str = "",
            author: str = "", tags: list | None = None) -> dict:
        """收藏一部作品。url 作唯一 key；已存在则更新元数据。返回记录。"""
        if not url:
            return {}
        rec = {
            "source_id": source_id,
            "url": url,
            "title": title or url,
            "content_type": content_type,
            "cover": cover,
            "author": author,
            "tags": list(tags or []),
            "favorited_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with self._lock:
            self._data[url] = rec
            self._save()
        return dict(rec)

    def remove(self, url: str) -> bool:
        """移除收藏（只删元数据，不删本地文件）。"""
        with self._lock:
            if url in self._data:
                del self._data[url]
                self._save()
                return True
        return False

    def has(self, url: str) -> bool:
        with self._lock:
            return url in self._data

    def get(self, url: str) -> Optional[dict]:
        with self._lock:
            rec = self._data.get(url)
            return dict(rec) if rec else None

    def list_all(self) -> list[dict]:
        """全部收藏记录（按收藏时间倒序）。"""
        with self._lock:
            items = [dict(v) for v in self._data.values()]
        items.sort(key=lambda r: r.get("favorited_at", ""), reverse=True)
        return items

    def count(self) -> int:
        with self._lock:
            return len(self._data)
