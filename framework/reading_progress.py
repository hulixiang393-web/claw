"""阅读进度记忆（reading_progress.py）。

跨类型（小说/漫画/视频）记录"当天阅读进度"，一天后未加入书架则删除。
仅供"当天续读"使用：打开某部作品时定位到最后阅读章节。

存储：data/reading_progress.json，按 book_url 归一，一本一条。
对应 PROJECT_GUIDE.md §6.7、§6.12 的阅读器续读与书架能力。

用法：
    rp = ReadingProgress("data/reading_progress.json")
    rp.save(source_id, book_url, content_type, chapter_url, chapter_title)
    rp.resume(book_url)          # -> {…} 或 None
    rp.prune(shelf_cb)           # 超24h 且 不在书架 → 删除
"""

from __future__ import annotations

import copy
import json
import math
import os
import time
import threading
from functools import wraps
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .atomic_io import atomic_write_json

# 记忆有效期（秒）：超时未加入书架则清理
MEMORY_TTL_SECONDS = 24 * 3600
HISTORY_TTL_SECONDS = 604800


def history_active(record, now=None):
    now = time.time() if now is None else now
    value = record.get("history_timestamp", record.get("updated_at"))
    if isinstance(value, str):
        try:
            value = time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, OverflowError, OSError):
            return False
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and 0 <= now - value < HISTORY_TTL_SECONDS)


def video_resume_record(record):
    result = {key: copy.deepcopy(record[key]) for key in (
        "source_id", "book_url", "content_type", "chapter_url", "position",
        "page", "library_url", "detail_url", "folder",
    ) if key in record}
    location = record.get("location")
    result["location"] = ({key: copy.deepcopy(location[key]) for key in
                           ("seconds", "duration", "position") if key in location}
                          if isinstance(location, dict) else None)
    return result


def _sanitized_video_history(records):
    return {key: video_resume_record(record)
            if record.get("content_type") == "video" and not history_active(record)
            else copy.deepcopy(record) for key, record in records.items()}


def _atomic_write_progress(path, records):
    atomic_write_json(path, records)


def sanitize_progress_backup(progress_path, create_records=None):
    path = Path(progress_path)
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        raw = json.loads(backup.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if create_records is not None:
            _atomic_write_progress(backup, _sanitized_video_history(create_records))
        return
    except (UnicodeDecodeError, ValueError):
        return
    if not isinstance(raw, dict) or any(not isinstance(record, dict) for record in raw.values()):
        return
    cleaned = _sanitized_video_history(raw)
    if cleaned != raw:
        _atomic_write_progress(backup, cleaned)


def _synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class ReadingProgress:
    def __init__(
        self,
        path: str | Path = "reading_progress.json",
        shelf_cb: Optional[Callable[[str], bool]] = None,
        repository=None,
        defer_cleanup=False,
    ):
        """shelf_cb(book_url) -> bool：该书是否已入书架。

        传入后 save/prune 对已收藏的书永久保留续读（超 24h 也不删），
        仅清理未收藏的过期记忆——修复"收藏的书也丢续读位置"。
        """
        self.path = Path(path)
        self.shelf_cb = shelf_cb
        self._repository = repository
        self._lock = threading.RLock()
        self._data: dict = self._load()
        self._defer_cleanup = defer_cleanup
        if not defer_cleanup:
            self._prune_video_history()

    def run_deferred_cleanup(self) -> None:
        with self._lock:
            self._defer_cleanup = False
            self._prune_video_history()


    def attach_repository(self, repository) -> None:
        with self._lock:
            self._repository = repository

    def _prune_video_history(self):
        if self._defer_cleanup:
            return
        sanitize_progress_backup(self.path)
        previous = self._data
        cleaned = _sanitized_video_history(previous)
        if cleaned != previous:
            self._data = cleaned
            try:
                self._save(strict=True)
            except (OSError, TypeError, ValueError):
                self._data = previous
                raise
        if self._repository is not None:
            self._repository.prune_video_history()

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

    def _save(self, strict=False) -> None:
        try:
            _atomic_write_progress(self.path, self._data)
        except (OSError, TypeError, ValueError):
            if strict:
                raise

    # ------------------------------------------------------------------ #
    @_synchronized
    def save(
        self,
        source_id: str,
        book_url: str,
        content_type: str,
        chapter_url: str,
        chapter_title: str,
        position: float | None = None,
        page: int | None = None,
        location: dict | None = None,
        title: str = "",
        library_url: str = "",
        folder: str = "",
        detail_url: str = "",
    ) -> None:
        """记录/更新一部作品的阅读进度（换章/滚动/翻页/播放时调用）。

        position：章内阅读位置——小说滚动/漫画滚动/视频播放用 0~1 比例；
        page：小说翻页模式的页索引（0 基），非翻页模式为 None。
        向后兼容：旧记录无这两个字段，恢复时按 None 处理（仅章级续读）。
        """
        if not book_url:
            return
        if location is not None and not isinstance(location, dict):
            raise TypeError("location must be a dict or None")
        stored_location = copy.deepcopy(location) if location is not None else None
        if stored_location is not None:
            try:
                json.dumps(stored_location, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise TypeError("location must contain only JSON-serializable values") from exc
        self._prune_video_history()
        old = self._data.get(book_url, {})
        same_chapter = old.get("chapter_url") == chapter_url
        if location is None and same_chapter:
            stored_location = copy.deepcopy(old.get("location"))
        # 同章且本次无位置信息（章级信号先于滚动/播放落盘）：保留旧位置，
        # 避免"打开续读的书 → 加载该章 → 章信号把存储位置刷成 None"丢失恢复点。
        # 换到新章（chapter_url 不同）才重置为无位置（新章从顶部开始）。
        if position is None and page is None and same_chapter:
            position = old.get("position")
            page = old.get("page")
        record = {
            "source_id": source_id,
            "book_url": book_url,
            "content_type": content_type,
            "chapter_url": chapter_url,
            "chapter_title": chapter_title,
            "title": title or old.get("title", ""),
            "library_url": library_url or old.get("library_url", ""),
            "detail_url": detail_url or old.get("detail_url", ""),
            "folder": folder or old.get("folder", ""),
            "position": position,
            "page": page,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "location": stored_location,
        }
        if content_type == "video":
            record["history_timestamp"] = time.time()
        self._data[book_url] = record
        if self._repository is not None:
            self._repository.upsert_book(book_url, source_id, book_url, content_type, {})
            self._repository.update_location(book_url, record)
        self._save()
        self.prune(shelf_cb=self.shelf_cb)  # 每次写入顺带清理超期项（收藏的书保留）

    @staticmethod
    def _normalized_url(url: str) -> str:
        parts = urlsplit((url or "").strip())
        host = parts.hostname or ""
        if parts.port not in (None, 80, 443):
            host = f"{host}:{parts.port}"
        query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
        return urlunsplit((parts.scheme.lower(), host.lower(), parts.path.rstrip("/") or "/", query, ""))

    @_synchronized
    def summary(self, book_url: str) -> Optional[dict]:
        rec = self._data.get(book_url)
        if rec is None:
            normalized = self._normalized_url(book_url)
            rec = next((candidate for key, candidate in self._data.items()
                        if self._normalized_url(key) == normalized), None)
        if rec is None and self._repository is not None:
            rec = self._repository.get_location(book_url)
        if not rec or (rec.get("content_type") == "video" and not history_active(rec)):
            return None
        return {key: rec[key] for key in ("chapter_title", "updated_at") if key in rec}

    @_synchronized
    def resume(self, book_url: str) -> Optional[dict]:
        """取某本书的进度（供续读定位）。无则 None。"""
        self._prune_video_history()
        rec = self._data.get(book_url)
        if rec is None:
            normalized = self._normalized_url(book_url)
            for key, candidate in self._data.items():
                if self._normalized_url(key) == normalized:
                    rec = candidate
                    break
        if rec is None and self._repository is not None:
            snapshot = self._repository.get_book_snapshot(book_url)
            rec = (snapshot or {}).get("location")
        return copy.deepcopy(rec) if rec else None

    @_synchronized
    def prune(self, shelf_cb: Optional[Callable[[str], bool]] = None) -> int:
        """清理过期记忆：超 TTL_SECONDS 且（书架回调未命中）→ 删除。

        shelf_cb(book_url) -> bool 表示该书是否已加入书架。
        书架未实现时传 None → 恒当作"未入书架"，超期即清理。
        返回删除条数。
        """
        self._prune_video_history()
        now = time.time()
        removed = 0
        expired_keys = []
        for url, rec in self._data.items():
            if rec.get("content_type") == "video":
                continue
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

    @_synchronized
    def all(self) -> dict:
        """最新记忆快照（供首页"继续阅读"等展示）。"""
        self._prune_video_history()
        return json.loads(json.dumps({key: record for key, record in self._data.items()
                                     if record.get("content_type") != "video" or history_active(record)}))

    def recent_videos(self, privacy=None, limit: int = 20) -> list[dict]:
        records = [rec for rec in self.all().values()
                   if rec.get("content_type") == "video" and rec.get("chapter_url")
                   and not (privacy and privacy.playback_history_hidden(rec))]
        records.sort(key=lambda rec: rec.get("updated_at", ""), reverse=True)
        return records[:limit]

    @_synchronized
    def clear(self) -> None:
        self._data = {}
        self._save()

    @_synchronized
    def remove(self, book_url: str) -> bool:
        """Remove one history entry by its normalized book URL."""
        if not book_url or book_url not in self._data:
            return False
        self._data.pop(book_url, None)
        self._save()
        return True

    @_synchronized
    def clear_videos(self) -> int:
        """Clear only the video history shown on the home page."""
        urls = [
            url for url, record in self._data.items()
            if record.get("content_type") == "video"
        ]
        for url in urls:
            self._data.pop(url, None)
        if urls:
            self._save()
        return len(urls)
class PositionSnapshot:
    @staticmethod
    def make_position_snapshot(view) -> tuple:
        if hasattr(view, 'position_snapshot'):
            try:
                return view.position_snapshot()
            except Exception:
                pass
        return None, None

    @staticmethod
    def make_current_context(view):
        if hasattr(view, 'current_context'):
            try:
                return view.current_context()
            except Exception:
                pass
        return None

    @staticmethod
    def make_location(view):
        if hasattr(view, '_build_current_location'):
            try:
                return view._build_current_location()
            except Exception:
                pass
        return None

class GenerationGuard:
    def __init__(self):
        self._generation = 0

    def next(self):
        self._generation += 1
        return self._generation

    def current(self):
        return self._generation

    def is_current(self, gen):
        return gen == self._generation

    def invalidate(self):
        self._generation += 1
