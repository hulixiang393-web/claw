from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class MigrationReport:
    imported_books: int = 0
    imported_chapters: int = 0
    imported_locations: int = 0
    imported_content: int = 0


def _timestamp(record: dict) -> str:
    return str(record.get("updated_at") or "")


def _newer(candidate: dict, existing: dict | None) -> bool:
    if not existing:
        return True
    return _timestamp(candidate) > _timestamp(existing)


def migrate_legacy_data(repository, reading_progress_path, shelf_service, legacy_cache, legacy_chapters_root=None) -> MigrationReport:
    progress_path = Path(reading_progress_path)
    raw = {}
    if progress_path.exists():
        raw = json.loads(progress_path.read_text(encoding="utf-8"))
    progress_records = raw if isinstance(raw, dict) else {}
    imported_books = imported_chapters = imported_locations = imported_content = 0

    favorites = []
    store = getattr(shelf_service, "_store", None)
    if store is not None:
        favorites = store.list_all()
    by_url = {str(r.get("url")): r for r in favorites if r.get("url")}
    keys = set(progress_records) | set(by_url)

    for book_key in keys:
        record = progress_records.get(book_key) or {}
        favorite = by_url.get(book_key) or {}
        source_id = record.get("source_id", favorite.get("source_id", ""))
        book_url = record.get("book_url", favorite.get("url", book_key))
        content_type = record.get("content_type", favorite.get("content_type", ""))
        metadata = dict(favorite)
        metadata.update({k: v for k, v in record.items() if k not in {"location"}})
        repository.upsert_book(book_key, source_id, book_url, content_type, metadata)
        imported_books += 1
        location = record.get("location")
        if location is None and record.get("chapter_url"):
            location = {
                "chapter_url": record.get("chapter_url"),
                "chapter_title": record.get("chapter_title", ""),
                "position": record.get("position"),
                "page": record.get("page"),
                "updated_at": record.get("updated_at", ""),
            }
        existing_snapshot = repository.get_book_snapshot(book_key)
        existing_location = (existing_snapshot or {}).get("location")
        if location and _newer(location, existing_location):
            repository.update_location(book_key, location)
            imported_locations += 1

    chapters_root = Path(legacy_chapters_root) if legacy_chapters_root else None
    if chapters_root and chapters_root.is_dir():
        for book_key in keys:
            snapshot = repository.get_book_snapshot(book_key)
            if snapshot and snapshot.get("chapters"):
                continue
            record = progress_records.get(book_key) or {}
            source_id = record.get("source_id", "")
            slug = str(book_key).rstrip("/").rsplit("/", 1)[-1]
            normalized = re.sub(r"[^A-Za-z0-9]+", "-", str(book_key).rstrip("/").split("://")[-1]).strip("-")
            names = {slug, normalized}
            candidates = [p for p in chapters_root.rglob("*") if p.is_dir() and p.name in names]
            if source_id:
                candidates = [p for p in candidates if p.parent.name == source_id] or candidates
            chapters = []
            for directory in candidates:
                for path in sorted(directory.glob("*.json")):
                    try:
                        item = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if isinstance(item, dict) and item.get("chapter_key"):
                        chapters.append(item)
            if chapters:
                repository.replace_chapters(book_key, chapters)
                imported_chapters += len(chapters)

    if legacy_cache is not None:
        for key in legacy_cache.scan("*"):
            value = legacy_cache.get(key)
            if value is None:
                continue
            parts = key.split(":", 2)
            if len(parts) != 3 or parts[0] not in {"page", "body", "pages", "cover"}:
                continue
            kind, source_id, url = parts
            book_key = url
            if repository.get_book_snapshot(book_key) is None:
                repository.upsert_book(book_key, source_id, url, "", {})
                imported_books += 1
            if kind == "page":
                payload, content_type = value, "text/html"
            elif kind == "body":
                payload, content_type = value, "text/plain"
            elif kind == "pages":
                payload, content_type = json.dumps(value, ensure_ascii=False), "application/json"
            else:
                payload, content_type = value, "image/jpeg"
            repository.put_content(book_key, kind, content_type, payload, {"legacy_key": key})
            imported_content += 1

    if progress_path.exists():
        backup = progress_path.with_suffix(progress_path.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(progress_path, backup)
    return MigrationReport(imported_books, imported_chapters, imported_locations, imported_content)
