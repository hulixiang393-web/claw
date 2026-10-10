import json
from types import SimpleNamespace

import pytest

import framework.external_player as player
from framework.library_store import LibraryStore
from framework.reading_progress import ReadingProgress


@pytest.fixture
def history_clock(monkeypatch):
    import framework.reading_progress as module

    clock = [1800000000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    return clock


def test_search_ttl_capacity_and_refresh(tmp_path, history_clock):
    from framework.search_history import SearchHistory

    path = tmp_path / "search.json"
    history = SearchHistory(path)
    for index in range(21):
        history.push(str(index))
    assert history.recent() == [str(index) for index in range(20, 0, -1)]
    history_clock[0] += 1
    history.push("1")
    assert len(history.recent()) == 20
    assert history.recent()[0] == "1"
    assert json.loads(path.read_text())[0]["timestamp"] == history_clock[0]
    history_clock[0] += 604799
    assert history.recent() == ["1"]
    history_clock[0] += 1
    assert history.recent() == []
    assert json.loads(path.read_text()) == []


def test_search_load_cleans_and_writes_capacity(tmp_path, history_clock):
    from framework.search_history import SearchHistory

    path = tmp_path / "search.json"
    now = history_clock[0]
    records = [{"keyword": str(i), "timestamp": now - i} for i in range(25)]
    records += [records[0], "legacy"]
    records += [{"keyword": "bad", "timestamp": value} for value in
                (None, True, "bad", float("nan"), float("inf"), now + 1, now - 604800)]
    path.write_text(json.dumps(records))
    history = SearchHistory(path)
    assert history.recent() == [str(i) for i in range(20)]
    assert len(json.loads(path.read_text())) == 20
    assert SearchHistory(path).recent() == history.recent()


@pytest.mark.parametrize("repository_enabled", [False, True])
def test_video_ttl_disk_resume_and_repository(tmp_path, history_clock, repository_enabled):
    from framework.shelf_cache_repository import ShelfCacheRepository

    repo = ShelfCacheRepository(tmp_path / "cache.db", tmp_path / "content") if repository_enabled else None
    path = tmp_path / "progress.json"
    progress = ReadingProgress(path, shelf_cb=lambda _: True, repository=repo)
    progress.save("demo", "book", "video", "ep", "secret chapter", title="secret title",
                  library_url="private-book", folder="private", detail_url="route",
                  location={"seconds": 123, "duration": 900})
    if repo:
        repo.upsert_book("book", "demo", "book", "video", progress.resume("book"))
    history_clock[0] += 604799
    assert len(progress.recent_videos()) == 1
    history_clock[0] += 1
    assert progress.recent_videos() == []
    disk = json.loads(path.read_text())
    assert "title" not in disk["book"]
    assert "updated_at" not in disk["book"]
    assert "history_timestamp" not in disk["book"]
    restored = ReadingProgress(path, repository=repo)
    assert restored.resume("book")["location"] == {"seconds": 123, "duration": 900}
    assert restored.resume("book")["detail_url"] == "route"
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("private", locked=True)
    store.add("demo", "private-book", "secret title", "video", folder="private")
    assert store.playback_history_hidden(restored.resume("book"))
    if repo:
        snapshot = repo.get_book_snapshot("book")
        assert "title" not in snapshot["metadata"]
        assert "updated_at" not in snapshot["location"]
        fallback = ReadingProgress(tmp_path / "missing.json", repository=repo)
        assert fallback.resume("book")["location"]["seconds"] == 123
        assert fallback.recent_videos() == []
        repo.close()


@pytest.mark.parametrize("timestamp", [None, True, "bad", float("nan"), float("inf"), 1800000001])
def test_video_bad_timestamp_preserves_only_resume(tmp_path, history_clock, timestamp):
    path = tmp_path / "progress.json"
    path.write_text(json.dumps({"book": {"book_url": "book", "content_type": "video",
        "chapter_url": "ep", "title": "expired", "history_timestamp": timestamp,
        "location": {"seconds": 42, "duration": 100, "title": "nested"}}}))
    progress = ReadingProgress(path)
    assert progress.recent_videos() == []
    assert progress.resume("book")["location"] == {"seconds": 42, "duration": 100}
    assert "expired" not in path.read_text()
    assert "nested" not in path.read_text()


def test_video_migration_twice_does_not_revive_history(tmp_path, history_clock):
    from framework.shelf_cache_repository import ShelfCacheRepository
    from framework.shelf_cache_migration import migrate_legacy_data

    path = tmp_path / "progress.json"
    record = {"book_url": "book", "source_id": "demo", "content_type": "video",
              "chapter_url": "ep", "title": "expired", "updated_at": "2020-01-01T00:00:00",
              "location": {"seconds": 42, "duration": 100}}
    path.write_text(json.dumps({"book": record}))
    backup = path.with_suffix(".json.bak")
    backup.write_text(json.dumps({"book": record}))
    repo = ShelfCacheRepository(tmp_path / "cache.db", tmp_path / "content")
    repo.upsert_book("book", "demo", "book", "video", record)
    repo.update_location("book", record)
    for _ in range(2):
        migrate_legacy_data(repo, path, SimpleNamespace(), None)
        progress = ReadingProgress(path, repository=repo)
        assert progress.recent_videos() == []
        assert progress.resume("book")["location"]["seconds"] == 42
        assert "expired" not in json.dumps(repo.get_book_snapshot("book"))
    assert "expired" not in backup.read_text()
    assert json.loads(backup.read_text())["book"]["location"]["seconds"] == 42
    repo.close()


def test_migration_creates_sanitized_expired_backup(tmp_path, history_clock):
    from framework.shelf_cache_repository import ShelfCacheRepository
    from framework.shelf_cache_migration import migrate_legacy_data

    path = tmp_path / "progress.json"
    path.write_text(json.dumps({"book": {
        "source_id": "demo", "book_url": "book", "content_type": "video",
        "chapter_url": "ep", "title": "expired", "chapter_title": "old chapter",
        "updated_at": "2020-01-01T00:00:00", "library_url": "private-book",
        "folder": "private", "detail_url": "route",
        "location": {"seconds": 42, "duration": 100, "title": "nested"}}}))
    repo = ShelfCacheRepository(tmp_path / "cache.db", tmp_path / "content")
    migrate_legacy_data(repo, path, SimpleNamespace(), None)
    backup = path.with_suffix(".json.bak")
    cleaned = json.loads(backup.read_text())["book"]
    assert cleaned == ReadingProgress(path).resume("book")
    assert "title" not in cleaned and "updated_at" not in cleaned
    assert cleaned["location"] == {"seconds": 42, "duration": 100}
    assert cleaned["library_url"] == "private-book"
    assert cleaned["folder"] == "private"
    assert cleaned["detail_url"] == "route"
    repo.close()


@pytest.mark.parametrize("lifecycle", ["startup", "prune", "migration"])
def test_backup_expires_at_boundary_and_stays_idempotent(tmp_path, history_clock, lifecycle):
    from framework.shelf_cache_repository import ShelfCacheRepository
    from framework.shelf_cache_migration import migrate_legacy_data

    path = tmp_path / "progress.json"
    progress = ReadingProgress(path)
    progress.save("demo", "book", "video", "ep", "chapter", title="title",
                  location={"seconds": 42, "duration": 100})
    backup = path.with_suffix(".json.bak")
    backup.write_bytes(path.read_bytes())
    unrelated = tmp_path / "other.json.bak"
    unrelated.write_text("unrelated")
    original = backup.read_bytes()
    repo = ShelfCacheRepository(tmp_path / "cache.db", tmp_path / "content") if lifecycle == "migration" else None

    def run():
        if lifecycle == "startup":
            return ReadingProgress(path)
        if lifecycle == "prune":
            progress.prune()
        else:
            migrate_legacy_data(repo, path, SimpleNamespace(), None)

    history_clock[0] += 604799
    run()
    assert backup.read_bytes() == original
    history_clock[0] += 1
    run()
    cleaned_bytes = backup.read_bytes()
    cleaned = json.loads(cleaned_bytes)["book"]
    assert "title" not in cleaned and "history_timestamp" not in cleaned
    assert cleaned["location"]["seconds"] == 42
    run()
    assert backup.read_bytes() == cleaned_bytes
    assert unrelated.read_text() == "unrelated"
    assert ReadingProgress(backup).resume("book")["location"]["seconds"] == 42
    assert ReadingProgress(backup).recent_videos() == []
    if repo:
        repo.close()


@pytest.mark.parametrize("payload", [b"broken JSON", b"[]", b'{"book": "invalid"}', b"\xff"])
@pytest.mark.parametrize("primary_exists", [False, True])
def test_corrupt_backup_is_ignored_not_overwritten(tmp_path, payload, primary_exists, caplog):
    from framework.shelf_cache_repository import ShelfCacheRepository
    from framework.shelf_cache_migration import migrate_legacy_data
    from gui.app import _make_reading_progress

    path = tmp_path / "progress.json"
    record = {"book_url": "book", "source_id": "demo", "content_type": "novel",
              "chapter_url": "chapter", "position": 0.42}
    if primary_exists:
        path.write_text(json.dumps({"book": record}), encoding="utf-8")
    original = path.read_bytes() if primary_exists else None
    backup = path.with_suffix(".json.bak")
    backup.write_bytes(payload)
    repo = ShelfCacheRepository(tmp_path / "cache.db", tmp_path / "content")
    try:
        progress = _make_reading_progress(path, lambda _: True, repo)
        assert progress.resume("book") == (record if primary_exists else None)
        migrate_legacy_data(repo, path, SimpleNamespace(), None)
        if primary_exists:
            assert repo.get_book_snapshot("book")["location"]["position"] == 0.42
            assert path.read_bytes() == original
        else:
            assert not path.exists()
        assert backup.read_bytes() == payload
        assert str(tmp_path) not in caplog.text
        assert "broken JSON" not in caplog.text
    finally:
        repo.close()


@pytest.mark.parametrize("backup_payload", [b"{}", b"broken backup"])
def test_corrupt_primary_keeps_existing_load_and_migration_semantics(tmp_path, backup_payload):
    from framework.shelf_cache_migration import migrate_legacy_data

    path = tmp_path / "progress.json"
    payload = b"broken primary"
    path.write_bytes(payload)
    backup = path.with_suffix(".json.bak")
    backup.write_bytes(backup_payload)
    assert ReadingProgress(path).resume("book") is None
    with pytest.raises(json.JSONDecodeError):
        migrate_legacy_data(None, path, SimpleNamespace(), None)
    assert path.read_bytes() == payload
    assert backup.read_bytes() == backup_payload


@pytest.mark.parametrize("existing", [False, True])
def test_backup_write_failure_preserves_original_and_retries(tmp_path, history_clock, monkeypatch, existing):
    import framework.reading_progress as module
    from framework.shelf_cache_repository import ShelfCacheRepository
    from framework.shelf_cache_migration import migrate_legacy_data

    path = tmp_path / "progress.json"
    progress = ReadingProgress(path)
    progress.save("demo", "book", "video", "ep", "chapter", title="title",
                  location={"seconds": 42, "duration": 100})
    backup = path.with_suffix(".json.bak")
    original = path.read_bytes()
    if existing:
        backup.write_bytes(original)
    history_clock[0] += 604800
    replace = module.os.replace

    def fail_backup_replace(source, destination):
        if destination == backup:
            raise PermissionError("backup write denied")
        return replace(source, destination)

    repo = ShelfCacheRepository(tmp_path / "cache.db", tmp_path / "content")
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", fail_backup_replace)
        with pytest.raises(PermissionError, match="backup write denied"):
            migrate_legacy_data(repo, path, SimpleNamespace(), None)
        if existing:
            with pytest.raises(PermissionError, match="backup write denied"):
                progress.prune()
            assert backup.read_bytes() == original
        else:
            assert not backup.exists()
    assert list(tmp_path.glob("*.tmp")) == []
    migrate_legacy_data(repo, path, SimpleNamespace(), None)
    assert "title" not in json.loads(backup.read_text())["book"]
    repo.close()


def test_backup_read_permission_failure_is_reported(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "progress.json"
    backup = path.with_suffix(".json.bak")
    backup.write_text("{}")
    read_text = Path.read_text

    def denied(target, *args, **kwargs):
        if target == backup:
            raise PermissionError("backup read denied")
        return read_text(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", denied)
        with pytest.raises(PermissionError, match="backup read denied"):
            ReadingProgress(path)
    assert backup.read_text() == "{}"


def test_expired_title_not_reused_by_new_playback(tmp_path, history_clock):
    progress = ReadingProgress(tmp_path / "progress.json")
    progress.save("demo", "book", "video", "ep", "chapter", title="expired title",
                  location={"seconds": 42, "duration": 100})
    history_clock[0] += 604800
    progress.save("demo", "book", "video", "ep", "chapter")
    assert progress.resume("book")["title"] == ""
    assert progress.resume("book")["location"]["seconds"] == 42
    assert progress.recent_videos()[0]["history_timestamp"] == history_clock[0]


def test_cleanup_atomic_failure_is_reported(tmp_path, history_clock, monkeypatch):
    import framework.reading_progress as module
    from framework.search_history import SearchHistory

    path = tmp_path / "search.json"
    history = SearchHistory(path)
    history.push("word")
    previous = path.read_bytes()
    progress = ReadingProgress(tmp_path / "progress.json")
    progress.save("demo", "book", "video", "ep", "chapter", title="title")
    previous_progress = progress.path.read_bytes()
    history_clock[0] += 604800

    def fail_replace(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        history.recent()
    with pytest.raises(OSError, match="replace failed"):
        progress.recent_videos()
    assert path.read_bytes() == previous
    assert progress.path.read_bytes() == previous_progress
    assert list(tmp_path.glob("*.tmp")) == []


def test_repository_legacy_split_location_survives_restart(tmp_path, history_clock):
    from framework.shelf_cache_repository import ShelfCacheRepository

    database, content = tmp_path / "cache.db", tmp_path / "content"
    repo = ShelfCacheRepository(database, content)
    repo.upsert_book("book", "demo", "book", "video", {
        "source_id": "demo", "book_url": "book", "content_type": "video",
        "chapter_url": "ep", "title": "expired", "updated_at": "2020-01-01T00:00:00",
        "detail_url": "route", "library_url": "private-book"})
    repo.update_location("book", {"seconds": 42, "duration": 100})
    repo.close()
    repo = ShelfCacheRepository(database, content)
    progress = ReadingProgress(tmp_path / "missing.json", repository=repo)
    record = progress.resume("book")
    assert record["chapter_url"] == "ep"
    assert record["detail_url"] == "route"
    assert record["library_url"] == "private-book"
    assert record["location"]["seconds"] == 42
    assert "title" not in record
    assert progress.recent_videos() == []
    repo.close()


def test_home_rechecks_expired_snapshot(tmp_path, history_clock):
    from gui.pages.home_page import HomePage
    from gui.app import MainWindow
    from framework.search_history import SearchHistory

    progress = ReadingProgress(tmp_path / "progress.json")
    progress.save("demo", "book", "video", "ep", "chapter", title="title")
    record = progress.recent_videos()[0]
    search = SearchHistory(tmp_path / "search.json")
    search.push("word")
    opened = []
    home = SimpleNamespace(_reading_progress=progress, _privacy=None, _history=search,
                           playback_requested=SimpleNamespace(emit=opened.append),
                           search_requested=SimpleNamespace(emit=opened.append))
    history_clock[0] += 604800
    HomePage._on_playback_clicked(home, SimpleNamespace(data=lambda role: record))
    HomePage._on_recent_clicked(home, "word")
    MainWindow._resume_playback(SimpleNamespace(reading_progress=progress), record)
    assert opened == []
    assert "title" not in json.loads(progress.path.read_text())["book"]


@pytest.mark.parametrize("mode", ["failed", "timeout", "too_long"])
def test_seek_recovery_is_bounded(mode, monkeypatch):
    state = {"port": 1}
    proc = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(player, "_control_state", state)
    monkeypatch.setattr(player, "_last_proc", proc)
    clock = [100.0]
    monkeypatch.setattr(player.time, "monotonic", lambda: clock[0])
    status = {"item_id": 7, "seconds": 0, "duration": 900, "state": "playing"}
    monkeypatch.setattr(player, "player_status", lambda **kw: dict(status))
    monkeypatch.setattr(player, "player_playlist_items", lambda **kw: [{"id": 7, "uri": "file:///D:/a.mp4"}])
    commands, writes = [], []
    monkeypatch.setattr(player, "player_command", lambda *a, **kw: commands.append(a) or mode != "failed")
    monitor = player.PlaybackMonitor(state, proc, [(0, "D:/a.mp4")], lambda i, loc: writes.append(loc), {0: {"seconds": 1000 if mode == "too_long" else 120}})
    monitor.tick()
    assert writes == []
    clock[0] += 1
    status["seconds"] = 1
    monitor.tick()
    assert writes == []
    status["seconds"] = 0
    clock[0] += 30
    monitor.tick()
    assert writes == []
    status["seconds"] = 31
    monitor.tick()
    assert writes and writes[-1]["seconds"] == 31
    monitor.tick()
    assert len(commands) == (0 if mode == "too_long" else 1)


def test_local_browser_handoff_preserves_privacy_and_precision(_qapp, tmp_path, monkeypatch):
    from gui.app import MainWindow

    monkeypatch.setattr(player, "_locate_vlc", lambda: None)
    monkeypatch.setattr(player.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(player, "_progress_monitor", None)
    monkeypatch.setattr(player, "_last_proc", None)
    monkeypatch.setattr(player, "_control_state", None)
    progress = ReadingProgress(tmp_path / "progress.json")
    window = SimpleNamespace(reading_progress=progress)
    record = {"key": "D:/videos", "episode_paths": ["D:/videos/1.mp4"], "title": "本地作品", "url": "book"}
    MainWindow._play_local_video(window, record)
    assert progress.resume("D:/videos")["location"] is None
    progress.save("", "D:/videos", "video", "D:/videos/1.mp4", "第一集", location={"seconds": 123, "duration": 900})
    MainWindow._play_local_video(window, record)
    assert progress.resume("D:/videos")["location"]["seconds"] == 123
    MainWindow._play_local_video(window, {**record, "episode_paths": ["D:/videos/2.mp4"]})
    assert progress.resume("D:/videos")["location"] is None
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("private", locked=True)
    store.add("age", "book", "本地作品", "video", folder="private")
    assert progress.recent_videos(store) == []


def test_local_history_resolves_later_unique_shelf_match(tmp_path):
    from gui.app import MainWindow

    store = LibraryStore(tmp_path / "library.json")
    progress = ReadingProgress(tmp_path / "progress.json")
    local = tmp_path / "episode.mp4"
    local.touch()
    progress.save("", str(tmp_path), "video", str(local), "第一集", title="测试 （作品）")
    record = progress.recent_videos(store)[0]
    store.create_folder("private", locked=True)
    store.add("age", "https://example.test/detail/1", "测试(作品)", "video", folder="private")
    opened = []
    window = SimpleNamespace(_ensure_library_store=lambda: store, _play_local_video=opened.append)
    MainWindow._resume_playback(window, record)
    assert opened == []
    assert progress.recent_videos(store) == []
    from gui.pages.home_page import HomePage
    home = SimpleNamespace(_privacy=store, playback_requested=SimpleNamespace(emit=opened.append))
    HomePage._on_playback_clicked(home, SimpleNamespace(data=lambda role: record))
    assert opened == []
    store.add("other", "https://example.test/detail/2", "测试(作品)", "video")
    assert len(progress.recent_videos(store)) == 1
    assert not store.playback_history_hidden({**record, "title": "未知作品"})


def test_switched_line_keeps_library_identity(_qapp, tmp_path, monkeypatch):
    from framework.content import Detail, Chapter
    from gui.pages.reader_page import ReaderPage
    from gui.app import MainWindow
    from PySide6.QtCore import QThreadPool

    root = "https://example.test/detail/1"
    route = root + "?sid=2"
    progress = ReadingProgress(tmp_path / "progress.json")
    source = SimpleNamespace(source_id="age", source_name="AGE", raw={})
    reader = ReaderPage(SimpleNamespace(get=lambda sid: source), SimpleNamespace(), reading_progress=progress)
    tasks = []
    monkeypatch.setattr(QThreadPool, "globalInstance", lambda: SimpleNamespace(start=tasks.append))
    reader.open("age", root, "video")
    original = Detail(source_id="age", url=root, title="作品", content_type="video", chapters=[Chapter("第一集", "ep1")])
    reader._on_detail(original, None, "video", "", "age")
    reader._on_source_changed((original, "2"))
    detail = Detail(source_id="age", url=route, title="作品", content_type="video", chapters=[Chapter("第二集", "https://example.test/play/1?sid=2&ep=2")])
    task = tasks[-1]
    task.signals.done.emit(detail, task)
    reader.video_view._current_idx = 0
    options = reader.video_view._progress_options()
    state = {"port": 1}
    monkeypatch.setattr(player, "_control_state", state)
    options["on_progress"](0, {"seconds": 123, "duration": 900, "session_id": id(state)})
    record = progress.resume(root)
    assert record is not None
    assert record["library_url"] == root
    assert record["detail_url"] == route
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("private", locked=True)
    store.add("age", root, "作品", "video", folder="private")
    assert progress.recent_videos(store) == []
    opened = []
    MainWindow._resume_playback(SimpleNamespace(_ensure_library_store=lambda: store, _open_online_from_shelf=opened.append), record)
    assert opened == []
    store.clear_folder_lock("private")
    MainWindow._resume_playback(SimpleNamespace(_ensure_library_store=lambda: store, _open_online_from_shelf=opened.append), record)
    assert opened == [("age", root, "video")]
    tasks = []
    monkeypatch.setattr(QThreadPool, "globalInstance", lambda: SimpleNamespace(start=tasks.append))
    reader.open("age", root, "video")
    assert tasks[-1]._url == route
    assert reader._current_book_url == root
    selected = []
    monkeypatch.setattr(reader.video_view, "_load_episode", selected.append)
    reader._on_detail(detail, None, "video", detail.chapters[0].url, "age")
    assert selected == [0]
    assert reader.video_view.library_url == root
    reader.deleteLater()
    _qapp.processEvents()


def test_browser_handoff_retains_only_matching_episode_progress(_qapp, tmp_path, monkeypatch):
    from framework.content import Detail, Chapter
    from gui.pages.reader_page import ReaderPage

    monkeypatch.setattr(player, "_locate_vlc", lambda: None)
    monkeypatch.setattr(player.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(player, "_progress_monitor", None)
    monkeypatch.setattr(player, "_last_proc", None)
    monkeypatch.setattr(player, "_control_state", None)
    progress = ReadingProgress(tmp_path / "progress.json")
    reader = ReaderPage(SimpleNamespace(), SimpleNamespace(), reading_progress=progress)
    detail = Detail(source_id="age", url="book", title="作品", content_type="video", chapters=[Chapter("第一集", "ep1"), Chapter("第二集", "ep2")])
    view = reader.video_view
    view._detail, view._episodes, view._current_idx = detail, detail.chapters, 0
    player.open_with_player("https://example.test/a.mp4", **view._progress_options())
    assert progress.resume("book") is not None
    assert progress.resume("book")["location"] is None
    progress.save("age", "book", "video", "ep1", "第一集", location={"seconds": 123, "duration": 900})
    player.open_with_player("https://example.test/a.mp4", **view._progress_options())
    assert progress.resume("book")["location"]["seconds"] == 123
    view._current_idx = 1
    player.open_with_player("https://example.test/b.mp4", **view._progress_options())
    assert progress.resume("book")["chapter_url"] == "ep2"
    assert progress.resume("book")["location"] is None
    assert progress.resume("book")["position"] is None
    monkeypatch.setattr(player.webbrowser, "open", lambda url: False)
    view._current_idx = 0
    player.open_with_player("https://example.test/a.mp4", **view._progress_options())
    assert progress.resume("book")["chapter_url"] == "ep2"
    reader.deleteLater()
    _qapp.processEvents()
