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


def test_repair_index_false_defers_full_repair_until_requested(tmp_path: Path):
    root = tmp_path / "content"
    root.mkdir()
    orphan = root / "payload-orphan.gz"
    orphan.write_bytes(b"orphan")

    repo = ShelfCacheRepository(
        tmp_path / "shelf.sqlite3",
        root,
        repair_index=False,
    )

    assert orphan.exists()
    repo.repair_index()
    assert not orphan.exists()


def test_repair_index_entry_point_repairs_stale_index(tmp_path: Path):
    repo = make_repo(tmp_path)
    repo.upsert_book("book", "s", "u", "novel", {})
    repo.put_content("book", "c1", "text/plain", "hello", {})
    path = next((tmp_path / "content").rglob("*.gz"))
    path.unlink()

    deferred = ShelfCacheRepository(
        tmp_path / "shelf.sqlite3",
        tmp_path / "content",
        repair_index=False,
    )
    assert deferred.get_book_snapshot("book")["content"]
    deferred.repair_index()
    assert deferred.get_book_snapshot("book")["content"] == []


def test_shelf_progress_summary_reads_only_location_and_counts(tmp_path, monkeypatch):
    from framework.reading_progress import ReadingProgress

    repo = make_repo(tmp_path)
    repo.upsert_book("book", "s", "u", "novel", {})
    repo.replace_chapters("book", [{"chapter_key": "c1", "title": "目录"}])
    repo.put_content("book", "c1", "text/plain", "正文", {})
    repo.update_location("book", {"chapter_title": "第一章", "updated_at": "now"})
    progress = ReadingProgress(tmp_path / "progress.json", repository=repo)
    calls = []
    for name in ("get_book_snapshot", "get_content", "cached_chapter_keys"):
        monkeypatch.setattr(repo, name, lambda *a, n=name: calls.append(n))
    queries = []
    repo._db.set_trace_callback(queries.append)
    assert progress.summary("book") == {"chapter_title": "第一章", "updated_at": "now"}
    assert repo.cached_chapter_count("book") == 1
    assert calls == []
    assert len(queries) == 2
    assert "locations" in queries[0]
    assert "COUNT(*)" in queries[1]
    repo.close()


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
    (root / "payload-orphan.gz").write_bytes(b"orphan")
    (root / ".payload-interrupted.tmp").write_bytes(b"partial")
    repo.close()
    restarted = make_repo(tmp_path)
    assert restarted.get_content("book", "c1") == "keep"
    assert not (root / "payload-orphan.gz").exists()
    assert not (root / ".payload-interrupted.tmp").exists()


def test_repair_preserves_neighbor_redis_and_legacy_shared_payload(tmp_path: Path):
    shared = tmp_path / "cache"
    content_root = shared / "payload"
    repo = ShelfCacheRepository(tmp_path / "shelf.sqlite3", content_root, repair_index=False)
    repo.upsert_book("book", "s", "u", "novel", {})
    repo.put_content("book", "c1", "text/plain", "keep", {})
    (shared / "redis_shelf.gz").write_bytes(b"redis-neighbor")
    legacy = shared / "payload-legacy.gz"
    legacy.write_bytes(b"legacy-shared-payload")
    orphan = content_root / "payload-orphan.gz"
    orphan.write_bytes(b"orphan")
    neighbor_tmp = content_root / "redis_shelf.gz.tmp"
    neighbor_tmp.write_bytes(b"neighbor-tmp")
    (content_root / ".payload-interrupted.tmp").write_bytes(b"partial")

    repo.repair_index()

    assert (shared / "redis_shelf.gz").read_bytes() == b"redis-neighbor"
    assert legacy.read_bytes() == b"legacy-shared-payload"
    assert not orphan.exists()
    assert neighbor_tmp.read_bytes() == b"neighbor-tmp"
    assert not (content_root / ".payload-interrupted.tmp").exists()
    repo.close()


def test_legacy_shared_root_payload_survives_restart_on_dedicated_root(tmp_path: Path):
    shared = tmp_path / "cache"
    legacy = ShelfCacheRepository(tmp_path / "shelf.sqlite3", shared, repair_index=False)
    legacy.upsert_book("book", "s", "u", "novel", {})
    legacy.put_content("book", "c1", "text/plain", "legacy", {})
    old_path = next((shared).rglob("payload-*.gz"))
    legacy.close()

    payload_root = shared / "payload"
    payload_root.mkdir(parents=True, exist_ok=True)
    (payload_root / "payload-orphan.gz").write_bytes(b"orphan")
    restarted = ShelfCacheRepository(tmp_path / "shelf.sqlite3", payload_root)

    assert restarted.get_content("book", "c1") == "legacy"
    assert old_path.exists()
    assert not (payload_root / "payload-orphan.gz").exists()
    restarted.close()


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


def test_cleanup_inactive_never_purges_pinned_books(tmp_path: Path):
    now = [1000.0]
    repo = make_repo(tmp_path, clock=lambda: now[0])
    repo.upsert_book("keep", "s", "u", "novel", {})
    repo.upsert_book("stale", "s", "v", "novel", {})
    repo.put_content("keep", "c", "text/plain", "K", {})
    repo.put_content("stale", "c", "text/plain", "S", {})
    repo.set_book_pinned("keep", True)

    now[0] = 1000 + 30 * 86400
    assert repo.cleanup_inactive(5) == 1  # 仅未固定且不活跃的 stale 被清理

    assert repo.get_content("keep", "c") == "K"
    assert repo.get_content("stale", "c") is None


def test_prune_video_history_can_scope_to_single_book(tmp_path: Path):
    repo = make_repo(tmp_path)
    stale = {"content_type": "video", "history_timestamp": 1.0,
             "chapter_url": "c", "position": 5}
    repo.upsert_book("a", "s", "a", "video", dict(stale))
    repo.upsert_book("b", "s", "b", "video", dict(stale))

    repo.prune_video_history("a")

    def meta(key):
        row = repo._db.execute(
            "SELECT metadata FROM books WHERE book_key=?", (key,)
        ).fetchone()
        return repo._loads(row[0])

    a = meta("a")
    assert "history_timestamp" not in a
    assert a["position"] == 5  # 续读字段保留（仅清理历史凭据）
    assert "history_timestamp" in meta("b")  # b 未被波及（作用域限定）


def test_content_io_runs_outside_repository_lock(tmp_path: Path, monkeypatch):
    """gzip 压缩/解压必须移出锁外（锁仅护共享内存结构），不阻塞并发读者。"""
    import framework.shelf_cache_repository as mod

    repo = make_repo(tmp_path)
    repo.upsert_book("book", "s", "u", "novel", {})
    owned: list[bool] = []
    real_open = mod.gzip.open

    def spy(*args, **kwargs):
        owned.append(repo._lock._is_owned())
        return real_open(*args, **kwargs)

    monkeypatch.setattr(mod.gzip, "open", spy)
    repo.put_content("book", "c1", "text/plain", "hello", {})
    assert repo.get_content("book", "c1") == "hello"
    assert owned == [False, False]  # 压缩、解压时均未持锁
