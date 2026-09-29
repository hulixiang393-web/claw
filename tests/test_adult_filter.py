import json
from pathlib import Path
from types import SimpleNamespace

from framework.config import SourceConfig
from framework.source_manager import SourceManager
from framework import events


ADULT_IDS = {
    "18j", "18mh", "18mh-novel", "18mh-video", "51cg1", "91pornacomic",
    "acgxmh", "alicesw", "avgood", "eporner", "h-comic", "hanime1",
    "hciyuan", "heavy-r", "ho5ho", "kanav", "pornhub", "rousewu",
    "wnacg", "xasiat", "xhentai85",
}

SOURCES_DIR = Path(__file__).resolve().parent.parent / "sources"


def _cfg(sid, ctype="comic", adult=None):
    data = {
        "$schema_version": 2,
        "$id": sid,
        "$type": ctype,
        "$name": sid.upper(),
        "transports": {"base_url": f"https://{sid}.example"},
        "$metadata": {"homepage": f"https://{sid}.example"},
    }
    if adult is not None:
        data["$metadata"]["adult"] = adult
    return SourceConfig.from_dict(data, f"{sid}.json")


def _mgr(*cfgs):
    m = SourceManager()
    for c in cfgs:
        m.add(c)
    return m


def test_adult_defaults_false_when_field_absent():
    assert _cfg("plain").adult is False


def test_adult_true_parses():
    assert _cfg("spicy", adult=True).adult is True


def test_adult_roundtrips_through_to_dict():
    src = _cfg("spicy", adult=True)
    out = src.to_dict()
    assert out["$metadata"]["adult"] is True
    # 往返后重新解析仍是 True
    again = SourceConfig.from_dict(out, "again.json")
    assert again.adult is True


def test_to_dict_preserves_absent_adult_as_false():
    out = _cfg("plain").to_dict()
    assert out["$metadata"]["adult"] is False


def test_all_hides_adult_when_invisible():
    m = _mgr(_cfg("plain"), _cfg("spicy", adult=True))
    m.set_adult_visible(False)
    assert sorted(s.source_id for s in m.all()) == ["plain"]


def test_all_shows_adult_when_visible():
    m = _mgr(_cfg("plain"), _cfg("spicy", adult=True))
    m.set_adult_visible(False)
    assert sorted(s.source_id for s in m.all()) == ["plain"]

    m.set_adult_visible(True)
    assert sorted(s.source_id for s in m.all()) == ["plain", "spicy"]


def test_by_type_filters_adult():
    m = _mgr(_cfg("plain"), _cfg("spicy", adult=True))
    m.set_adult_visible(False)
    got = sorted(s.source_id for s in m.by_type("comic"))
    assert got == ["plain"]


def test_discoverable_sources_filters_adult_and_preserves_exclusions():
    plain = _cfg("plain")
    plain.raw["endpoints"] = {"discovery": {"url": "/discover"}}

    spicy = _cfg("spicy", adult=True)
    spicy.raw["endpoints"] = {"discovery": {"url": "/discover"}}

    disabled = _cfg("disabled")
    disabled.raw["endpoints"] = {"discovery": {"url": "/discover"}}
    disabled.enabled = False

    no_discovery = _cfg("no-discovery")
    m = _mgr(plain, spicy, disabled, no_discovery)

    m.set_adult_visible(False)
    assert sorted(s.source_id for s in m.discoverable_sources()) == ["plain"]

    m.set_adult_visible(True)
    assert sorted(s.source_id for s in m.discoverable_sources()) == ["plain", "spicy"]


def test_get_does_not_filter():
    """get() 故意不过滤：内部逻辑（健康诊断/已打开阅读器）仍需拿到配置。"""
    m = _mgr(_cfg("spicy", adult=True))
    m.set_adult_visible(False)
    assert m.get("spicy").source_id == "spicy"


def test_is_adult_reads_metadata_without_filtering():
    m = _mgr(_cfg("plain"), _cfg("spicy", adult=True))
    m.set_adult_visible(False)
    assert m.is_adult("spicy") is True
    assert m.is_adult("plain") is False
    assert m.is_adult("nonexistent") is False


def test_set_adult_visible_without_bus_does_not_raise():
    m = _mgr(_cfg("spicy", adult=True))
    # 未注入 EventBus 时调用不得抛异常
    m.set_adult_visible(False)
    assert m.is_adult_visible() is False


def test_set_adult_visible_emits_visibility_event():
    """注入真实 EventBus 后，切换可见性必须广播单参 Event。"""
    from framework.events import EventBus

    m = _mgr(_cfg("spicy", adult=True))
    bus = EventBus()
    seen = []
    bus.subscribe(lambda ev: seen.append(ev))
    m.set_event_bus(bus)

    m.set_adult_visible(False)

    assert len(seen) == 1
    assert seen[0].type == events.EVENT_SOURCE_VISIBILITY_CHANGED
    assert seen[0].payload == {"visible": False}


def test_event_constant_defined():
    assert events.EVENT_SOURCE_VISIBILITY_CHANGED == "SOURCE_VISIBILITY_CHANGED"


def test_real_sources_adult_flag_matches_approved_list():
    """真实源文件的 adult 标记必须与用户确认的 21 个源完全一致。"""
    flagged = set()
    for path in SOURCES_DIR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("$metadata") or {}).get("adult") is True:
            flagged.add(data.get("$id"))
    assert flagged == ADULT_IDS


def test_explicitly_non_adult_sources_stay_clean():
    """09-28 老 spec 误列的这 6 个源必须不带 adult 标记。"""
    for sid in ("comicbox", "manben", "czbooks", "badnews", "fdzys", "trtag"):
        data = json.loads((SOURCES_DIR / f"{sid}.json").read_text(encoding="utf-8"))
        assert (data.get("$metadata") or {}).get("adult") is not True, sid


def test_content_setting_default_true():
    from framework.settings_manager import DEFAULTS

    assert DEFAULTS["content"]["show_adult_sources"] is True


def test_settings_manager_reads_content_section(tmp_path):
    from framework.settings_manager import SettingsManager

    config_path = tmp_path / "app_config.json"
    sm = SettingsManager(config_path)
    assert sm.get("content", "show_adult_sources", None) is True

    sm.set("content", "show_adult_sources", False)
    sm.save()

    sm2 = SettingsManager(config_path)
    assert sm2.get("content", "show_adult_sources", True) is False


def test_settings_page_content_tab_loads_and_saves(tmp_path, _qapp, monkeypatch):
    from framework.settings_manager import SettingsManager
    from gui.pages.settings_page import SettingsPage

    config_path = tmp_path / "app_config.json"
    sm = SettingsManager(config_path)
    sm.set("content", "show_adult_sources", False)
    sm.save()

    monkeypatch.setattr("framework.cache_service.get_shelf_cache", lambda: None)
    monkeypatch.setattr("framework.cache_service.get_search_cache", lambda: None)

    class _ThemeManager:
        def switch_to(self, _theme):
            pass

    page = SettingsPage(sm, _ThemeManager())

    assert "内容" in [page.tabs.tabText(i) for i in range(page.tabs.count())]
    assert page._content_adult.isChecked() is False

    page._content_adult.setChecked(True)
    page._on_apply()

    reloaded = SettingsManager(config_path)
    assert reloaded.get("content", "show_adult_sources", False) is True


def _assert_visibility_event_calls(page, bus, method_name):
    calls = []
    setattr(page, method_name, lambda: calls.append(method_name))

    bus.emit(events.Event("UNRELATED"))
    assert calls == []

    bus.emit(events.Event(events.EVENT_SOURCE_VISIBILITY_CHANGED))
    assert calls == [method_name]


def test_discover_page_refreshes_for_visibility_event_only(_qapp):
    from framework.events import EventBus
    from gui.pages.discover_page import DiscoverPage

    bus = EventBus()
    page = DiscoverPage(_mgr(), None, None, None, bus, None)

    _assert_visibility_event_calls(page, bus, "refresh")


def test_source_page_refreshes_for_visibility_event_only(tmp_path, _qapp):
    from framework.events import EventBus
    from gui.pages.source_page import SourcePage

    bus = EventBus()
    page = SourcePage(_mgr(), None, tmp_path, event_bus=bus)

    _assert_visibility_event_calls(page, bus, "refresh")


def test_home_page_refreshes_for_visibility_event_only(tmp_path, _qapp):
    from framework.events import EventBus
    from framework.search_history import SearchHistory
    from framework.settings_manager import SettingsManager
    from gui.pages.home_page import HomePage

    class _ThemeManager:
        def current_key(self):
            return "sakura"

        def switch_to(self, _key):
            pass

    bus = EventBus()
    page = HomePage(
        _mgr(),
        bus,
        _ThemeManager(),
        SettingsManager(tmp_path / "app_config.json"),
        SearchHistory(tmp_path / "search_history.json"),
    )

    _assert_visibility_event_calls(page, bus, "refresh")


def test_home_page_hides_adult_aggregates_and_broken_entry_after_event(
    tmp_path, _qapp
):
    from framework.events import EventBus
    from framework.search_history import SearchHistory
    from framework.settings_manager import SettingsManager
    from framework.source_manager import HEALTH_BROKEN
    from gui.pages.home_page import HomePage

    class _ThemeManager:
        def current_key(self):
            return "sakura"

        def switch_to(self, _key):
            pass

    plain = _cfg("plain", ctype="comic")
    spicy = _cfg("spicy", ctype="video", adult=True)
    manager = _mgr(plain, spicy)
    manager.update_health("plain", HEALTH_BROKEN, "plain failure")
    manager.update_health("spicy", HEALTH_BROKEN, "adult failure")
    bus = EventBus()
    manager.set_event_bus(bus)
    page = HomePage(
        manager,
        bus,
        _ThemeManager(),
        SettingsManager(tmp_path / "app_config.json"),
        SearchHistory(tmp_path / "search_history.json"),
    )

    assert page.stats_row.card_comic.value_label.text() == "1"
    assert page.stats_row.card_video.value_label.text() == "1"
    assert page.stats_row.card_enabled.value_label.text() == "2"
    assert page.stats_row.card_broken.value_label.text() == "2"
    assert "SPICY" in page.broken_card.list_label.text()

    manager.set_adult_visible(False)

    assert page.stats_row.card_comic.value_label.text() == "1"
    assert page.stats_row.card_video.value_label.text() == "0"
    assert page.stats_row.card_enabled.value_label.text() == "1"
    assert page.stats_row.card_broken.value_label.text() == "1"
    assert "PLAIN" in page.broken_card.list_label.text()
    assert "plain failure" in page.broken_card.list_label.text()
    assert "SPICY" not in page.broken_card.list_label.text()
    assert "adult failure" not in page.broken_card.list_label.text()


def test_search_page_visibility_event_restarts_existing_search_without_adult_sources(
    _qapp, monkeypatch
):
    from framework.events import EventBus
    from framework.parser import Parser
    from framework.search import Search
    from gui.pages import search_page as search_page_module
    from gui.pages.search_page import SearchPage

    plain = _cfg("plain")
    spicy = _cfg("spicy", adult=True)
    manager = _mgr(plain, spicy)
    manager.set_adult_visible(False)
    bus = EventBus()
    page = SearchPage(manager, Search(http=None, parser=Parser()), event_bus=bus)
    page.keyword_input.setText("existing query")
    page._results = [
        SimpleNamespace(source_id="plain", source_name="PLAIN", url="/plain"),
        SimpleNamespace(source_id="spicy", source_name="SPICY", url="/spicy"),
    ]
    page._refresh_source_chips()
    assert sorted(page._source_chip_btns) == ["plain", "spicy"]

    captured_sources = []
    page._build_status_bar = lambda sources: captured_sources.append(list(sources))

    class _Pool:
        def start(self, task):
            pass

    class _RecordingThreadPool:
        @staticmethod
        def globalInstance():
            return _Pool()

    monkeypatch.setattr(search_page_module, "QThreadPool", _RecordingThreadPool)

    bus.emit(events.Event("UNRELATED"))
    assert len(page._results) == 2
    assert captured_sources == []

    bus.emit(events.Event(events.EVENT_SOURCE_VISIBILITY_CHANGED))

    assert page._results == []
    assert page._source_chip_btns == {}
    assert [[source.source_id for source in sources] for sources in captured_sources] == [
        ["plain"]
    ]


def test_search_page_visibility_event_invalidates_old_search_when_none_remain(_qapp):
    from framework.events import EventBus
    from framework.parser import Parser
    from framework.search import Search
    from gui.pages.search_page import SearchPage

    manager = _mgr(_cfg("spicy", adult=True))
    manager.set_adult_visible(False)
    bus = EventBus()
    page = SearchPage(manager, Search(http=None, parser=Parser()), event_bus=bus)
    page.keyword_input.setText("existing query")
    page._results = [
        SimpleNamespace(source_id="spicy", source_name="SPICY", url="/spicy")
    ]
    page._build_status_bar([manager.get("spicy")])
    page.status_bar.setVisible(True)
    assert sorted(page._status_chips) == ["spicy"]
    assert page.status_bar.isHidden() is False
    old_epoch = page._search_epoch

    bus.emit(events.Event(events.EVENT_SOURCE_VISIBILITY_CHANGED))

    assert page._results == []
    assert page._search_epoch == old_epoch + 1
    assert page.status_label.text() == "没有可搜索的源"
    assert page._status_chips == {}
    assert page.status_bar.isHidden() is True

    stale = SimpleNamespace(
        source_id="spicy",
        source_name="SPICY",
        url="/stale-adult",
        cover="",
    )
    page._on_source_page(manager.get("spicy"), [stale], old_epoch)

    assert page._results == []
    assert page._source_chip_btns == {}
    assert page.grid_layout.count() == 0


def test_search_page_hides_status_when_explicit_adult_selection_disappears(_qapp):
    from framework.events import EventBus
    from framework.parser import Parser
    from framework.search import Search
    from gui.pages.search_page import SearchPage

    spicy = _cfg("spicy", adult=True)
    manager = _mgr(spicy)
    bus = EventBus()
    manager.set_event_bus(bus)
    page = SearchPage(manager, Search(http=None, parser=Parser()), event_bus=bus)
    page.keyword_input.setText("existing query")
    page._all_selected = False
    page._selected_sources = {"spicy"}
    page._results = [
        SimpleNamespace(source_id="spicy", source_name="SPICY", url="/spicy")
    ]
    page._build_status_bar([spicy])
    page.status_bar.setVisible(True)
    old_epoch = page._search_epoch

    manager.set_adult_visible(False)

    assert page._selected_sources == set()
    assert page._results == []
    assert page._search_epoch == old_epoch + 1
    assert page.status_label.text() == "未选择任何源"
    assert page._status_chips == {}
    assert page.status_bar.isHidden() is True

    stale = SimpleNamespace(
        source_id="spicy",
        source_name="SPICY",
        url="/stale-adult",
        cover="",
    )
    page._on_source_page(spicy, [stale], old_epoch)

    assert page._results == []
    assert page._source_chip_btns == {}
    assert page.grid_layout.count() == 0


def test_search_page_visibility_clears_prior_adult_search_with_empty_keyword(_qapp):
    from framework.events import EventBus
    from framework.parser import Parser
    from framework.search import Search, SearchResult
    from gui.pages.search_page import SearchPage

    spicy = _cfg("spicy", adult=True)
    manager = _mgr(spicy)
    bus = EventBus()
    manager.set_event_bus(bus)
    page = SearchPage(manager, Search(http=None, parser=Parser()), event_bus=bus)
    result = SearchResult(
        title="Adult result",
        url="/spicy",
        source_id="spicy",
        source_name="SPICY",
    )
    page._results = [result]
    page._results_display = [result]
    page._shown_count = 1
    page._refresh_source_chips()
    page._show_results()
    page._build_status_bar([spicy])
    page.status_bar.setVisible(True)
    page.keyword_input.clear()
    old_epoch = page._search_epoch

    page._on_search()

    assert page._search_epoch == old_epoch
    assert page._results == [result]

    manager.set_adult_visible(False)

    assert page.keyword_input.text() == ""
    assert page._search_epoch == old_epoch + 1
    assert page._results == []
    assert page._source_chip_btns == {}
    assert page._status_chips == {}
    assert page.status_bar.isHidden() is True
    assert page.grid_layout.count() == 0

    stale = SearchResult(
        title="Stale adult result",
        url="/stale-adult",
        source_id="spicy",
        source_name="SPICY",
    )
    page._on_source_page(spicy, [stale], old_epoch)

    assert page._results == []
    assert page._source_chip_btns == {}
    assert page.grid_layout.count() == 0


def test_sync_source_visibility_applies_setting_and_bus():
    from gui.app import _sync_source_visibility

    class _Settings:
        def get(self, section, key, default):
            assert (section, key, default) == ("content", "show_adult_sources", True)
            return False

    class _Manager:
        def __init__(self):
            self.calls = []

        def set_event_bus(self, bus):
            self.calls.append(("bus", bus))

        def set_adult_visible(self, visible):
            self.calls.append(("visible", visible))

    manager = _Manager()
    bus = object()

    _sync_source_visibility(_Settings(), manager, bus)

    assert manager.calls == [("bus", bus), ("visible", False)]


def test_mainwindow_startup_syncs_visibility_before_page_construction(
    tmp_path, _qapp, monkeypatch
):
    import gui.app as app_module
    import framework.cache_service as cache_service_module
    import framework.shelf_cache_repository as repository_module
    from gui.components.cover_loader import CoverLoader

    (tmp_path / "app_config.json").write_text(
        json.dumps({"content": {"show_adult_sources": False}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "_app_base_dir", lambda: tmp_path)
    monkeypatch.setattr(cache_service_module, "get_shelf_cache", lambda *_args: None)
    monkeypatch.setattr(cache_service_module, "get_search_cache", lambda *_args: None)

    class _NoRepository:
        def __init__(self, *_args, **_kwargs):
            raise OSError("repository disabled for startup boundary test")

    monkeypatch.setattr(repository_module, "ShelfCacheRepository", _NoRepository)

    class _Service:
        def __init__(self, *_args, **_kwargs):
            pass

    for name in (
        "HttpClient",
        "Parser",
        "StructureChecker",
        "Discovery",
        "Decrypter",
        "Content",
        "BulkFetch",
        "Search",
        "DownloadQueue",
    ):
        monkeypatch.setattr(app_module, name, _Service)

    class _CoverLoader:
        def configure(self, *_args, **_kwargs):
            pass

        def use_http(self, _http):
            pass

        def register_source(self, _source):
            pass

    monkeypatch.setattr(CoverLoader, "instance", staticmethod(lambda: _CoverLoader()))

    order = []
    real_sync = app_module._sync_source_visibility

    def record_sync(settings, source_manager, event_bus=None):
        order.append(("sync", settings, source_manager, event_bus))
        real_sync(settings, source_manager, event_bus)

    def record_pages(window):
        order.append(
            ("pages", window.settings, window.source_manager, window.event_bus)
        )

    monkeypatch.setattr(app_module, "_sync_source_visibility", record_sync)
    monkeypatch.setattr(app_module.MainWindow, "_build_pages", record_pages)
    monkeypatch.setattr(app_module.MainWindow, "_apply_theme_qss", lambda *_args: None)
    monkeypatch.setattr(app_module.MainWindow, "_install_shortcuts", lambda *_args: None)
    monkeypatch.setattr(app_module.MainWindow, "_schedule_startup_diag", lambda *_args: None)
    monkeypatch.setattr(app_module.MainWindow, "_schedule_source_selection", lambda *_args: None)

    window = app_module.MainWindow()

    assert [entry[0] for entry in order] == ["sync", "pages"]
    assert order[0][1:] == (window.settings, window.source_manager, window.event_bus)
    assert order[1][1:] == (window.settings, window.source_manager, window.event_bus)
    assert window.source_manager.is_adult_visible() is False
    assert window.source_manager._bus is window.event_bus


def test_mainwindow_settings_apply_syncs_real_attributes_first(_qapp, monkeypatch):
    import gui.app as app_module
    import framework.cache_service as cache_service_module
    from framework.events import EventBus
    from framework.source_manager import SourceManager
    from gui.components.cover_loader import CoverLoader

    class _Settings:
        def get(self, section, key, default=None):
            values = {
                ("content", "show_adult_sources"): False,
                ("ui", "font_scale"): 1.0,
                ("ui", "reading_bg"): "",
                ("ui", "reading_font_size"): 0,
                ("ui", "cover_cache_size_mb"): 256,
            }
            return values.get((section, key), default)

    class _ThemeManager:
        def current_key(self):
            return "sakura"

    class _CoverLoader:
        def configure(self, *_args, **_kwargs):
            pass

    cover_loader = _CoverLoader()
    monkeypatch.setattr(CoverLoader, "instance", staticmethod(lambda: cover_loader))
    monkeypatch.setattr(cache_service_module, "get_shelf_cache", lambda *_args: None)

    window = app_module.MainWindow.__new__(app_module.MainWindow)
    window.settings = _Settings()
    window.source_manager = SourceManager()
    window.event_bus = EventBus()
    window.theme_manager = _ThemeManager()
    window.reader = None
    order = []
    window._apply_theme_qss = lambda _key: order.append("theme")
    real_sync = app_module._sync_source_visibility

    def record_sync(settings, source_manager, event_bus=None):
        order.append(("sync", settings, source_manager, event_bus))
        real_sync(settings, source_manager, event_bus)

    monkeypatch.setattr(app_module, "_sync_source_visibility", record_sync)

    window._on_settings_applied()

    assert order == ["theme"]
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.processEvents()
    assert order[1] == (
        "sync",
        window.settings,
        window.source_manager,
        window.event_bus,
    )
    assert window.source_manager.is_adult_visible() is False
    assert window.source_manager._bus is window.event_bus


def test_settings_apply_defers_visibility_refresh_and_uses_latest_value(_qapp, monkeypatch):
    import gui.app as app_module
    import framework.cache_service as cache_service_module
    from framework.events import EventBus
    from framework.source_manager import SourceManager
    from PySide6.QtCore import QCoreApplication
    from gui.components.cover_loader import CoverLoader

    class _Settings:
        def __init__(self, visible):
            self.visible = visible

        def get(self, section, key, default=None):
            if (section, key) == ("content", "show_adult_sources"):
                return self.visible
            return default

    class _CoverLoader:
        def configure(self, *_args, **_kwargs):
            pass

    window = app_module.MainWindow.__new__(app_module.MainWindow)
    window.settings = _Settings(False)
    window.source_manager = SourceManager()
    window.event_bus = EventBus()
    window.theme_manager = type("Theme", (), {"current_key": lambda self: "sakura"})()
    window.reader = None
    window._apply_theme_qss = lambda _key: None

    values = []
    real_sync = app_module._sync_source_visibility

    def record_sync(settings, manager, bus):
        values.append(settings.get("content", "show_adult_sources", True))
        real_sync(settings, manager, bus)

    monkeypatch.setattr(app_module, "_sync_source_visibility", record_sync)
    monkeypatch.setattr(CoverLoader, "instance", staticmethod(lambda: _CoverLoader()))
    monkeypatch.setattr(cache_service_module, "get_shelf_cache", lambda *_args: None)

    QCoreApplication.processEvents()
    window._on_settings_applied()
    window.settings.visible = True
    window._on_settings_applied()

    assert values == []
    QCoreApplication.processEvents()
    assert values == [True]
    assert window.source_manager.is_adult_visible() is True


def test_settings_apply_reentrant_visibility_sync_is_bounded(_qapp, monkeypatch):
    import gui.app as app_module
    import framework.cache_service as cache_service_module
    from framework.events import EventBus
    from framework.source_manager import SourceManager
    from PySide6.QtCore import QCoreApplication
    from gui.components.cover_loader import CoverLoader

    class _Settings:
        def __init__(self):
            self.visible = False

        def get(self, section, key, default=None):
            if (section, key) == ("content", "show_adult_sources"):
                return self.visible
            return default

    class _CoverLoader:
        def configure(self, *_args, **_kwargs):
            pass

    window = app_module.MainWindow.__new__(app_module.MainWindow)
    window.settings = _Settings()
    window.source_manager = SourceManager()
    window.event_bus = EventBus()
    window.theme_manager = type("Theme", (), {"current_key": lambda self: "sakura"})()
    window.reader = None
    window._apply_theme_qss = lambda _key: None

    values = []
    reentrant_applies = []
    real_sync = app_module._sync_source_visibility

    def record_sync(settings, manager, bus):
        values.append(settings.get("content", "show_adult_sources", True))
        real_sync(settings, manager, bus)

    def apply_once_on_visibility(event):
        if reentrant_applies:
            return
        reentrant_applies.append(1)
        window.settings.visible = True
        window._on_settings_applied()

    monkeypatch.setattr(app_module, "_sync_source_visibility", record_sync)
    monkeypatch.setattr(CoverLoader, "instance", staticmethod(lambda: _CoverLoader()))
    monkeypatch.setattr(cache_service_module, "get_shelf_cache", lambda *_args: None)
    window.event_bus.subscribe(apply_once_on_visibility)

    QCoreApplication.processEvents()
    window._on_settings_applied()
    for _ in range(3):
        QCoreApplication.processEvents()

    assert reentrant_applies == [1]
    assert values == [False, True]
    assert len(values) == 2
    assert window.source_manager.is_adult_visible() is True
