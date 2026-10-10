import json
from types import SimpleNamespace

import pytest

from framework.library_store import LibraryStore
from framework.reading_progress import ReadingProgress
from framework.search_history import SearchHistory
import framework.external_player as player


def test_private_history_dynamic_lock(tmp_path):
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("private")
    store.add("demo", "https://example.test/book", "秘密作品", "video", folder="private")
    history = SearchHistory(tmp_path / "search.json", privacy=store)
    history.push("秘密作品")
    history.push("普通搜索")
    progress = ReadingProgress(tmp_path / "progress.json")
    progress.save("demo", "https://example.test/book", "video", "episode", "第一集", title="秘密作品")
    assert len(progress.recent_videos(store)) == 1
    store.set_folder_lock("private", True)
    assert history.recent() == ["普通搜索"]
    assert progress.recent_videos(store) == []
    history.push("秘密作品", context_url="https://example.test/book")
    assert history.recent() == ["普通搜索"]
    assert store.history_hidden("https://example.test/book")
    assert LibraryStore(tmp_path / "library.json").history_hidden("https://example.test/book")
    history.push("上下文泛词", context_url="https://example.test/book")
    assert "上下文泛词" not in history.recent()
    history.push("泛词")
    assert "泛词" in history.recent()
    assert not store.search_history_hidden("作品")


def test_uncollected_video_survives_prune_and_restart(tmp_path):
    path = tmp_path / "progress.json"
    progress = ReadingProgress(path)
    progress.save("demo", "book", "video", "ep", "第一集", title="作品", location={"seconds": 123, "duration": 900})
    data = json.loads(path.read_text(encoding="utf-8"))
    data["book"]["updated_at"] = "2020-01-01T00:00:00"
    data["book"].pop("history_timestamp")
    path.write_text(json.dumps(data), encoding="utf-8")
    restored = ReadingProgress(path)
    restored.prune()
    assert restored.resume("book")["location"]["seconds"] == 123
    assert restored.recent_videos() == []
    assert "title" not in json.loads(path.read_text(encoding="utf-8"))["book"]


def test_monitor_seeks_once_only_correct_ready_item(monkeypatch):
    state = {"port": 1234, "password": "test"}
    proc = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(player, "_control_state", state)
    monkeypatch.setattr(player, "_last_proc", proc)
    writes, seeks = [], []
    monitor = player.PlaybackMonitor(state, proc, [(0, "file:///video.mp4")], lambda i, p: writes.append((i, p)), {0: {"seconds": 123}})
    monkeypatch.setattr(player, "player_status", lambda **kw: {"item_id": 7, "seconds": 0, "duration": 900, "state": "opening"})
    monkeypatch.setattr(player, "player_playlist_items", lambda **kw: [{"id": 7, "uri": "file:///wrong.mp4"}])
    monkeypatch.setattr(player, "player_command", lambda *a, **kw: seeks.append((a, kw)) or True)
    monitor.tick()
    assert writes == seeks == []
    monkeypatch.setattr(player, "player_status", lambda **kw: {"item_id": 7, "seconds": 30, "duration": 900, "state": "playing"})
    monitor.tick()
    assert writes == seeks == []
    monkeypatch.setattr(player, "player_playlist_items", lambda **kw: [{"id": 7, "uri": "file:///video.mp4"}])
    monkeypatch.setattr(player, "player_status", lambda **kw: {"item_id": 7, "seconds": 0, "duration": 900, "state": "playing"})
    monitor.tick()
    monitor.tick()
    assert len(seeks) == 1
    assert writes == []
    monkeypatch.setattr(player, "player_status", lambda **kw: {"item_id": 7, "seconds": 124, "duration": 900, "state": "playing"})
    monitor.tick()
    assert writes[0][1]["seconds"] == 124
    monkeypatch.setattr(player, "_control_state", dict(state))
    monitor.tick()
    assert len(writes) == 1


def test_monitor_flushes_last_valid_sample_on_exit(monkeypatch):
    state = {"port": 1}
    running = [True]
    proc = SimpleNamespace(poll=lambda: None if running[0] else 0)
    monkeypatch.setattr(player, "_control_state", state)
    monkeypatch.setattr(player, "_last_proc", proc)
    status = {"item_id": 7, "seconds": 60, "duration": 900, "state": "playing"}
    monkeypatch.setattr(player, "player_status", lambda **kw: dict(status))
    monkeypatch.setattr(player, "player_playlist_items", lambda **kw: [{"id": 7, "uri": "https://example.test/ep"}])
    saved = []
    monitor = player.PlaybackMonitor(state, proc, [(0, "https://example.test/ep")], lambda idx, location: saved.append(location))
    monitor.tick()
    status["seconds"] = 61
    monitor.tick()
    assert [x["seconds"] for x in saved] == [60]
    running[0] = False
    monitor.stop()
    assert [x["seconds"] for x in saved] == [60, 61]
    monitor.stop()
    assert len(saved) == 2


def test_local_privacy_association(tmp_path):
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("private", locked=True)
    store.add("demo", "book", "秘密作品", "video", folder="private")
    progress = ReadingProgress(tmp_path / "progress.json")
    progress.save("", "D:/videos", "video", "D:/videos/a.mp4", "a", title="本地", library_url="book")
    assert progress.recent_videos(store) == []
    assert not store.history_hidden("D:/unknown")


@pytest.fixture
def vlc_http(monkeypatch):
    import base64
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlsplit

    data = {"state": "playing", "time": 0, "length": 900, "currentplid": 7}
    commands = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.headers["Authorization"] == "Basic " + base64.b64encode(b":secret").decode()
            parsed = urlsplit(self.path)
            if parsed.path.endswith("playlist.json"):
                payload = {"children": [{"id": "7", "uri": "file:///D:/video.mp4"}]}
            elif parsed.path.endswith("status.xml"):
                query = parse_qs(parsed.query)
                commands.append(query)
                data["time"] = int(query["val"][0])
                payload = {}
            else:
                payload = data
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = {"port": server.server_port, "password": "secret"}
    proc = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(player, "_control_state", state)
    monkeypatch.setattr(player, "_last_proc", proc)
    monkeypatch.setattr(player, "_playlist_items", [])
    yield state, proc, data, commands
    server.shutdown()
    server.server_close()
    thread.join()


def test_http_status_resume_and_persistence(vlc_http, tmp_path):
    state, proc, data, commands = vlc_http
    progress = ReadingProgress(tmp_path / "progress.json")
    progress.save("", "D:/videos", "video", "D:/video.mp4", "第一集", location={"seconds": 120, "duration": 900})
    restored = ReadingProgress(progress.path)

    def save(idx, location):
        restored.save("", "D:/videos", "video", "D:/video.mp4", "第一集", location=location)

    monitor = player.PlaybackMonitor(state, proc, [(0, "D:/video.mp4")], save, {0: restored.resume("D:/videos")["location"]})
    assert player.player_status()["seconds"] == 0
    monitor.tick()
    monitor.tick()
    monitor.tick()
    assert len(commands) == 1
    assert ReadingProgress(progress.path).resume("D:/videos")["location"]["seconds"] == 120
    data["time"] = 130
    data["state"] = "paused"
    monitor.tick()
    assert ReadingProgress(progress.path).resume("D:/videos")["location"]["seconds"] == 130
    data["length"] = 0
    assert player.player_status() is None
    data["length"] = 900
    data["time"] = float("nan")
    assert player.player_status() is None
    data["time"] = 0
    data["state"] = "stopped"
    assert player.player_status() is None


def test_offscreen_home_filters_counts_and_reopen(_qapp, tmp_path):
    from framework.events import EventBus
    from framework.settings_manager import SettingsManager
    from framework.source_manager import SourceManager
    from framework.theme_manager import ThemeManager
    from gui.pages.home_page import HomePage

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("private")
    store.add("demo", "book", "秘密作品", "video", folder="private")
    progress = ReadingProgress(tmp_path / "progress.json")
    progress.save("demo", "book", "video", "ep", "第一集", title="秘密作品", location={"seconds": 65, "duration": 900})
    history = SearchHistory(tmp_path / "search.json", privacy=store)
    history.push("秘密作品")
    settings = SettingsManager(tmp_path / "settings.json")
    manager = SourceManager(tmp_path / "sources", health_file=tmp_path / "health.json")
    home = HomePage(manager, EventBus(), ThemeManager(settings), settings, history, reading_progress=progress, privacy=store)
    opened = []
    home.playback_requested.connect(opened.append)
    assert home.playback_list.count() == 1
    assert "01:05" in home.playback_list.item(0).text()
    store.set_folder_lock("private", True)
    store._unlocked_folders = {"private"}
    home._on_playback_clicked(home.playback_list.item(0))
    assert opened == []
    home.refresh()
    assert home.playback_list.count() == 0
    assert home.playback_title.text() == "最近播放（0）"
    assert history.recent() == []
    home.deleteLater()
    _qapp.processEvents()


def test_local_video_app_saves_and_restores(_qapp, tmp_path, monkeypatch):
    from gui.app import MainWindow

    progress = ReadingProgress(tmp_path / "progress.json")
    state = {"port": 2}
    monkeypatch.setattr(player, "_control_state", state)
    calls = []
    monkeypatch.setattr(player, "open_with_player", lambda target, **kw: calls.append((target, kw)))
    window = SimpleNamespace(reading_progress=progress)
    window._on_local_progress = lambda payload: MainWindow._on_local_progress(window, payload)
    record = {"key": "D:/videos", "episode_paths": ["D:/videos/1.mp4"], "title": "本地视频", "url": "book", "folder": "private"}
    MainWindow._play_local_video(window, record)
    assert progress.resume("D:/videos") is None
    calls[0][1]["on_progress"](0, {"seconds": 321, "duration": 900, "session_id": id(state)})
    assert ReadingProgress(progress.path).resume("D:/videos")["library_url"] == "book"
    window.reading_progress = ReadingProgress(progress.path)
    MainWindow._play_local_video(window, record)
    assert calls[1][1]["resume_positions"][0]["seconds"] == 321
    calls[1][1]["on_progress"](0, {"seconds": 0, "duration": 900, "session_id": id(state)})
    assert window.reading_progress.resume("D:/videos")["location"]["seconds"] == 321


def test_search_records_once_for_multiple_sources(_qapp, tmp_path, monkeypatch):
    from framework.config import SourceConfig
    from framework.source_manager import SourceManager
    from gui.pages.search_page import SearchPage
    import gui.pages.search_page as search_page

    manager = SourceManager(tmp_path / "sources", health_file=tmp_path / "health.json")
    for sid in ("site_a", "site_b"):
        manager.add(SourceConfig.from_dict({
            "$schema_version": 2, "$id": sid, "$name": sid, "$type": "video",
            "transports": {"base_url": "https://example.test"},
            "endpoints": {"search": {"base_url": "/search", "method": "GET"}},
        }))
    page = SearchPage(manager, SimpleNamespace())
    monkeypatch.setattr(search_page, "QThreadPool", SimpleNamespace(globalInstance=lambda: SimpleNamespace(start=lambda task: None)))
    history = SearchHistory(tmp_path / "search.json")
    writes = []
    monkeypatch.setattr(history, "_save", lambda: writes.append(history.recent()))
    page.search_clicked.connect(history.push)
    page._all_selected = True
    page.keyword_input.setText("普通搜索")
    page._on_search()
    assert history.recent() == ["普通搜索"]
    assert len(writes) == 1
    page.deleteLater()
    _qapp.processEvents()


def test_reader_restarts_uncollected_video_at_saved_episode(_qapp, tmp_path, monkeypatch):
    from framework.content import Detail, Chapter
    from gui.pages.reader_page import ReaderPage
    import gui.pages.reader_page as reader_module

    progress = ReadingProgress(tmp_path / "progress.json")
    progress.save("demo", "book", "video", "ep2", "第二集", location={"seconds": 123, "duration": 900})
    source = SimpleNamespace(source_id="demo", source_name="站点", raw={})
    manager = SimpleNamespace(get=lambda sid: source)
    reader = ReaderPage(manager, SimpleNamespace(), reading_progress=ReadingProgress(progress.path))
    reader.set_favorite_checker(lambda url: False)
    monkeypatch.setattr(reader_module, "QThreadPool", SimpleNamespace(globalInstance=lambda: SimpleNamespace(start=lambda task: None)))
    from PySide6.QtCore import QThreadPool
    monkeypatch.setattr(QThreadPool, "globalInstance", lambda: SimpleNamespace(start=lambda task: None))
    selected = []
    monkeypatch.setattr(reader.video_view, "_load_episode", selected.append)
    reader.open("demo", "book", "video")
    assert reader._current_start_url == "ep2"
    detail = Detail(source_id="demo", url="book", title="作品", content_type="video", chapters=[Chapter("第一集", "ep1"), Chapter("第二集", "ep2")])
    reader._on_detail(detail, None, "video", "ep2", "demo")
    assert selected == [1]
    assert reader.video_view._progress_options()["resume_positions"][1]["seconds"] == 123
    reader.deleteLater()
    _qapp.processEvents()


def test_offscreen_video_progress_uncollected_and_old_session(_qapp, tmp_path, monkeypatch):
    from framework.content import Detail, Chapter
    from gui.pages.reader_page import ReaderPage

    progress = ReadingProgress(tmp_path / "progress.json")
    reader = ReaderPage(SimpleNamespace(), SimpleNamespace(), reading_progress=progress)
    reader.set_favorite_checker(lambda url: False)
    detail = Detail(source_id="demo", url="book", title="作品", content_type="video", chapters=[Chapter("第一集", "ep")])
    reader.video_view._detail = detail
    reader.video_view._episodes = detail.chapters
    reader.video_view._current_idx = 0
    options = reader.video_view._progress_options()
    state = {"port": 1}
    monkeypatch.setattr(player, "_control_state", state)
    options["on_progress"](0, {"seconds": 123, "duration": 900, "session_id": id(state)})
    assert ReadingProgress(progress.path).resume("book")["location"]["seconds"] == 123
    reader.video_view._detail = Detail(source_id="demo", url="other", title="另一部", content_type="video")
    options["on_progress"](0, {"seconds": 124, "duration": 900, "session_id": id(state)})
    assert progress.resume("other") is None
    assert progress.resume("book")["location"]["seconds"] == 124
    monkeypatch.setattr(player, "_control_state", dict(state))
    options["on_progress"](0, {"seconds": 200, "duration": 900, "session_id": id(state)})
    assert progress.resume("book")["location"]["seconds"] == 124
    reader.deleteLater()
    _qapp.processEvents()
