from __future__ import annotations

import json
from pathlib import Path

from framework.cache_service import RedisLikeStore
from framework.library_store import LibraryStore
from framework.reading_progress import ReadingProgress
from framework.shelf_cache_migration import migrate_legacy_data
from framework.shelf_cache_repository import ShelfCacheRepository
from framework.shelf_service import ShelfService


def test_startup_migration_passes_chapter_root_and_imports_sqlite_chapters(tmp_path):
    from gui.app import _run_startup_migration

    progress_path = tmp_path / "data" / "reading_progress.json"
    progress_path.parent.mkdir(parents=True)
    progress_path.write_text(json.dumps({"https://book/1": {
        "source_id": "src", "book_url": "https://book/1", "content_type": "novel"
    }}), encoding="utf-8")
    chapter_dir = tmp_path / "data" / "chapters" / "src" / "book-1"
    chapter_dir.mkdir(parents=True)
    (chapter_dir / "c1.json").write_text(json.dumps({"chapter_key": "c1", "title": "One"}), encoding="utf-8")
    repo = make_repo(tmp_path)
    shelf = ShelfService(tmp_path / "downloads", data_dir=tmp_path / "data")

    _run_startup_migration(repo, progress_path, shelf, None, tmp_path)

    assert repo.get_book_snapshot("https://book/1")["chapters"] == [{"chapter_key": "c1", "title": "One"}]


def test_startup_fallback_rebuilds_progress_without_repository(tmp_path):
    from gui.app import _make_reading_progress

    repo = object()
    progress = _make_reading_progress(tmp_path / "progress.json", lambda _: False)
    assert progress._repository is None
    assert repo is not progress._repository


def make_repo(tmp_path: Path) -> ShelfCacheRepository:
    return ShelfCacheRepository(tmp_path / "shelf.sqlite3", tmp_path / "content")


def test_first_startup_migrates_progress_shelf_and_cache(tmp_path: Path):
    progress_path = tmp_path / "reading_progress.json"
    progress = ReadingProgress(progress_path)
    progress.save("src", "https://book/1", "novel", "https://book/1/c2", "Chapter 2", position=0.4)

    library = LibraryStore(tmp_path / "library.json")
    library.add("src", "https://book/1", "Book", content_type="novel", cover="cover.jpg")
    shelf = ShelfService(tmp_path / "downloads", library_store=library, data_dir=tmp_path / "data")
    cache = RedisLikeStore(1024 * 1024)
    cache.set("page:src:https://book/1", "<detail>")
    cache.set("body:src:https://book/1/c2", "chapter body")
    cache.set("pages:src:https://book/1/c2", ["p1", "p2"])

    repo = make_repo(tmp_path)
    report = migrate_legacy_data(repo, progress_path, shelf, cache)

    snapshot = repo.get_book_snapshot("https://book/1")
    assert snapshot["metadata"]["title"] == "Book"
    assert snapshot["location"]["chapter_url"] == "https://book/1/c2"
    assert snapshot["content"]
    assert report.imported_books == 2
    assert progress_path.with_suffix(progress_path.suffix + ".bak").exists()


def test_migrated_body_is_served_by_repository_first_content(tmp_path: Path):
    progress_path = tmp_path / "reading_progress.json"
    progress_path.write_text(json.dumps({"https://book/1": {
        "source_id": "src", "book_url": "https://book/1", "content_type": "novel"
    }}), encoding="utf-8")
    shelf = ShelfService(tmp_path / "downloads", data_dir=tmp_path / "data")
    cache = RedisLikeStore(1024 * 1024)
    cache.set("body:src:https://book/1/c2", "migrated body")
    repo = make_repo(tmp_path)
    migrate_legacy_data(repo, progress_path, shelf, cache)

    from framework.content import Content
    from tests.test_cache_service import _fake_checker, _fake_parser, _fake_source

    class OfflineHttp:
        defaults = type("D", (), {"timeout": 1, "retries": 0, "interval_ms": 0})()
        def get_text(self, *args, **kwargs):
            raise AssertionError("network must not be used")
    content = Content(OfflineHttp(), _fake_parser(), _fake_checker(), repository=repo)
    assert content.fetch_chapter(_fake_source(), "https://book/1/c2") == "migrated body"


def test_second_startup_is_idempotent_and_does_not_regress_newer_location(tmp_path: Path):
    progress_path = tmp_path / "reading_progress.json"
    progress_path.write_text(json.dumps({"https://book/1": {
        "source_id": "src", "book_url": "https://book/1", "content_type": "novel",
        "chapter_url": "old", "chapter_title": "Old", "updated_at": "2026-09-28T10:00:00"
    }}), encoding="utf-8")
    repo = make_repo(tmp_path)
    repo.upsert_book("https://book/1", "src", "https://book/1", "novel", {"title": "Book"})
    repo.update_location("https://book/1", {"chapter_url": "new", "chapter_title": "New", "updated_at": "2026-09-28T12:00:00"})
    shelf = ShelfService(tmp_path / "downloads", data_dir=tmp_path / "data")

    first = migrate_legacy_data(repo, progress_path, shelf, None)
    second = migrate_legacy_data(repo, progress_path, shelf, None)

    assert repo.get_book_snapshot("https://book/1")["location"]["chapter_url"] == "new"
    assert first.imported_locations == 0
    assert second.imported_locations == 0


def test_migrates_legacy_chapter_directory(tmp_path: Path):
    progress_path = tmp_path / "reading_progress.json"
    progress_path.write_text(json.dumps({"https://book/1": {
        "source_id": "src", "book_url": "https://book/1", "content_type": "novel",
        "chapter_url": "https://book/1/c2", "chapter_title": "Chapter 2",
        "updated_at": "2026-09-28T10:00:00"
    }}), encoding="utf-8")
    chapter_dir = tmp_path / "chapters" / "src" / "book-1"
    chapter_dir.mkdir(parents=True)
    (chapter_dir / "c1.json").write_text(json.dumps({"chapter_key": "c1", "title": "One", "url": "u1"}), encoding="utf-8")
    (chapter_dir / "c2.json").write_text(json.dumps({"chapter_key": "c2", "title": "Two", "url": "u2"}), encoding="utf-8")
    shelf = ShelfService(tmp_path / "downloads", data_dir=tmp_path / "data")
    repo = make_repo(tmp_path)

    report = migrate_legacy_data(repo, progress_path, shelf, None, legacy_chapters_root=tmp_path / "chapters")

    assert report.imported_chapters == 2
    assert [c["chapter_key"] for c in repo.get_book_snapshot("https://book/1")["chapters"]] == ["c1", "c2"]


def test_failed_migration_keeps_legacy_progress_readable_and_writable(tmp_path: Path):
    progress_path = tmp_path / "reading_progress.json"
    progress_path.write_text(json.dumps({"https://book/1": {
        "source_id": "src", "book_url": "https://book/1", "content_type": "novel",
        "chapter_url": "c1", "chapter_title": "One"
    }}), encoding="utf-8")
    shelf = ShelfService(tmp_path / "downloads", data_dir=tmp_path / "data")
    class BrokenRepository:
        def upsert_book(self, *args, **kwargs):
            raise RuntimeError("broken")

    try:
        migrate_legacy_data(BrokenRepository(), progress_path, shelf, None)
    except RuntimeError:
        pass
    progress = ReadingProgress(progress_path)
    assert progress.resume("https://book/1")["chapter_url"] == "c1"
    progress.save("src", "https://book/1", "novel", "c2", "Two")
    assert ReadingProgress(progress_path).resume("https://book/1")["chapter_url"] == "c2"
    assert not progress_path.with_suffix(progress_path.suffix + ".bak").exists()
