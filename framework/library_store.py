"""收藏库（library_store.py）。

手动收藏的在线作品元数据存储（JSON 文件）。收藏只存元数据快照，
不依赖文件是否还在（文件删了收藏仍在，可重新下载）。

支持收藏夹：收藏可归入命名收藏夹，空夹也持久化保留。
对应 ui-library.md 功能点 #2 收藏 + 收藏夹。
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
        self._folders: list[str] = []  # 收藏夹名（独立于收藏，空夹保留）
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        try:
            if self._path.is_file():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    # 新格式 {"favorites": {...}, "folders": [...]}；旧格式直接是 dict
                    self._data = raw.get("favorites", raw) if isinstance(raw, dict) else raw
                    folders = raw.get("folders")
                    if isinstance(folders, list):
                        self._folders = [f for f in folders if f]
        except (OSError, json.JSONDecodeError):
            self._data = {}  # 损坏文件 → 空收藏，不崩溃

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"favorites": self._data, "folders": self._folders},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    # ------------------------------------------------------------------ #
    # 收藏夹
    # ------------------------------------------------------------------ #
    def create_folder(self, name: str) -> bool:
        """新建收藏夹（空夹也保留）。同名返回 False。"""
        name = name.strip()
        if not name or name in self._folders:
            return False
        with self._lock:
            self._folders.append(name)
            self._save()
        return True

    def rename_folder(self, old: str, new: str) -> bool:
        """重命名收藏夹（同步更新其中的收藏记录）。"""
        old, new = old.strip(), new.strip()
        if not old or not new or old == new:
            return False
        with self._lock:
            if old not in self._folders:
                return False
            self._folders = [new if f == old else f for f in self._folders]
            for rec in self._data.values():
                if rec.get("folder") == old:
                    rec["folder"] = new
            self._save()
        return True

    def delete_folder(self, name: str) -> None:
        """删除收藏夹：夹内收藏移回未归类（不删收藏）。"""
        with self._lock:
            self._folders = [f for f in self._folders if f != name]
            for rec in self._data.values():
                if rec.get("folder") == name:
                    rec["folder"] = ""
            self._save()

    def list_folders(self) -> list[str]:
        """现有收藏夹名（含空夹，排序）。"""
        with self._lock:
            folders = set(self._folders)
            folders.update(r.get("folder", "") for r in self._data.values())
        return sorted(f for f in folders if f)

    def folder_items(self, folder: str) -> list[dict]:
        """某收藏夹内的收藏记录。folder="" = 未归类。"""
        with self._lock:
            items = [dict(v) for v in self._data.values()
                     if (v.get("folder") or "") == folder]
        items.sort(key=lambda r: r.get("favorited_at", ""), reverse=True)
        return items

    # ------------------------------------------------------------------ #
    # 收藏
    # ------------------------------------------------------------------ #
    def add(self, source_id: str, url: str, title: str,
            content_type: str = "", cover: str = "",
            author: str = "", tags: list | None = None,
            folder: str = "") -> dict:
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
            "folder": folder,
            "favorited_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with self._lock:
            self._data[url] = rec
            self._save()
        return dict(rec)

    def set_folder(self, url: str, folder: str) -> bool:
        """把某收藏移入指定收藏夹（folder 为空 = 移出到默认）。"""
        with self._lock:
            rec = self._data.get(url)
            if rec is None:
                return False
            rec["folder"] = folder
            self._save()
        return True

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

    # ------------------------------------------------------------------ #
    # 导出（书架设置里的导出目录/按钮用）
    # ------------------------------------------------------------------ #
    def export_backup(self, path: str | Path) -> Path:
        """导出书架全部数据（收藏 + 收藏夹）到指定 JSON 文件，返回落盘路径。

        与内部持久化同构（{"favorites": {...}, "folders": [...]}），
        便于备份或迁移。父目录不存在会自动创建。
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with self._lock:
            payload = {
                "favorites": json.loads(json.dumps(self._data)),
                "folders": list(self._folders),
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
        return path
