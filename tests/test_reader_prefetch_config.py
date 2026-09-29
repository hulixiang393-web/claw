def _sm(tmp_path):
    from framework.settings_manager import SettingsManager
    return SettingsManager(tmp_path / "app_config.json")


def test_defaults(tmp_path):
    from framework.settings_manager import DEFAULTS

    assert DEFAULTS["reader"] == {
        "prefetch_enabled": True,
        "prefetch_ahead": 3,
        "prefetch_behind": 1,
    }
    assert DEFAULTS["discover"] == {"preload_pages": 5, "preload_concurrency": 3}
    assert DEFAULTS["shelf_cache"] == {"max_book_mb": 256, "max_total_mb": 8192}


def test_reader_prefetch_settings_defaults(tmp_path):
    from framework.settings_manager import reader_prefetch_settings

    assert reader_prefetch_settings(_sm(tmp_path)) == {
        "enabled": True,
        "ahead": 3,
        "behind": 1,
    }


def test_reader_prefetch_settings_reads_values(tmp_path):
    from framework.settings_manager import reader_prefetch_settings

    sm = _sm(tmp_path)
    sm.set("reader", "prefetch_ahead", 7)
    sm.set("reader", "prefetch_behind", 2)
    sm.set("reader", "prefetch_enabled", False)

    assert reader_prefetch_settings(sm) == {"enabled": False, "ahead": 7, "behind": 2}


def test_reader_prefetch_settings_clamps_out_of_range(tmp_path):
    from framework.settings_manager import reader_prefetch_settings

    sm = _sm(tmp_path)
    sm.set("reader", "prefetch_ahead", 999)
    sm.set("reader", "prefetch_behind", -5)

    got = reader_prefetch_settings(sm)
    assert got["ahead"] == 20
    assert got["behind"] == 0


def test_discover_preload_settings_clamps(tmp_path):
    from framework.settings_manager import discover_preload_settings

    sm = _sm(tmp_path)
    sm.set("discover", "preload_pages", 99)
    sm.set("discover", "preload_concurrency", 0)

    assert discover_preload_settings(sm) == {"pages": 10, "concurrency": 1}


def test_shelf_cache_settings_clamps(tmp_path):
    from framework.settings_manager import shelf_cache_settings

    sm = _sm(tmp_path)
    assert shelf_cache_settings(sm) == {"max_book_mb": 256, "max_total_mb": 8192}
    sm.set("shelf_cache", "max_total_mb", 999999)
    sm.set("shelf_cache", "max_book_mb", 1)

    got = shelf_cache_settings(sm)
    assert got["max_total_mb"] == 131072
    assert got["max_book_mb"] == 16


def test_reader_page_accepts_settings(_qapp, tmp_path):
    from framework.settings_manager import SettingsManager
    from gui.pages.reader_page import ReaderPage

    sm = SettingsManager(tmp_path / "app_config.json")
    page = ReaderPage(source_manager=None, content=None, settings=sm)

    assert page.settings is sm


def test_settings_page_reader_tab_loads_and_saves(tmp_path, _qapp, monkeypatch):
    from framework.settings_manager import SettingsManager
    from gui.pages.settings_page import SettingsPage

    monkeypatch.setattr("framework.cache_service.get_shelf_cache", lambda: None)
    monkeypatch.setattr("framework.cache_service.get_search_cache", lambda: None)

    sm = SettingsManager(tmp_path / "app_config.json")

    class _ThemeManager:
        def switch_to(self, _theme):
            pass

    page = SettingsPage(sm, _ThemeManager())

    assert page.tabs.count() == 8
    assert page._reader_prefetch_enabled.isChecked() is True
    assert page._reader_prefetch_ahead.value() == 3
    assert page._reader_prefetch_behind.value() == 1

    page._reader_prefetch_enabled.setChecked(False)
    page._reader_prefetch_ahead.setValue(8)
    page._reader_prefetch_behind.setValue(2)
    page._on_apply()

    reloaded = SettingsManager(tmp_path / "app_config.json")
    assert reloaded.get("reader", "prefetch_enabled") is False
    assert reloaded.get("reader", "prefetch_ahead") == 8
    assert reloaded.get("reader", "prefetch_behind") == 2


class _Ch:
    def __init__(self, url):
        self.url = url
        self.title = url
        self._cached_text = None


class _Src:
    source_id = "s1"
    base_url = "https://s1.example"


class _MemCache:
    def __init__(self):
        self.d = {}

    def get(self, key):
        return self.d.get(key)

    def set(self, key, value, ttl=None):
        self.d[key] = value


def _comic(ahead=3, behind=1, n=20):
    from gui.pages.reader.comic_view import ComicView

    v = ComicView.__new__(ComicView)
    v._source = object()
    v._chapters = [_Ch(f"u{i}") for i in range(n)]
    v._prefetch_count = ahead
    v._prefetch_back = behind
    v._prefetch_queue = []
    v._prefetch_busy = False
    v._prefetched = {}
    v._current_idx = 3
    v._content = None
    v._gen = 0
    v._cancel_evt = None
    v._prefetch_task = None
    return v


def test_comic_back_default_is_now_one():
    from gui.pages.reader import comic_view

    assert comic_view.PREFETCH_COUNT == 3
    assert comic_view.PREFETCH_BACK == 1


def test_comic_prefetch_window_size_follows_setting():
    v = _comic(ahead=4)
    v._prefetch_future(3, v._prefetch_count)
    assert v._prefetch_queue == ["u5", "u6", "u7"]


def test_comic_prefetch_zero_does_nothing():
    v = _comic(ahead=0, behind=0)
    v._prefetch_future(3, v._prefetch_count)
    assert v._prefetch_queue == []


def test_comic_prefetch_window_clamps_at_end():
    v = _comic(ahead=10, n=20)
    v._current_idx = 17
    v._prefetch_future(17, v._prefetch_count)
    assert v._prefetch_queue == ["u19"]


def test_comic_set_prefetch_config_zero_disables():
    v = _comic(ahead=5, behind=4)
    v.set_prefetch_config(False, 5, 4)
    assert (v._prefetch_count, v._prefetch_back) == (0, 0)


def test_comic_set_prefetch_config_sets_values():
    v = _comic()
    v.set_prefetch_config(True, 6, 2)
    assert (v._prefetch_count, v._prefetch_back) == (6, 2)


def test_novel_back_window_respects_behind():
    from gui.pages.reader.novel_view import NovelView

    assert NovelView._prefetch_back_queue(20, 5, behind=3) == [4, 3, 2]


def test_novel_back_window_behind_one_is_single_chapter():
    from gui.pages.reader.novel_view import NovelView

    assert NovelView._prefetch_back_queue(20, 5, behind=1) == [4]


def test_novel_back_window_behind_zero_is_empty():
    from gui.pages.reader.novel_view import NovelView

    assert NovelView._prefetch_back_queue(20, 5, behind=0) == []


def test_novel_back_window_clamps_at_zero():
    from gui.pages.reader.novel_view import NovelView

    assert NovelView._prefetch_back_queue(3, 1, behind=5) == [0]


def test_novel_prefetch_config_zero_disables():
    from gui.pages.reader.novel_view import NovelView

    v = NovelView.__new__(NovelView)
    v._prefetch_ahead, v._prefetch_behind = 3, 1
    v.set_prefetch_config(False, 5, 4)
    assert (v._prefetch_ahead, v._prefetch_behind) == (0, 0)


def test_novel_prefetch_config_clamps_invalid_values():
    from gui.pages.reader.novel_view import NovelView

    v = NovelView.__new__(NovelView)
    v.set_prefetch_config(True, "bad", -4)
    assert (v._prefetch_ahead, v._prefetch_behind) == (3, 0)


def test_novel_prefetch_config_sets_values():
    from gui.pages.reader.novel_view import NovelView

    v = NovelView.__new__(NovelView)
    v._prefetch_ahead, v._prefetch_behind = 3, 1
    v.set_prefetch_config(True, 8, 2)
    assert (v._prefetch_ahead, v._prefetch_behind) == (8, 2)


def test_novel_prefetch_pumps_one_at_a_time(_qapp, monkeypatch):
    from gui.pages.reader.novel_view import NovelView

    started = []

    class _Signal:
        def connect(self, callback):
            self.callback = callback

    class _Task:
        def __init__(self, content, source, chapter):
            started.append(chapter.url)
            self.signals = type("_Signals", (), {"finished": _Signal()})()

    class _Pool:
        def start(self, task):
            pass

    monkeypatch.setattr("gui.pages.reader.novel_view._LoadChapterTask", _Task)
    monkeypatch.setattr(
        "gui.pages.reader.novel_view.QThreadPool.globalInstance",
        staticmethod(lambda: _Pool()),
    )

    v = NovelView.__new__(NovelView)
    v._source = object()
    v._content = None
    v._chapters = [_Ch(f"u{i}") for i in range(10)]
    v._next_prefetch_queue = [4, 5, 6]
    v._prefetch_idx = -2
    v._prefetch_task = None
    v._pump_next_prefetch()

    assert started == ["u4"]
    assert v._next_prefetch_queue == [5, 6]
    assert v._prefetch_idx == 4


def test_precache_chapters_respects_ahead():
    fetched = []
    from framework.content import Content

    c = Content.__new__(Content)
    c._cache = _MemCache()
    c._repository = None
    c.fetch_chapter = lambda src, url: (fetched.append(url), f"正文 {url}")[1]
    c._abs_url = lambda src, url: url

    c.precache_chapters(_Src(), [_Ch(f"u{i}") for i in range(10)], 2, ahead=3)

    assert fetched == ["u2", "u3", "u4", "u5"]


def test_precache_chapters_disabled_is_noop():
    fetched = []
    from framework.content import Content

    c = Content.__new__(Content)
    c._cache = _MemCache()
    c._repository = None
    c.fetch_chapter = lambda src, url: fetched.append(url) or "text"
    c._abs_url = lambda src, url: url

    c.precache_chapters(_Src(), [_Ch(f"u{i}") for i in range(10)], 2,
                        ahead=3, enabled=False)

    assert fetched == []


def test_precache_chapters_ahead_zero_keeps_current_chapter():
    fetched = []
    from framework.content import Content

    c = Content.__new__(Content)
    c._cache = _MemCache()
    c._repository = None
    c.fetch_chapter = lambda src, url: (fetched.append(url), f"正文 {url}")[1]
    c._abs_url = lambda src, url: url

    c.precache_chapters(_Src(), [_Ch(f"u{i}") for i in range(10)], 2, ahead=0)

    assert fetched == ["u2"]


def test_precache_chapters_clamps_at_end():
    fetched = []
    from framework.content import Content

    c = Content.__new__(Content)
    c._cache = _MemCache()
    c._repository = None
    c.fetch_chapter = lambda src, url: (fetched.append(url), f"正文 {url}")[1]
    c._abs_url = lambda src, url: url

    c.precache_chapters(_Src(), [_Ch(f"u{i}") for i in range(3)], 2, ahead=9)

    assert fetched == ["u2"]


def test_precache_task_passes_settings(monkeypatch):
    from gui.pages.reader_page import _PrecacheTask

    calls = []
    content = type("Content", (), {})()
    task = _PrecacheTask(content, _Src(), [_Ch("u0")], 0, ahead=6, enabled=False)
    task._content.precache_chapters = lambda *args, **kwargs: calls.append((args, kwargs))
    task.run()

    assert calls[0][1] == {"ahead": 6, "enabled": False}


def test_reader_page_start_precache_reads_settings(monkeypatch):
    from gui.pages.reader_page import ReaderPage

    class _Settings:
        pass

    class _Pool:
        def start(self, task):
            captured.append(task)

    captured = []
    page = ReaderPage.__new__(ReaderPage)
    page._content = type("Content", (), {"_cache": object()})()
    page._manager = type("Manager", (), {"get": lambda self, sid: _Src()})()
    page._current_start_url = ""
    page.settings = _Settings()
    monkeypatch.setattr(
        "gui.pages.reader_page.reader_prefetch_settings",
        lambda settings: {"ahead": 6, "enabled": False, "behind": 2},
    )
    monkeypatch.setattr(
        "gui.pages.reader_page.QThreadPool.globalInstance",
        staticmethod(lambda: _Pool()),
    )

    detail = type("Detail", (), {"chapters": [_Ch("u0")]})()
    page._start_precache("s1", detail)

    assert captured[0]._ahead == 6
    assert captured[0]._enabled is False


def test_reader_page_injects_view_prefetch_settings(monkeypatch):
    from gui.pages.reader_page import ReaderPage

    calls = []
    page = ReaderPage.__new__(ReaderPage)
    page.settings = object()
    page._current_source_id = "s1"
    page._current_book_url = "book"
    page.video_view = type("Video", (), {"stop_playback": lambda self: None})()
    page.stack = type("Stack", (), {"setCurrentWidget": lambda self, view: None})()
    page.novel_view = type("View", (), {
        "load": lambda self, *args, **kwargs: None,
        "set_prefetch_config": lambda self, *args: calls.append(args),
    })()
    page.comic_view = page.novel_view
    page._pending_position = page._pending_page = page._pending_location = None
    page._current_detail = None
    page.title_label = type("Label", (), {"setText": lambda self, text: None})()
    page.refresh_favorite_state = lambda: None
    page._start_precache = lambda *args: None
    page._manager = type("Manager", (), {"get": lambda self, sid: _Src()})()
    monkeypatch.setattr(
        "gui.pages.reader_page.reader_prefetch_settings",
        lambda settings: {"ahead": 6, "enabled": False, "behind": 2},
    )

    detail = type("Detail", (), {"url": "book", "title": "Book"})()
    page._on_detail(detail, None, "novel", "", "s1")

    assert calls == [(False, 6, 2)]


def test_novel_prefetch_done_keeps_idle_sentinel_and_pumps_next():

    from gui.pages.reader.novel_view import NovelView

    v = NovelView.__new__(NovelView)
    v._next_prefetch_queue = [2]
    v._prefetch_idx = 4
    calls = []
    v._pump_next_prefetch = lambda: calls.append(True)
    ch = _Ch("u1")

    v._on_prefetch_done(ch, "text", None)

    assert v._prefetch_idx == -2
    assert ch._cached_text == "text"
    assert calls == [True]
