import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from framework.content import Chapter, Detail
from framework.library_store import LibraryStore
from framework.reading_progress import ReadingProgress
from framework.shelf_service import ShelfService


BOOK = "https://example.test/book"


def test_corrupt_library_fails_closed_without_overwrite(tmp_path):
    path = tmp_path / "library.json"
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    store = LibraryStore(path)
    assert store.load_failed
    assert store.reading_status(BOOK) == "unknown"
    assert store.history_hidden(BOOK)
    assert store.playback_history_hidden({"book_url": BOOK})
    assert store.search_history_hidden("keyword")
    assert store.add("demo", BOOK, "书") == {}
    assert path.read_bytes() == before


def test_corrupt_library_blocks_progress_and_legal_empty_library_allows(tmp_path):
    from gui.app import MainWindow

    bad = tmp_path / "library.json"
    bad.write_text("{broken", encoding="utf-8")
    store = LibraryStore(bad)
    progress = ReadingProgress(tmp_path / "progress.json")
    progress.save("demo", BOOK, "video", "ep", "chapter", title="secret",
                  location={"seconds": 5, "duration": 100})
    # 隐私状态不可判定：隐藏相关历史、阻止写进度
    assert progress.recent_videos(store) == []
    assert not MainWindow._local_progress_allowed(SimpleNamespace(library_store=store), BOOK)
    # 合法空库：既非加载失败，也未隐藏/拦截
    empty = LibraryStore(tmp_path / "empty.json")
    assert not empty.load_failed
    assert empty.list_all() == []
    assert MainWindow._local_progress_allowed(SimpleNamespace(library_store=empty), BOOK)
    shown = progress.recent_videos(empty)
    assert shown and shown[0]["book_url"] == BOOK


@pytest.mark.parametrize("raw", [{}, {"reading_status": "invalid"}, {"reading_status": None}])
def test_legacy_favorite_defaults_to_reading(tmp_path, raw):
    path = tmp_path / "library.json"
    path.write_text(json.dumps({"favorites": {BOOK: {"url": BOOK, **raw}}}), encoding="utf-8")
    store = LibraryStore(path)
    assert store.reading_status(BOOK) == "reading"
    assert store.get(BOOK)["reading_status"] == "reading"
    assert store.list_all()[0]["reading_status"] == "reading"


def test_status_switch_preserves_favorite_and_metadata(tmp_path):
    path = tmp_path / "library.json"
    store = LibraryStore(path)
    original = store.add("demo", BOOK, "书", "video", folder="收藏夹")
    assert original["reading_status"] == "reading"
    assert store.set_reading_status(BOOK, "completed")
    record = store.get(BOOK)
    assert record == {**original, "reading_status": "completed"}
    restored = LibraryStore(path)
    assert restored.reading_status(BOOK) == "completed"
    assert restored.has(BOOK) and restored.count() == 1
    restored.add("demo", BOOK, "更新标题", "video", folder="收藏夹")
    assert restored.reading_status(BOOK) == "completed"
    assert restored.set_reading_status(BOOK, "reading")
    assert LibraryStore(path).reading_status(BOOK) == "reading"
    assert not restored.set_reading_status("missing", "completed")
    with pytest.raises(ValueError):
        restored.set_reading_status(BOOK, "other")
    assert restored.reading_status(BOOK) == "reading"


def test_status_failed_save_rolls_back(tmp_path, monkeypatch):
    store = LibraryStore(tmp_path / "library.json")
    store.add("demo", BOOK, "书")
    monkeypatch.setattr(store, "_save", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        store.set_reading_status(BOOK, "completed")
    assert store.reading_status(BOOK) == "reading"
    assert LibraryStore(store._path).reading_status(BOOK) == "reading"


def test_concurrent_metadata_updates_preserve_completed_status(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = LibraryStore(tmp_path / "library.json")
    store.add("demo", BOOK, "书")
    store.set_reading_status(BOOK, "completed")
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda index: store.add("demo", BOOK, f"书{index}"), range(12)))
    assert all(record["reading_status"] == "completed" for record in results)
    assert store.reading_status(BOOK) == "completed"
    assert LibraryStore(store._path).reading_status(BOOK) == "completed"
    assert store.count() == 1


def test_shelf_merged_local_retains_favorite_status(tmp_path):
    folder = tmp_path / "downloads" / "视频"
    folder.mkdir(parents=True)
    (folder / "1.mp4").write_bytes(b"video")
    store = LibraryStore(tmp_path / "library.json")
    store.add("demo", BOOK, "视频", "video")
    service = ShelfService(tmp_path / "downloads", library_store=store, data_dir=tmp_path / "data")
    assert service.favorite_set_reading_status(BOOK, "completed")
    assert service.favorites()[0].to_rec()["reading_status"] == "completed"
    items = service.list_items()
    assert len(items) == 1
    assert items[0].kind == "local" and items[0].online
    assert items[0].reading_status == "completed"
    assert store.has(BOOK)


@pytest.mark.parametrize("status,label", [("reading", "在看"), ("completed", "已看完")])
def test_card_and_ui_merge_status(_qapp, status, label):
    from gui.pages.library_page import LibraryPage, _ShelfCard

    favorite = {"kind": "favorite", "url": BOOK, "source_id": "demo", "reading_status": status}
    local = {"kind": "local", "url": BOOK, "source_id": "demo", "path": "local", "reading_status": "reading"}
    merged, remaining = LibraryPage._deduplicate_visible_books([local], [favorite])
    assert not remaining
    assert merged[0]["reading_status"] == status
    assert merged[0]["online"]
    card = _ShelfCard(merged[0])
    assert card._reading_status_label.text() == label
    plain = _ShelfCard({"kind": "local", "title": "未收藏"})
    assert not hasattr(plain, "_reading_status_label")
    card.deleteLater()
    plain.deleteLater()
    _qapp.processEvents()


def test_status_menu_switches_exclusively_and_refreshes(_qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QMenu
    from gui.pages.library_page import LibraryPage

    store = LibraryStore(tmp_path / "library.json")
    store.add("demo", BOOK, "书", "video")
    service = ShelfService(tmp_path / "downloads", library_store=store, data_dir=tmp_path / "data")
    monkeypatch.setattr(LibraryPage, "_rebuild", lambda self: None)
    page = LibraryPage(library_store=store, shelf_service=service)
    rebuild = Mock()
    monkeypatch.setattr(page, "_rebuild", rebuild)
    menus = []

    class TestMenu(QMenu):
        def exec(self, pos):
            menus.append(self)

    monkeypatch.setattr("PySide6.QtWidgets.QMenu", TestMenu)
    rec = service.favorites()[0].to_rec()
    page._show_card_menu(rec, QPoint())
    actions = {action.text(): action for action in menus[0].actions()}
    assert actions["在看"].isChecked()
    assert not actions["已看完"].isChecked()
    assert actions["在看"].actionGroup().isExclusive()
    actions["已看完"].trigger()
    assert not actions["在看"].isChecked()
    assert actions["已看完"].isChecked()
    assert store.reading_status(BOOK) == "completed" and store.has(BOOK)
    rebuild.assert_called_once()
    page._show_card_menu(rec, QPoint())
    actions = {action.text(): action for action in menus[1].actions()}
    assert actions["已看完"].isChecked()
    actions["在看"].trigger()
    assert store.reading_status(BOOK) == "reading" and store.has(BOOK)
    assert rebuild.call_count == 2
    page.deleteLater()
    _qapp.processEvents()


@pytest.fixture
def reader_state(_qapp, tmp_path):
    from gui.pages.reader_page import ReaderPage

    store = LibraryStore(tmp_path / "library.json")
    store.add("demo", BOOK, "书")
    progress = ReadingProgress(tmp_path / "progress.json", shelf_cb=store.has)
    source = SimpleNamespace(source_id="demo", source_name="测试", raw={})
    reader = ReaderPage(SimpleNamespace(get=lambda sid: source), SimpleNamespace(), reading_progress=progress)
    reader.set_favorite_checker(store.has)
    reader.set_reading_status_checker(store.reading_status)
    yield reader, store, progress
    reader.deleteLater()
    _qapp.processEvents()


@pytest.mark.parametrize("ctype", ["novel", "comic", "video"])
def test_completed_open_does_not_resume_and_reading_restores(reader_state, monkeypatch, ctype):
    from PySide6.QtCore import QThreadPool

    reader, store, progress = reader_state
    progress.save("demo", BOOK, ctype, "episode2", "第二章", position=0.42, location={"seconds": 42, "duration": 100})
    before = progress.resume(BOOK)
    monkeypatch.setattr(QThreadPool, "globalInstance", lambda: SimpleNamespace(start=lambda task: None))
    store.set_reading_status(BOOK, "completed")
    reader.open("demo", BOOK, ctype)
    assert reader._current_start_url == ""
    assert reader._pending_position is None
    assert reader._pending_location is None
    assert progress.resume(BOOK) == before
    assert store.has(BOOK)
    store.set_reading_status(BOOK, "reading")
    reader.open("demo", BOOK, ctype)
    assert reader._current_start_url == "episode2"
    assert reader._pending_position == 0.42
    assert reader._pending_location == before["location"]


@pytest.mark.parametrize("ctype", ["novel", "comic", "video"])
def test_completed_signal_never_calls_save_then_reading_writes(reader_state, monkeypatch, ctype):
    import framework.external_player as player

    reader, store, progress = reader_state
    detail_url = "https://example.test/switched" if ctype == "video" else BOOK
    detail = Detail(source_id="demo", content_type=ctype, url=detail_url, title="书")
    session = {}
    monkeypatch.setattr(player, "player_session", lambda: session)
    location = {"seconds": 50, "duration": 100, "chapter_url": "episode2", "session_id": id(session), "library_url": BOOK}
    payload = (detail, "第二章", "episode2", 0.5, 2, location)
    save = Mock(wraps=progress.save)
    monkeypatch.setattr(progress, "save", save)
    store.set_reading_status(BOOK, "completed")
    reader._on_progress_signal(payload)
    if ctype == "video":
        reader._on_video_handoff(detail, BOOK, "第二章", "episode2")
    save.assert_not_called()
    assert store.has(BOOK)
    store.set_reading_status(BOOK, "reading")
    reader._on_progress_signal(payload)
    save.assert_called_once()
    assert progress.resume(BOOK)["position"] == 0.5


def test_completed_video_options_drop_lookup_and_pending_resume(reader_state, monkeypatch):
    import framework.external_player as player

    reader, store, progress = reader_state
    detail = Detail(source_id="demo", content_type="video", url=BOOK, title="书", chapters=[Chapter("第二集", "ep2")])
    view = reader.video_view
    view._detail = detail
    view._episodes = detail.chapters
    view._current_idx = 0
    view.library_url = BOOK
    progress.save("demo", BOOK, "video", "ep2", "第二集", location={"seconds": 60, "duration": 100})
    view._restore_episode_url = "ep2"
    view._pending_location = {"seconds": 70, "duration": 100}
    view._pending_position = 0.7
    monkeypatch.setattr(player, "flush_player_progress", lambda: None)
    store.set_reading_status(BOOK, "completed")
    options = view._progress_options()
    assert options["resume_positions"] == {0: {}}
    save = Mock(wraps=progress.save)
    monkeypatch.setattr(progress, "save", save)
    options["on_handoff"]()
    save.assert_not_called()
    store.set_reading_status(BOOK, "reading")
    assert view._progress_options()["resume_positions"][0]["seconds"] == 60
    options["on_handoff"]()
    save.assert_called_once()


def test_local_completed_blocks_resume_progress_and_handoff(_qapp, tmp_path, monkeypatch):
    from gui.app import MainWindow
    import framework.external_player as player

    store = LibraryStore(tmp_path / "library.json")
    store.add("demo", BOOK, "视频", "video")
    progress = ReadingProgress(tmp_path / "progress.json")
    key, episode = "D:/videos", "D:/videos/1.mp4"
    progress.save("", key, "video", episode, "1.mp4", location={"seconds": 60, "duration": 100}, library_url=BOOK)
    before = progress.resume(key)
    session = {}
    monkeypatch.setattr(player, "player_session", lambda: session)
    monkeypatch.setattr(player, "flush_player_progress", lambda: None)
    calls = []
    monkeypatch.setattr(player, "open_with_player", lambda target, **kw: calls.append((target, kw)))
    window = SimpleNamespace(reading_progress=progress, library_store=store)
    window._on_local_progress = lambda payload: MainWindow._on_local_progress(window, payload)
    record = {"key": key, "url": BOOK, "episode_paths": [episode], "title": "视频"}
    save = Mock(wraps=progress.save)
    monkeypatch.setattr(progress, "save", save)
    store.set_reading_status(BOOK, "completed")
    MainWindow._play_local_video(window, record)
    assert calls[0][1]["resume_positions"] == {0: {}}
    calls[0][1]["on_handoff"]()
    calls[0][1]["on_progress"](0, {"seconds": 80, "duration": 100, "session_id": id(session)})
    save.assert_not_called()
    assert progress.resume(key) == before
    assert store.has(BOOK)
    store.set_reading_status(BOOK, "reading")
    MainWindow._play_local_video(window, record)
    assert calls[1][1]["resume_positions"][0]["seconds"] == 60
    calls[0][1]["on_progress"](0, {"seconds": 80, "duration": 100, "session_id": id(session)})
    assert progress.resume(key)["location"]["seconds"] == 80
    calls[1][1]["on_handoff"]()
    assert save.call_count == 2


def test_local_completed_starts_selection_at_first_episode(_qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QComboBox, QDialog, QMainWindow
    from gui.app import MainWindow

    store = LibraryStore(tmp_path / "library.json")
    store.add("demo", BOOK, "视频", "video")
    progress = ReadingProgress(tmp_path / "progress.json")
    progress.save("demo", BOOK, "video", "ep2", "第2集")
    selections = []

    class TestDialog(QDialog):
        def exec(self):
            selections.append(self.findChild(QComboBox).currentIndex())
            return QDialog.Rejected

    monkeypatch.setattr("PySide6.QtWidgets.QDialog", TestDialog)
    window = QMainWindow()
    window.library_store = store
    window.reading_progress = progress
    record = {"key": "D:/videos", "url": BOOK, "episode_paths": ["D:/videos/第1集.mp4", "D:/videos/第2集.mp4"]}
    store.set_reading_status(BOOK, "completed")
    MainWindow._play_local_video(window, record)
    store.set_reading_status(BOOK, "reading")
    MainWindow._play_local_video(window, record)
    assert selections == [0, 1]
    assert progress.resume(BOOK)["chapter_title"] == "第2集"
    window.deleteLater()
    _qapp.processEvents()


def test_uncollected_local_video_keeps_progress_behavior(_qapp, tmp_path, monkeypatch):
    from gui.app import MainWindow
    import framework.external_player as player

    store = LibraryStore(tmp_path / "library.json")
    progress = ReadingProgress(tmp_path / "progress.json")
    session = {}
    monkeypatch.setattr(player, "player_session", lambda: session)
    payload = {
        "key": "D:/videos", "chapter_url": "D:/videos/1.mp4", "chapter_title": "1.mp4",
        "title": "视频", "library_url": "", "folder": "",
        "location": {"seconds": 50, "duration": 100, "session_id": id(session)},
    }
    window = SimpleNamespace(reading_progress=progress, library_store=store)
    MainWindow._on_local_progress(window, payload)
    assert progress.resume(payload["key"])["position"] == 0.5


def test_uncollected_online_video_keeps_progress_and_resume(reader_state, monkeypatch):
    import framework.external_player as player

    reader, store, progress = reader_state
    store.remove(BOOK)
    detail = Detail(source_id="demo", content_type="video", url=BOOK, title="视频")
    session = {}
    monkeypatch.setattr(player, "player_session", lambda: session)
    location = {"seconds": 50, "duration": 100, "session_id": id(session), "chapter_url": "episode"}
    reader._on_progress_signal((detail, "第一集", "episode", 0.5, None, location))
    assert reader._resume_progress(BOOK)["position"] == 0.5
    assert not store.has(BOOK)
