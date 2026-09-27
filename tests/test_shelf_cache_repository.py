from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from framework.shelf_cache_repository import ShelfCacheRepository


def make_repo(tmp_path: Path, **kwargs) -> ShelfCacheRepository:
    return ShelfCacheRepository(
        tmp_path / "shelf.sqlite3",
        tmp_path / "content",
        **kwargs,
    )


def test_schema_book_chapter_snapshot_and_restart(tmp_path: Path):
    db = tmp_path / "shelf.sqlite3"
    repo = ShelfCacheRepository(db, tmp_path / "content")
    repo.upsert_book("book", "source", "https://book", "novel", {"title": "Title"})
    repo.replace_chapters("book", [{"chapter_key": "c1", "title": "One", "url": "u1"}])
    repo.update_location("book", {"chapter_key": "c1", "position": 0.5})
    repo.update_session("book", {"last_opened": 12})
    snapshot = repo.get_book_snapshot("book")
    assert snapshot["book_key"] == "book"
    assert snapshot["metadata"] == {"title": "Title"}
    assert snapshot["chapters"] == [{"chapter_key": "c1", "title": "One", "url": "u1"}]
    assert snapshot["location"] == {"chapter_key": "c1", "position": 0.5}
    assert snapshot["session"] == {"last_opened": 12}
    repo.close()

    restarted = ShelfCacheRepository(db, tmp_path / "content")
    assert restarted.get_book_snapshot("book") == snapshot
    assert restarted._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert restarted._db.execute("SELECT version FROM schema_version").fetchone()[0] >= 1


def test_atomic_text_and_image_content_are_idempotent(tmp_path: Path):
    repo = make_repo(tmp_path)
    repo.upsert_book("book", "s", "u", "novel", {})
    repo.replace_chapters("book", [{"chapter_key": "c1", "title": "One"}])
    text = repo.put_content("book", "c1", "text/plain", "hello", {"etag": "1"})
    image = repo.put_content("book", "cover", "image/png", b"PNG", {})
    assert repo.get_content("book", "c1") == "hello"
    assert repo.get_content("book", "cover") == b"PNG"
    duplicate = repo.put_content("book", "c1", "text/plain", "hello", {"etag": "1"})
    assert duplicate["checksum"] == text["checksum"]
    assert duplicate["path"] == text["path"]
    assert repo.get_book_snapshot("book")["content"] and len(repo.get_book_snapshot("book")["content"]) == 2
    assert not list((tmp_path / "content").rglob("*.tmp"))
    assert text["size"] == 5 and image["size"] == 3


def test_old_file_cleanup_failure_keeps_new_payload_and_index(tmp_path: Path, monkeypatch):
    repo = make_repo(tmp_path)
    repo.upsert_book("book", "s", "u", "novel", {})
    old = repo.put_content("book", "c1", "text/plain", "old", {})
    original_unlink = Path.unlink

    def fail_old(path, missing_ok=False):
        if str(path) == old["path"]:
            raise OSError("injected old-file cleanup failure")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_old)
    new = repo.put_content("book", "c1", "text/plain", "new", {})
    assert repo.get_content("book", "c1") == "new"
    assert Path(new["path"]).exists()
    assert repo.get_book_snapshot("book")["content"][0]["checksum"] == new["checksum"]


def test_missing_file_is_repaired_by_removing_stale_index(tmp_path: Path):
    repo = make_repo(tmp_path)
    repo.upsert_book("book", "s", "u", "novel", {})
    repo.put_content("book", "c1", "text/plain", "hello", {})
    path = next((tmp_path / "content").rglob("*.gz"))
    path.unlink()
    assert repo.get_content("book", "c1") is None
    assert repo.get_book_snapshot("book")["content"] == []


def test_per_book_and_global_limits_evict_old_content(tmp_path: Path):
    repo = ShelfCacheRepository(tmp_path / "db", tmp_path / "content", max_book_bytes=5, max_total_bytes=8)
    repo.upsert_book("a", "s", "a", "novel", {})
    repo.upsert_book("b", "s", "b", "novel", {})
    repo.put_content("a", "1", "text/plain", "12345", {})
    repo.put_content("a", "2", "text/plain", "67890", {})
    assert repo.get_content("a", "1") is None
    repo.put_content("b", "1", "text/plain", "abcde", {})
    assert repo.get_content("a", "2") is None
    assert repo.get_content("b", "1") == "abcde"


def test_clear_content_preserves_metadata_and_cleanup(tmp_path: Path):
    now = [1000.0]
    repo = make_repo(tmp_path, clock=lambda: now[0])
    repo.upsert_book("book", "s", "u", "novel", {"title": "T"})
    repo.replace_chapters("book", [{"chapter_key": "c1", "title": "One"}])
    repo.update_location("book", {"chapter_key": "c1"})
    repo.update_session("book", {"x": 1})
    repo.put_content("book", "c1", "text/plain", "hello", {})
    repo.clear_content_cache("book")
    snapshot = repo.get_book_snapshot("book")
    assert snapshot["content"] == []
    assert snapshot["chapters"]
    assert snapshot["location"] == {"chapter_key": "c1"}
    assert snapshot["session"] == {"x": 1}
    repo.put_content("book", "c1", "text/plain", "hello", {})
    now[0] = 1000 + 10 * 86400
    assert repo.cleanup_inactive(5) == 1
    assert repo.get_book_snapshot("book")["content"] == []
    assert repo.get_book_snapshot("book")["chapters"]


def test_startup_reconciles_orphan_and_temp_files_but_preserves_indexed_content(tmp_path: Path):
    repo = make_repo(tmp_path)
    repo.upsert_book("book", "s", "u", "novel", {})
    repo.put_content("book", "c1", "text/plain", "keep", {})
    root = tmp_path / "content"
    (root / "orphan.gz").write_bytes(b"orphan")
    (root / "interrupted.tmp").write_bytes(b"partial")
    repo.close()
    restarted = make_repo(tmp_path)
    assert restarted.get_content("book", "c1") == "keep"
    assert not (root / "orphan.gz").exists()
    assert not (root / "interrupted.tmp").exists()


def test_failed_content_index_write_removes_new_payload_artifact(tmp_path: Path, monkeypatch):
    repo = make_repo(tmp_path)
    repo.upsert_book("book", "s", "u", "novel", {})
    def fail_content_insert(row, now, book_key):
        raise sqlite3.OperationalError("injected failure")

    monkeypatch.setattr(repo, "_write_content_index", fail_content_insert)
    with pytest.raises(sqlite3.OperationalError):
        repo.put_content("book", "c1", "text/plain", "failed", {})
    assert repo.get_book_snapshot("book")["content"] == []
    assert not list((tmp_path / "content").rglob("*.gz"))
    assert not list((tmp_path / "content").rglob("*.tmp"))


def test_concurrent_content_writes_do_not_collide(tmp_path: Path):
    repo = make_repo(tmp_path)
    repo.upsert_book("book", "s", "u", "novel", {})
    errors = []

    def write(chapter: str):
        try:
            repo.put_content("book", chapter, "text/plain", chapter, {})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(f"c{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert {repo.get_content("book", f"c{i}") for i in range(8)} == {f"c{i}" for i in range(8)}


def test_cleanup_inactive_is_atomic_when_file_delete_fails(tmp_path: Path, monkeypatch):
    repo = make_repo(tmp_path)
    for book in ("a", "b"):
        repo.upsert_book(book, "s", book, "novel", {})
        repo.put_content(book, "c", "text/plain", book, {})
    original = Path.replace
    calls = []

    def fail_second(path, target):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("injected delete failure")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", fail_second)
    with pytest.raises(OSError):
        repo.cleanup_inactive(0, now=repo.clock() + 1)
    assert repo.get_content("a", "c") == "a"
    assert repo.get_content("b", "c") == "b"


def test_unsupported_schema_version_is_rejected(tmp_path: Path):
    db = tmp_path / "shelf.sqlite3"
    repo = make_repo(tmp_path)
    repo.close()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE schema_version SET version=999")
    with pytest.raises(RuntimeError, match="schema version"):
        ShelfCacheRepository(db, tmp_path / "content")
