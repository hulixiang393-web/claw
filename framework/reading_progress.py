"""阅读进度记忆（reading_progress.py）。

跨类型（小说/漫画/视频）记录"当天阅读进度"，一天后未加入书架则删除。
仅供"当天续读"使用：打开某部作品时定位到最后阅读章节。

存储：data/reading_progress.json，按 book_url 归一，一本一条。
对应 ui-reader.md「退出/续读」与 ui-library.md 书架（书架未实现前先用本模块）。

用法：
    rp = ReadingProgress("data/reading_progress.json")
    rp.save(source_id, book_url, content_type, chapter_url, chapter_title)
    rp.resume(book_url)          # -> {…} 或 None
    rp.prune(shelf_cb)           # 超24h 且 不在书架 → 删除
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

# 记忆有效期（秒）：超时未加入书架则清理
MEMORY_TTL_SECONDS = 24 * 3600


class ReadingProgress:
    def __init__(self, path: str | Path = "reading_progress.json"):
        self.path = Path(path)
        self._data: dict = self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    def save(
        self,
        source_id: str,
        book_url: str,
        content_type: str,
        chapter_url: str,
        chapter_title: str,
    ) -> None:
        """记录/更新一部作品的阅读进度（换章时调用）。"""
        if not book_url:
            return
        self._data[book_url] = {
            "source_id": source_id,
            "book_url": book_url,
            "content_type": content_type,
            "chapter_url": chapter_url,
            "chapter_title": chapter_title,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._save()
        self.prune(shelf_cb=None)  # 每次写入顺带清理超期项

    def resume(self, book_url: str) -> Optional[dict]:
        """取某本书的进度（供续读定位）。无则 None。"""
        rec = self._data.get(book_url)
        return dict(rec) if rec else None

    def prune(self, shelf_cb: Optional[Callable[[str], bool]] = None) -> int:
        """清理过期记忆：超 TTL_SECONDS 且（书架回调未命中）→ 删除。

        shelf_cb(book_url) -> bool 表示该书是否已加入书架。
        书架未实现时传 None → 恒当作"未入书架"，超期即清理。
        返回删除条数。
        """
        now = time.time()
        removed = 0
        expired_keys = []
        for url, rec in self._data.items():
            try:
                updated = time.strptime(rec.get("updated_at", ""), "%Y-%m-%dT%H:%M:%S")
                ts = time.mktime(updated)
            except (ValueError, OSError):
                ts = now  # 时间戳无法解析 → 视为最新，暂不删
            if now - ts < MEMORY_TTL_SECONDS:
                continue  # 未过期
            # 已过期：检查是否已加入书架
            if shelf_cb and shelf_cb(url):
                continue  # 已在书架，保留（长期续读）
            expired_keys.append(url)
        for url in expired_keys:
            self._data.pop(url, None)
            removed += 1
        if removed:
            self._save()
        return removed

    def all(self) -> dict:
        """最新记忆快照（供首页"继续阅读"等展示）。"""
        return json.loads(json.dumps(self._data))

    def clear(self) -> None:
        self._data = {}
        self._save()