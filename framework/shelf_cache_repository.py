from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable


class ShelfCacheRepository:
    def __init__(
        self,
        path: str | Path,
        content_root: str | Path,
        clock: Callable[[], float] | None = None,
        max_book_bytes: int = 256 * 1024 * 1024,
        max_total_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        self.path = Path(path)
        self.content_root = Path(content_root)
        self.max_book_bytes = max(1, int(max_book_bytes))
        self.max_total_bytes = max(1, int(max_total_bytes))
        self.clock = clock or time.time
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.content_root.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._init_schema()
        self._validate_schema_version()
        self._repair_index()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _init_schema(self) -> None:
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version(version)
                    SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
                CREATE TABLE IF NOT EXISTS books (
                    book_key TEXT PRIMARY KEY, source_id TEXT NOT NULL, book_url TEXT NOT NULL,
                    content_type TEXT NOT NULL, metadata TEXT NOT NULL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, last_active_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chapters (
                    book_key TEXT NOT NULL, chapter_key TEXT NOT NULL, chapter_json TEXT NOT NULL,
                    PRIMARY KEY(book_key, chapter_key), FOREIGN KEY(book_key) REFERENCES books(book_key) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS locations (
                    book_key TEXT PRIMARY KEY, value_json TEXT NOT NULL,
                    FOREIGN KEY(book_key) REFERENCES books(book_key) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    book_key TEXT PRIMARY KEY, value_json TEXT NOT NULL,
                    FOREIGN KEY(book_key) REFERENCES books(book_key) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS content (
                    book_key TEXT NOT NULL, chapter_key TEXT NOT NULL, content_type TEXT NOT NULL,
                    path TEXT NOT NULL, size INTEGER NOT NULL, metadata TEXT NOT NULL,
                    checksum TEXT NOT NULL, created_at REAL NOT NULL, accessed_at REAL NOT NULL,
                    PRIMARY KEY(book_key, chapter_key), FOREIGN KEY(book_key) REFERENCES books(book_key) ON DELETE CASCADE
                );
                """
            )

    def _validate_schema_version(self) -> None:
        row = self._db.execute("SELECT version FROM schema_version").fetchone()
        if not row or row[0] != 1:
            raise RuntimeError(f"unsupported schema version: {row[0] if row else None}")

    def _repair_index(self) -> None:
        with self._lock, self._db:
            rows = self._db.execute("SELECT book_key, chapter_key, path FROM content").fetchall()
            indexed = set()
            for row in rows:
                path = Path(row["path"])
                indexed.add(path.resolve())
                if not path.exists():
                    self._db.execute("DELETE FROM content WHERE book_key=? AND chapter_key=?", (row["book_key"], row["chapter_key"]))
            for path in self.content_root.rglob("*"):
                if path.is_file() and (path.suffix == ".tmp" or path.name.startswith(".delete-") or path.resolve() not in indexed):
                    path.unlink(missing_ok=True)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _loads(value: str) -> Any:
        return json.loads(value)

    def upsert_book(self, book_key: str, source_id: str, book_url: str, content_type: str, metadata: dict) -> None:
        now = self.clock()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO books(book_key,source_id,book_url,content_type,metadata,created_at,updated_at,last_active_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(book_key) DO UPDATE SET source_id=excluded.source_id, book_url=excluded.book_url,
                   content_type=excluded.content_type, metadata=excluded.metadata, updated_at=excluded.updated_at,
                   last_active_at=excluded.last_active_at""",
                (book_key, source_id, book_url, content_type, self._json(metadata), now, now, now),
            )

    def replace_chapters(self, book_key: str, chapters: list[dict]) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM chapters WHERE book_key=?", (book_key,))
            self._db.executemany(
                "INSERT INTO chapters(book_key,chapter_key,chapter_json) VALUES(?,?,?)",
                [(book_key, c["chapter_key"], self._json(c)) for c in chapters],
            )

    def update_location(self, book_key: str, location: dict) -> None:
        self._upsert_state("locations", book_key, location)

    def update_session(self, book_key: str, session: dict) -> None:
        self._upsert_state("sessions", book_key, session)

    def _upsert_state(self, table: str, book_key: str, value: dict) -> None:
        with self._lock, self._db:
            self._db.execute(f"INSERT INTO {table}(book_key,value_json) VALUES(?,?) ON CONFLICT(book_key) DO UPDATE SET value_json=excluded.value_json", (book_key, self._json(value)))
            self._db.execute("UPDATE books SET last_active_at=? WHERE book_key=?", (self.clock(), book_key))

    def get_book_snapshot(self, book_key: str) -> dict | None:
        with self._lock:
            book = self._db.execute("SELECT * FROM books WHERE book_key=?", (book_key,)).fetchone()
            if not book:
                return None
            chapters = [self._loads(r[0]) for r in self._db.execute("SELECT chapter_json FROM chapters WHERE book_key=? ORDER BY rowid", (book_key,))]
            content = [dict(r) for r in self._db.execute("SELECT chapter_key,content_type,size,metadata,checksum,accessed_at FROM content WHERE book_key=? ORDER BY chapter_key", (book_key,))]
            for item in content:
                item["metadata"] = self._loads(item["metadata"])
            result = {"book_key": book_key, "source_id": book["source_id"], "book_url": book["book_url"], "content_type": book["content_type"], "metadata": self._loads(book["metadata"]), "chapters": chapters, "content": content}
            for table, key in (("locations", "location"), ("sessions", "session")):
                row = self._db.execute(f"SELECT value_json FROM {table} WHERE book_key=?", (book_key,)).fetchone()
                result[key] = self._loads(row[0]) if row else None
            return result

    def _new_payload_paths(self) -> tuple[Path, Path]:
        operation = uuid.uuid4().hex
        fd, tmp_name = tempfile.mkstemp(prefix=f".payload-{operation}-", suffix=".tmp", dir=self.content_root)
        os.close(fd)
        return Path(tmp_name), self.content_root / f"payload-{operation}.gz"

    def _write_content_index(self, row: dict, now: float, book_key: str) -> None:
        self._db.execute("INSERT INTO content VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(book_key,chapter_key) DO UPDATE SET content_type=excluded.content_type,path=excluded.path,size=excluded.size,metadata=excluded.metadata,checksum=excluded.checksum,accessed_at=excluded.accessed_at", tuple(row.values()))
        self._db.execute("UPDATE books SET last_active_at=? WHERE book_key=?", (now, book_key))
        self._evict_locked()

    def put_content(self, book_key: str, chapter_key: str, content_type: str, payload: bytes | str, metadata: dict) -> dict:
        raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
        checksum = hashlib.sha256(raw).hexdigest()
        with self._lock:
            existing = self._db.execute("SELECT * FROM content WHERE book_key=? AND chapter_key=?", (book_key, chapter_key)).fetchone()
            if existing and existing["checksum"] == checksum and existing["content_type"] == content_type and existing["metadata"] == self._json(metadata) and Path(existing["path"]).exists():
                row = dict(existing)
                row["metadata"] = metadata
                return row
            tmp, path = self._new_payload_paths()
            try:
                with gzip.open(tmp, "wb") as fh:
                    fh.write(raw)
                    fh.flush()
                os.replace(tmp, path)
                now = self.clock()
                created_at = existing["created_at"] if existing else now
                row = {"book_key": book_key, "chapter_key": chapter_key, "content_type": content_type, "path": str(path), "size": len(raw), "metadata": self._json(metadata), "checksum": checksum, "created_at": created_at, "accessed_at": now}
                with self._db:
                    self._write_content_index(row, now, book_key)
                if existing:
                    Path(existing["path"]).unlink(missing_ok=True)
                row["metadata"] = metadata
                return row
            except Exception:
                tmp.unlink(missing_ok=True)
                path.unlink(missing_ok=True)
                raise

    def get_content(self, book_key: str, chapter_key: str) -> bytes | str | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM content WHERE book_key=? AND chapter_key=?", (book_key, chapter_key)).fetchone()
            if not row:
                return None
            try:
                with gzip.open(row["path"], "rb") as fh:
                    raw = fh.read()
            except OSError:
                with self._db:
                    self._db.execute("DELETE FROM content WHERE book_key=? AND chapter_key=?", (book_key, chapter_key))
                return None
            with self._db:
                self._db.execute("UPDATE content SET accessed_at=? WHERE book_key=? AND chapter_key=?", (self.clock(), book_key, chapter_key))
            return raw.decode("utf-8") if row["content_type"].startswith(("text/", "application/json", "application/xml")) else raw

    def _evict_locked(self) -> None:
        for book_key in [r[0] for r in self._db.execute("SELECT book_key FROM books")]:
            while self._db.execute("SELECT COALESCE(SUM(size),0) FROM content WHERE book_key=?", (book_key,)).fetchone()[0] > self.max_book_bytes:
                self._delete_oldest_locked(book_key)
        while self._db.execute("SELECT COALESCE(SUM(size),0) FROM content").fetchone()[0] > self.max_total_bytes:
            self._delete_oldest_locked(None)

    def _delete_oldest_locked(self, book_key: str | None) -> None:
        query = "SELECT book_key,chapter_key,path FROM content" + (" WHERE book_key=?" if book_key else "") + " ORDER BY accessed_at,created_at LIMIT 1"
        row = self._db.execute(query, (book_key,) if book_key else ()).fetchone()
        if not row:
            return
        Path(row["path"]).unlink(missing_ok=True)
        self._db.execute("DELETE FROM content WHERE book_key=? AND chapter_key=?", (row["book_key"], row["chapter_key"]))

    def evict_book_content(self, book_key: str, keep_chapters: list[str]) -> None:
        with self._lock, self._db:
            rows = self._db.execute("SELECT chapter_key,path FROM content WHERE book_key=?", (book_key,)).fetchall()
            for row in rows:
                if row["chapter_key"] not in keep_chapters:
                    Path(row["path"]).unlink(missing_ok=True)
                    self._db.execute("DELETE FROM content WHERE book_key=? AND chapter_key=?", (book_key, row["chapter_key"]))

    def clear_content_cache(self, book_key: str | None = None) -> None:
        with self._lock, self._db:
            rows = self._db.execute("SELECT book_key,chapter_key,path FROM content" + (" WHERE book_key=?" if book_key else ""), (book_key,) if book_key else ()).fetchall()
            for row in rows:
                Path(row["path"]).unlink(missing_ok=True)
            if book_key:
                self._db.execute("DELETE FROM content WHERE book_key=?", (book_key,))
            else:
                self._db.execute("DELETE FROM content")

    def cleanup_inactive(self, days: int, now: float | None = None) -> int:
        cutoff = (self.clock() if now is None else now) - days * 86400
        with self._lock:
            books = [r[0] for r in self._db.execute("SELECT book_key FROM books WHERE last_active_at < ?", (cutoff,))]
            moved: list[tuple[Path, Path]] = []
            try:
                for book_key in books:
                    rows = self._db.execute("SELECT book_key,chapter_key,path FROM content WHERE book_key=?", (book_key,)).fetchall()
                    for row in rows:
                        source = Path(row["path"])
                        trash = source.with_name(f".delete-{uuid.uuid4().hex}.tmp")
                        if source.exists():
                            source.replace(trash)
                            moved.append((trash, source))
                    for row in rows:
                        self._db.execute("DELETE FROM content WHERE book_key=? AND chapter_key=?", (row["book_key"], row["chapter_key"]))
                self._db.commit()
            except Exception:
                self._db.rollback()
                for trash, source in reversed(moved):
                    if trash.exists():
                        trash.replace(source)
                raise
            for trash, _ in moved:
                trash.unlink(missing_ok=True)
            return len(books)
