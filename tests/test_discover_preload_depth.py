from framework.settings_manager import SettingsManager
from gui.pages.discover_page import DiscoverPage


class _EmptySourceManager:
    def discoverable_sources(self):
        return []


def test_discover_page_accepts_settings(_qapp, tmp_path):
    sm = SettingsManager(tmp_path / "app_config.json")
    page = DiscoverPage(
        source_manager=_EmptySourceManager(),
        discovery=None,
        content=None,
        bulk_fetch=None,
        event_bus=None,
        theme_manager=None,
        settings=sm,
    )
    assert page.settings is sm


class _FakeBar:
    def __init__(self, maximum):
        self._maximum = maximum

    def maximum(self):
        return self._maximum

    def value(self):
        return 0

    def setValue(self, value):
        pass


class _FakeViewport:
    def height(self):
        return 600

    def width(self):
        return 1200


class _FakeScroll:
    def __init__(self, vbar_maximum=0):
        self._bar = _FakeBar(vbar_maximum)

    def verticalScrollBar(self):
        return self._bar

    def viewport(self):
        return _FakeViewport()

    def height(self):
        return 600


def _page(ahead=2, concurrency=3, vbar_maximum=0):
    p = DiscoverPage.__new__(DiscoverPage)
    p.settings = None
    p._preload_ahead = ahead
    p._preload_concurrency = concurrency
    p._active_pages = set()
    p._loaded_pages = set()
    p._has_more = True
    p._current_page = 0
    p._source_epoch = 1
    p._current_source = None
    p._current_cat_url = "https://cat"
    p.scroll = _FakeScroll(vbar_maximum)
    p.dispatched = []
    p._load_next_page = lambda page=-1: p.dispatched.append(page)
    return p


def test_preload_pages_default_is_five():
    from framework.settings_manager import DEFAULTS

    assert DEFAULTS["discover"]["preload_pages"] == 5
    assert DEFAULTS["discover"]["preload_concurrency"] == 3


def test_discover_page_reads_clamped_settings(_qapp, tmp_path):
    sm = SettingsManager(tmp_path / "app_config.json")
    sm.set("discover", "preload_pages", 99)
    sm.set("discover", "preload_concurrency", 0)
    page = DiscoverPage(
        source_manager=_EmptySourceManager(),
        discovery=None,
        content=None,
        bulk_fetch=None,
        event_bus=None,
        theme_manager=None,
        settings=sm,
    )
    assert page._preload_ahead == 10
    assert page._preload_concurrency == 1


def test_apply_preload_settings_sets_both_values():
    p = _page()
    p.apply_preload_settings(7, 4)
    assert p._preload_ahead == 7
    assert p._preload_concurrency == 4


def test_apply_preload_settings_clamps_negative():
    p = _page()
    p.apply_preload_settings(-3, -1)
    assert p._preload_ahead == 0
    assert p._preload_concurrency == 1


def test_maybe_preload_dispatches_next_page_when_viewport_not_full():
    p = _page(ahead=5, vbar_maximum=0)
    p._loaded_pages = {1, 2, 3, 4, 5}
    p._maybe_preload()
    assert p.dispatched == [6]


def test_maybe_preload_stops_at_configured_depth():
    p = _page(ahead=5, vbar_maximum=0)
    p._loaded_pages = {1, 2, 3, 4, 5, 6}
    p._maybe_preload()
    assert p.dispatched == []


def test_maybe_preload_stops_when_viewport_full():
    p = _page(ahead=5, vbar_maximum=100000)
    p._loaded_pages = {1}
    p._maybe_preload()
    assert p.dispatched == []


def _pump_page(ahead=5, concurrency=3, active=None, pending=None):
    p = _page(ahead=ahead, concurrency=concurrency)
    p._active_pages = set(active or ())
    p._preload_pending = list(pending or ())
    p.dispatched = []

    def _load(page=-1):
        p.dispatched.append(page)
        p._active_pages.add(page)

    p._load_next_page = _load
    return p


def test_pump_respects_inflight_cap():
    p = _pump_page(active={1, 2, 3}, pending=[4, 5, 6])
    p._pump_preload()
    assert p.dispatched == []


def test_pump_fills_up_to_cap():
    p = _pump_page(active={1}, pending=[2, 3, 4, 5, 6])
    p._pump_preload()
    assert p.dispatched == [2, 3]
    assert p._preload_pending == [4, 5, 6]


def test_pump_concurrency_one_is_serial():
    p = _pump_page(concurrency=1, active={1}, pending=[2, 3, 4])
    p._pump_preload()
    assert p.dispatched == []
    p._active_pages = set()
    p._pump_preload()
    assert p.dispatched == [2]
    assert p._preload_pending == [3, 4]


def test_pump_empty_pending_is_noop():
    p = _pump_page(pending=[])
    p._pump_preload()
    assert p.dispatched == []


def test_pump_guards_against_unregistered_page():
    p = _page(ahead=5, concurrency=2)
    p._active_pages = set()
    p._preload_pending = [2, 3, 4]
    p._load_next_page = lambda page=-1: None
    p._pump_preload()
    assert p._preload_pending == []
    assert p.dispatched == []


def test_on_page_loaded_pumps_more():
    p = _page(ahead=5, concurrency=3)
    p._active_pages = {1, 2, 3}
    p._loaded_pages = {1}
    p._preload_pending = [4, 5, 6]
    p.dispatched = []

    def _load(page=-1):
        p.dispatched.append(page)
        p._active_pages.add(page)

    p._load_next_page = _load
    p._on_page_loaded([], "boom", 1, 1)
    assert p.dispatched == [4]
