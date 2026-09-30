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


def _normalize_folder_name(raw) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _normalize_locked(raw) -> bool:
    return raw if isinstance(raw, bool) else False


def _normalize_lock_text(raw) -> Optional[str]:
    return raw if isinstance(raw, str) and raw else None


def _folder_record(name, locked=False, pw=None, salt=None,
                   recovery_pw=None, recovery_salt=None) -> dict:
    return {
        "name": _normalize_folder_name(name),
        "locked": _normalize_locked(locked),
        "pw": _normalize_lock_text(pw),
        "salt": _normalize_lock_text(salt),
        "recovery_pw": _normalize_lock_text(recovery_pw),
        "recovery_salt": _normalize_lock_text(recovery_salt),
    }


def _normalize_folder_entry(raw) -> dict:
    """兼容字符串和对象两种收藏夹格式，统一为对象记录。"""
    if isinstance(raw, str):
        return _folder_record(raw)
    if isinstance(raw, dict):
        return _folder_record(
            raw.get("name"), raw.get("locked", False), raw.get("pw"), raw.get("salt"),
            raw.get("recovery_pw"), raw.get("recovery_salt")
        )
    return _folder_record("")


class LibraryStore:
    """收藏存储：JSON 文件读写，线程安全。"""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._folder_locks: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        try:
            if self._path.is_file():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    # 新格式 {"favorites": {...}, "folders": [...]}；旧格式直接是 dict
                    self._data = raw.get("favorites", raw) if isinstance(raw, dict) else raw
                    if isinstance(self._data, dict):
                        for favorite in self._data.values():
                            if isinstance(favorite, dict) and "folder" in favorite:
                                favorite["folder"] = _normalize_folder_name(
                                    favorite.get("folder")
                                )
                    folders = raw.get("folders")
                    if isinstance(folders, list):
                        for folder in folders:
                            rec = _normalize_folder_entry(folder)
                            if rec["name"]:
                                self._folder_locks[rec["name"]] = rec
        except (OSError, json.JSONDecodeError):
            self._data = {}  # 损坏文件 → 空收藏，不崩溃

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "favorites": self._data,
                    "folders": sorted(
                        self._folder_locks.values(), key=lambda rec: rec["name"]
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    # ------------------------------------------------------------------ #
    # 收藏夹
    # ------------------------------------------------------------------ #
    def create_folder(self, name: str, locked: bool = False,
                      pw=None, salt=None, recovery_pw=None,
                      recovery_salt=None) -> bool:
        """新建收藏夹（空夹也保留）。同名返回 False。"""
        name = _normalize_folder_name(name)
        if not name:
            return False
        with self._lock:
            if name in self._folder_locks:
                return False
            self._folder_locks[name] = _folder_record(
                name, locked, pw, salt, recovery_pw, recovery_salt
            )
            self._save()
        return True

    def rename_folder(self, old: str, new: str) -> bool:
        """重命名收藏夹（同步更新其中的收藏记录）。"""
        old, new = _normalize_folder_name(old), _normalize_folder_name(new)
        if not old or not new or old == new:
            return False
        with self._lock:
            if old not in self._folder_locks or new in self._folder_locks:
                return False
            rec = self._folder_locks.pop(old)
            rec["name"] = new
            self._folder_locks[new] = rec
            for rec in self._data.values():
                if rec.get("folder") == old:
                    rec["folder"] = new
            self._save()
        return True

    def delete_folder(self, name: str) -> bool:
        """删除收藏夹：夹内收藏移回未归类（不删收藏）。"""
        name = _normalize_folder_name(name)
        if not name:
            return False
        with self._lock:
            had_record = name in self._folder_locks
            had_favorites = any(
                rec.get("folder") == name for rec in self._data.values()
            )
            if not had_record and not had_favorites:
                return False
            self._folder_locks.pop(name, None)
            for rec in self._data.values():
                if rec.get("folder") == name:
                    rec["folder"] = ""
            self._save()
        return True

    def list_folders(self) -> list[str]:
        """现有收藏夹名（含空夹，排序）。"""
        with self._lock:
            folders = set(self._folder_locks)
            folders.update(r.get("folder", "") for r in self._data.values())
        return sorted(f for f in folders if f)

    def folder_items(self, folder: str) -> list[dict]:
        """某收藏夹内的收藏记录。folder="" = 未归类。"""
        with self._lock:
            items = [dict(v) for v in self._data.values()
                     if (v.get("folder") or "") == folder]
        items.sort(key=lambda r: r.get("favorited_at", ""), reverse=True)
        return items

    def folder_info(self, name: str) -> Optional[dict]:
        """返回收藏夹记录的副本；收藏夹不存在时返回 None。"""
        name = _normalize_folder_name(name)
        with self._lock:
            rec = self._folder_locks.get(name)
            return dict(rec) if rec is not None else None

    def set_folder_lock(self, name: str, locked: bool,
                        pw=None, salt=None, recovery_pw=None,
                        recovery_salt=None) -> bool:
        """更新收藏夹锁状态和凭据元数据。"""
        name = _normalize_folder_name(name)
        with self._lock:
            rec = self._folder_locks.get(name)
            if rec is None:
                return False
            rec["locked"] = _normalize_locked(locked)
            rec["pw"] = _normalize_lock_text(pw)
            rec["salt"] = _normalize_lock_text(salt)
            rec["recovery_pw"] = _normalize_lock_text(recovery_pw)
            rec["recovery_salt"] = _normalize_lock_text(recovery_salt)
            self._save()
        return True

    def clear_folder_lock(self, name: str) -> bool:
        """清除收藏夹锁和凭据元数据。"""
        return self.set_folder_lock(name, False, None, None)

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

    def set_cover(self, url: str, cover: str) -> bool:
        """后台补写收藏封面（仅改 cover，保留收藏时间等其余元数据）。"""
        if not url or not cover:
            return False
        with self._lock:
            rec = self._data.get(url)
            if rec is None:
                return False
            rec["cover"] = cover
            self._save()
        return True

    def clear_folder(self, folder: str) -> int:
        """一键清空某收藏夹内的全部收藏（保留收藏夹本身，不删本地文件）。

        返回移除条数；folder 为空或不存在该夹返回 0。
        """
        folder = folder.strip()
        if not folder:
            return 0
        with self._lock:
            urls = [u for u, v in self._data.items() if (v.get("folder") or "") == folder]
            for u in urls:
                del self._data[u]
            if urls:
                self._save()
        return len(urls)

    def remove_all(self, exclude_folders: set[str] | None = None) -> int:
        """清空收藏，可排除指定收藏夹（保留收藏夹）。返回移除条数。"""
        excluded = set(exclude_folders or ())
        with self._lock:
            urls = [
                url for url, rec in self._data.items()
                if (rec.get("folder") or "") not in excluded
            ]
            for url in urls:
                del self._data[url]
            if urls:
                self._save()
        return len(urls)

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

        与内部持久化同构（收藏夹为含锁元数据的对象数组），
        便于备份或迁移。父目录不存在会自动创建。
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with self._lock:
            payload = {
                "favorites": json.loads(json.dumps(self._data)),
                "folders": sorted(
                    (
                        json.loads(json.dumps(rec))
                        for rec in self._folder_locks.values()
                    ),
                    key=lambda rec: rec["name"],
                ),
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
        return path

    def inspect_backup(self, path) -> dict:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            return {"ok": False, "error": f"无法读取文件：{exc}", "favorites": 0, "folders": 0}
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": f"JSON 解析失败：{exc}", "favorites": 0, "folders": 0}
        if not isinstance(data, dict):
            return {"ok": False, "error": "顶层结构不是对象。", "favorites": 0, "folders": 0}
        favorites = data.get("favorites")
        if not isinstance(favorites, dict):
            return {"ok": False, "error": "缺少 favorites 字典。", "favorites": 0, "folders": 0}
        folders = data.get("folders", [])
        if not isinstance(folders, list):
            return {"ok": False, "error": "folders 不是数组。", "favorites": 0, "folders": 0}
        return {
            "ok": True,
            "error": "",
            "favorites": len(favorites),
            "folders": len(folders),
        }

    def import_backup(self, path, mode: str = "merge") -> dict:
        if mode not in ("merge", "replace"):
            raise ValueError(f"未知导入模式：{mode}")
        info = self.inspect_backup(path)
        if not info["ok"]:
            raise ValueError(info["error"])
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        incoming = data["favorites"]
        incoming_folders = data.get("folders", [])

        import copy

        with self._lock:
            old_data = self._data
            old_folders = self._folder_locks
            staged_data = copy.deepcopy(old_data) if mode == "merge" else {}
            staged_folders = copy.deepcopy(old_folders) if mode == "merge" else {}
            imported = 0
            skipped = 0

            for url, record in incoming.items():
                if not isinstance(record, dict):
                    skipped += 1
                    continue
                key = url or record.get("url")
                if not key:
                    skipped += 1
                    continue
                new_record = copy.deepcopy(record)
                old_record = staged_data.get(key)
                if mode == "merge" and isinstance(old_record, dict):
                    old_ts = old_record.get("favorited_at") or ""
                    new_ts = new_record.get("favorited_at") or ""
                    if old_ts and new_ts:
                        new_record["favorited_at"] = min(old_ts, new_ts)
                    elif old_ts:
                        new_record["favorited_at"] = old_ts
                    elif new_ts:
                        new_record["favorited_at"] = new_ts
                    old_folder = old_record.get("folder") or ""
                    if old_folder and staged_folders.get(old_folder, {}).get("locked"):
                        new_record["folder"] = old_folder
                staged_data[key] = new_record
                imported += 1

            folders_added = 0
            for entry in incoming_folders:
                folder = _normalize_folder_entry(entry)
                name = folder["name"]
                if not name or name in staged_folders:
                    continue
                staged_folders[name] = folder
                folders_added += 1

            self._data = staged_data
            self._folder_locks = staged_folders
            try:
                self._save()
            except Exception:
                self._data = old_data
                self._folder_locks = old_folders
                raise

        return {
            "imported": imported,
            "skipped": skipped,
            "folders_added": folders_added,
        }
