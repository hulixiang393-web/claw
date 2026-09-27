from __future__ import annotations

import sqlite3
from pathlib import Path

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
