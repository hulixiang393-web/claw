import json
from types import SimpleNamespace

import pytest

from framework.library_store import LibraryStore
from framework.reading_progress import ReadingProgress
from framework.search_history import SearchHistory


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


def test_local_privacy_association(tmp_path):
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("private", locked=True)
    store.add("demo", "book", "秘密作品", "video", folder="private")
    progress = ReadingProgress(tmp_path / "progress.json")
    progress.save("", "D:/videos", "video", "D:/videos/a.mp4", "a", title="本地", library_url="book")
    assert progress.recent_videos(store) == []
    assert not store.history_hidden("D:/unknown")




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
    """本地视频：进度落盘 → 再次打开续播到该秒数；0 秒不覆盖已存进度。"""
    import framework.player_session as _ps
    from gui.app import MainWindow

    progress = ReadingProgress(tmp_path / "progress.json")
    calls = []

    class _Sig:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def emit(self, *args):
            for slot in self.slots:
                slot(*args)

    class _FakePlayer:
        """播放后端桩：拉起的 URI / 起播秒数、IPC 时间与状态信号。"""

        def __init__(self, *a, **kw):
            self.time_changed = _Sig()
            self.length_changed = _Sig()
            self.state_changed = _Sig()
            self.time = 0
            self.length = 0
            calls.append(self)

        def play(self, url, audio_url="", title="", kind="",
                 network_caching_ms=0, start_s=0):
            self.url = url
            self.start = start_s
            self.state_changed.emit(3)      # 起播成功 → handoff 落盘

        def get_length(self):
            return self.length

        def get_time(self):
            return self.time

        def close(self):
            pass

    monkeypatch.setattr(_ps, "PlayerSession", _FakePlayer)
    window = SimpleNamespace(reading_progress=progress)
    window._on_local_progress = lambda payload: MainWindow._on_local_progress(window, payload)
    record = {"key": "D:/videos", "episode_paths": ["D:/videos/1.mp4"], "title": "本地视频", "url": "book", "folder": "private"}
    MainWindow._play_local_video(window, record)
    # 还没播到任何位置：不得凭空产生进度
    assert progress.resume("D:/videos")["location"] is None
    calls[0].length = 900_000
    calls[0].time = 321_000
    calls[0].time_changed.emit(321_000)
    assert ReadingProgress(progress.path).resume("D:/videos")["library_url"] == "book"
    window.reading_progress = ReadingProgress(progress.path)
    MainWindow._play_local_video(window, record)
    assert calls[1].start == 321
    calls[1].length = 900_000
    calls[1].time = 0
    calls[1].time_changed.emit(0)
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
    """内嵌播放的进度回写：不问会话、按作品归属、陈旧外播会话仍被拒。"""
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
    # 内嵌播放器：进度自带 engine=embedded，无需外播会话
    options["on_progress"](0, {"seconds": 123, "duration": 900})
    assert ReadingProgress(progress.path).resume("book")["location"]["seconds"] == 123
    # 换作品后同一回调不得写到上一部
    reader.video_view._detail = Detail(source_id="demo", url="other", title="另一部", content_type="video")
    options["on_progress"](0, {"seconds": 124, "duration": 900})
    assert progress.resume("other") is None
    assert progress.resume("book")["location"]["seconds"] == 124
    # 外部播放器路径的「陈旧会话」守卫仍在：会话不匹配的迟到回写必须丢弃
    stale = {"seconds": 200, "duration": 900, "chapter_url": "ep"}
    reader._on_progress_signal((detail, "第一集", "ep", 0.5, None, stale))
    assert progress.resume("book")["location"]["seconds"] == 124
    reader.deleteLater()
    _qapp.processEvents()


def test_offscreen_home_playback_rows_stay_full_height(_qapp, tmp_path):
    """最近播放：行高随条数自适应，单行不被压扁（标题 + 删除按钮完整可见）。"""
    from framework.events import EventBus
    from framework.settings_manager import SettingsManager
    from framework.source_manager import SourceManager
    from framework.theme_manager import ThemeManager
    from gui.pages.home_page import HomePage, _PLAYBACK_ROW_HEIGHT

    progress = ReadingProgress(tmp_path / "progress.json")
    for i in range(6):
        progress.save("demo", f"book{i}", "video", "ep", "第一集", title=f"作品{i}", location={"seconds": 30})
    settings = SettingsManager(tmp_path / "settings.json")
    manager = SourceManager(tmp_path / "sources", health_file=tmp_path / "health.json")
    home = HomePage(manager, EventBus(), ThemeManager(settings), settings,
                    SearchHistory(tmp_path / "search.json"), reading_progress=progress)
    home.resize(900, 640)
    home.show()
    _qapp.processEvents()

    lst = home.playback_list
    assert lst.count() == 6
    # 列表高度按条数撑开（6 行），不再固定挤在 180px 里
    assert lst.minimumHeight() >= 6 * _PLAYBACK_ROW_HEIGHT
    for i in range(lst.count()):
        rect = lst.visualRect(lst.indexFromItem(lst.item(i)))
        assert rect.height() == _PLAYBACK_ROW_HEIGHT
        row = lst.itemWidget(lst.item(i))
        assert row.height() == _PLAYBACK_ROW_HEIGHT
        assert row.delete_btn.height() <= row.height()
        assert row.delete_btn.geometry().right() <= row.width()

    # 其他区块仍按自身尺寸显示（整页靠滚动容纳，而不是互相挤压）
    for widget in (home.topbar, home.stats_row, home.recent, home.mini_progress,
                   home.batch_update):
        if widget.isVisible():
            assert widget.height() >= widget.sizeHint().height()
    home.deleteLater()
    _qapp.processEvents()
