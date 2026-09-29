import time

from framework.config import SourceConfig
from framework.content import Content, Detail


def _src():
    return SourceConfig.from_dict({
        "$schema_version": 2,
        "$id": "s1",
        "$type": "novel",
        "$name": "S1",
        "transports": {"base_url": "https://s1.example"},
        "$metadata": {"homepage": "https://s1.example"},
    }, "s1.json")


class _Cache:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key):
        expiry = self.ttls.get(key)
        if expiry is not None and expiry <= time.time():
            return None
        return self.values.get(key)

    def set(self, key, value, ttl=None):
        self.values[key] = value
        self.ttls[key] = time.time() + ttl if ttl is not None else None

    def delete(self, key):
        self.values.pop(key, None)
        self.ttls.pop(key, None)

    def scan(self, pattern):
        prefix = pattern.removesuffix("*")
        return [key for key in self.values if key.startswith(prefix)]


def test_shelf_merge_keeps_same_title_from_different_sources_distinct(tmp_path):
    from framework.shelf_service import ShelfItem, ShelfService

    service = ShelfService(tmp_path / "downloads")
    items = service._merge(
        [],
        [
            ShelfItem("a", "favorite", "同名", "novel", url="https://book.example/1", source_id="s1"),
            ShelfItem("b", "favorite", "同名", "novel", url="https://book.example/1", source_id="s2"),
        ],
    )

    assert [(item.source_id, item.url) for item in items] == [
        ("s1", "https://book.example/1"),
        ("s2", "https://book.example/1"),
    ]


def test_shelf_merge_collapses_duplicate_same_source_url_preserving_first_record(tmp_path):
    from framework.shelf_service import ShelfItem, ShelfService

    service = ShelfService(tmp_path / "downloads")
    items = service._merge(
        [],
        [
            ShelfItem("first", "favorite", "首选", "novel", url="https://BOOK.example:443/1?a=1&b=2", source_id="s1"),
            ShelfItem("second", "favorite", "重复", "novel", url="https://book.example/1?b=2&a=1", source_id="s1"),
        ],
    )

    assert len(items) == 1
    assert items[0].key == "first"
    assert items[0].title == "首选"


def test_render_deduplicates_same_stable_url_preserving_local_metadata(_qapp, tmp_path):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    store = LibraryStore(tmp_path / "library.json")
    page = LibraryPage(library_store=store)
    page._last_ctype = ""
    page._last_folder = "全部"
    page._last_kw = ""
    page._last_sort = "recent"
    page._visible_combo_state = lambda: True
    page._maybe_backfill_covers = lambda favorites: None
    captured = []
    page._add_group = lambda title, items: captured.append((title, items))

    page._render([
        {
            "kind": "local",
            "title": "本地书名",
            "url": "https://book.example/1",
            "path": str(tmp_path / "book.epub"),
            "folder": "收藏夹",
            "source_id": "s1",
            "online": True,
        },
        {
            "kind": "favorite",
            "title": "收藏书名",
            "url": "https://book.example/1",
            "folder": "收藏夹",
            "source_id": "s1",
        },
    ])

    rendered = [item["rec"] for _, items in captured for item in items]
    assert len(rendered) == 1
    assert rendered[0]["kind"] == "local"
    assert rendered[0]["path"].endswith("book.epub")
    assert rendered[0]["source_id"] == "s1"


def test_same_source_and_url_renders_one_preferred_item(_qapp, tmp_path):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    page = LibraryPage(library_store=LibraryStore(tmp_path / "library.json"))
    page._last_ctype = ""
    page._last_folder = "全部"
    page._last_kw = ""
    page._last_sort = "recent"
    page._visible_combo_state = lambda: True
    page._maybe_backfill_covers = lambda favorites: None
    captured = []
    page._add_group = lambda title, items: captured.append((title, items))

    page._render([
        {"kind": "favorite", "source_id": "s1", "url": "https://book.example/1", "title": "旧"},
        {"kind": "favorite", "source_id": "s1", "url": "https://book.example/1", "title": "新"},
    ])

    rendered = [item["rec"] for _, items in captured for item in items]
    assert [(item["source_id"], item["title"]) for item in rendered] == [("s1", "新")]


def test_same_url_from_different_sources_remains_two_rendered_items(_qapp, tmp_path):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    page = LibraryPage(library_store=LibraryStore(tmp_path / "library.json"))
    page._last_ctype = ""
    page._last_folder = "全部"
    page._last_kw = ""
    page._last_sort = "recent"
    page._visible_combo_state = lambda: True
    page._maybe_backfill_covers = lambda favorites: None
    captured = []
    page._add_group = lambda title, items: captured.append((title, items))

    page._render([
        {"kind": "favorite", "source_id": "s1", "url": "https://book.example/1", "title": "源一"},
        {"kind": "favorite", "source_id": "s2", "url": "https://book.example/1", "title": "源二"},
    ])

    rendered = [item["rec"] for _, items in captured for item in items]
    assert [(item["source_id"], item["title"]) for item in rendered] == [("s1", "源一"), ("s2", "源二")]


def test_folder_render_deduplicates_after_local_favorite_merge_using_normalized_identity(_qapp, tmp_path):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    page = LibraryPage(library_store=LibraryStore(tmp_path / "library.json"))
    page._last_ctype = ""
    page._last_folder = "收藏夹"
    page._last_kw = ""
    page._last_sort = "recent"
    page._visible_combo_state = lambda: True
    page._maybe_backfill_covers = lambda favorites: None
    captured = []
    page._add_group = lambda title, items: captured.append((title, items))

    page._render([
        {"kind": "local", "source_id": "s1", "url": "https://book.example:443/1/?a=1&b=2", "path": str(tmp_path / "book.epub"), "title": "本地", "folder": "收藏夹"},
        {"kind": "favorite", "source_id": "s1", "url": "https://BOOK.example/1?b=2&a=1", "title": "收藏", "folder": "收藏夹"},
        {"kind": "favorite", "source_id": "s1", "url": "https://book.example:443/1?a=1&b=2", "title": "重复", "folder": "收藏夹"},
    ])

    rendered = [item["rec"] for _, items in captured for item in items]
    assert len(rendered) == 1
    assert rendered[0]["kind"] == "local"
    assert rendered[0]["path"].endswith("book.epub")


def test_all_view_locked_content_stays_hidden_after_session_unlock(_qapp, tmp_path):
    from framework.folder_lock import hash_password, new_salt
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    salt = new_salt()
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("私密", locked=True, pw=hash_password("secret", salt), salt=salt)
    store.add("s1", "u1", "秘密书", "novel", folder="私密")
    page = LibraryPage(library_store=store)
    page._last_ctype = ""
    page._last_folder = "全部"
    page._last_kw = ""
    page._last_sort = "recent"
    page._visible_combo_state = lambda: True
    page._maybe_backfill_covers = lambda favorites: None
    captured = []
    page._add_group = lambda title, items: captured.append((title, items))

    book = {"kind": "favorite", "title": "秘密书", "folder": "私密", "url": "u1"}
    page._render([book])
    assert captured == []

    page._unlocked.add("私密")
    page._render([book])
    assert captured == []


def test_refresh_detail_forces_network_and_invalidates_chapter_cache():
    c = Content.__new__(Content)
    c._cache = _Cache()
    c._repository = None
    source = _src()
    source.raw["api_endpoints"] = {"detail": {"url": "/detail"}}
    detail_url = "https://s1.example/book/1"
    detail_key = "detail:s1:" + detail_url
    body_key = "body:s1:https://s1.example/ch1"
    unrelated_body_key = "body:s1:https://s1.example/other-book/ch1"
    c._cache.set(detail_key, '{"title":"旧","url":"https://s1.example/book/1","source_id":"s1","content_type":"novel","chapters":[{"title":"第一章","url":"https://s1.example/ch1"}]}')
    c._cache.set(body_key, "旧正文")
    c._cache.set(unrelated_body_key, "另一本书正文")
    calls = []
    c._fetch_detail_api = lambda *args: calls.append(args) or Detail(
        title="新详情", url=detail_url, source_id="s1", content_type="novel",
        chapters=[{"title":"第一章","url":"https://s1.example/ch1"}],
    )
    c._fetch_chapter_page = lambda *args: ("新正文", "")

    got = c.refresh_detail(source, detail_url, current_chapter_url=body_key.split(":", 2)[2])

    assert got.title == "新详情"
    assert len(calls) == 1
    assert c._cache.get(unrelated_body_key) == "另一本书正文"
    assert c.fetch_chapter(source, "https://s1.example/ch1") == "新正文"


def test_refresh_detail_evicts_repository_body_cache_and_refetches_current(tmp_path):
    from framework.shelf_cache_repository import ShelfCacheRepository

    repository = ShelfCacheRepository(tmp_path / "shelf.sqlite3", tmp_path / "content")
    c = Content.__new__(Content)
    c._cache = _Cache()
    c._repository = repository
    source = _src()
    detail_url = "https://s1.example/book/1"
    current = "https://s1.example/ch1"
    stale = "https://s1.example/old"
    latest = "https://s1.example/ch2"
    for chapter, text in ((current, "旧正文"), (stale, "过时正文"), (latest, "保留正文")):
        c._cache_set(f"body:s1:{chapter}", text, book_key=detail_url)
    c.fetch_detail = lambda *_args: Detail(
        title="新详情", url=detail_url, source_id="s1", content_type="novel",
        chapters=[{"title": "第二章", "url": latest}],
    )
    c._fetch_chapter_page = lambda *args: ("新正文", "")

    c.refresh_detail(source, detail_url, current_chapter_url=current)

    assert repository.get_content(detail_url, current) is None
    assert repository.get_content(detail_url, stale) is None
    assert repository.get_content(detail_url, latest) == "保留正文"
    assert c.fetch_chapter(source, current) == "新正文"


def test_session_unlock_is_not_persisted_across_fresh_page_instance(_qapp, tmp_path):
    from framework.folder_lock import hash_password, new_salt
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    path = tmp_path / "library.json"
    salt = new_salt()
    store = LibraryStore(path)
    store.create_folder("私密", locked=True, pw=hash_password("secret", salt), salt=salt)
    store.add("s1", "u1", "秘密书", "novel", folder="私密")

    first = LibraryPage(library_store=store)
    first._unlocked.add("私密")
    assert first.is_folder_unlocked("私密") is True

    second = LibraryPage(library_store=LibraryStore(path))
    assert second.is_folder_unlocked("私密") is False


def test_fetch_chapter_cache_hit_avoids_network_callable():
    c = Content.__new__(Content)
    c._cache = _Cache()
    c._repository = None
    c._cache_set("body:s1:https://s1.example/ch1", "缓存正文", ttl=100)
    c._fetch_chapter_page = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network"))

    assert c.fetch_chapter(_src(), "https://s1.example/ch1") == "缓存正文"


def test_expired_detail_cache_falls_back_to_network_callable():
    c = Content.__new__(Content)
    c._cache = _Cache()
    c._repository = None
    source = _src()
    source.raw["api_endpoints"] = {"detail": {"url": "/detail"}}
    key = "detail:s1:" + c._abs_url(source, "https://s1.example/book/1")
    c._cache.set(key, '{"title":"旧"}', ttl=-1)
    calls = []
    c._fetch_detail_api = lambda *args: calls.append(args) or Detail(
        title="网络详情", url="https://s1.example/book/1", source_id="s1", content_type="novel", chapters=[]
    )

    got = c.fetch_detail(source, "https://s1.example/book/1")
    assert got.title == "网络详情"
    assert len(calls) == 1


def test_malformed_detail_cache_falls_back_to_network_callable():
    c = Content.__new__(Content)
    c._cache = _Cache()
    c._repository = None
    source = _src()
    source.raw["api_endpoints"] = {"detail": {"url": "/detail"}}
    key = "detail:s1:" + c._abs_url(source, "https://s1.example/book/1")
    c._cache.values[key] = "not-json"
    calls = []
    c._fetch_detail_api = lambda *args: calls.append(args) or Detail(
        title="网络详情", url="https://s1.example/book/1", source_id="s1", content_type="novel", chapters=[]
    )

    got = c.fetch_detail(source, "https://s1.example/book/1")
    assert got.title == "网络详情"
    assert len(calls) == 1


def test_chapter_cache_miss_uses_network_and_writes_cache():
    c = Content.__new__(Content)
    c._cache = _Cache()
    c._repository = None
    calls = []
    c._fetch_chapter_page = lambda *args: calls.append(args) or ("网络正文", "")

    assert c.fetch_chapter(_src(), "https://s1.example/ch1") == "网络正文"
    assert len(calls) == 1
    assert c._cache.get("body:s1:https://s1.example/ch1") == "网络正文"


def test_cached_detail_keeps_chapters_for_shelf_opening():
    c = Content.__new__(Content)
    c._cache = _Cache()
    c._repository = None
    detail = Detail(
        title="缓存书",
        url="https://s1.example/book/1",
        source_id="s1",
        content_type="novel",
        chapters=[{"title": "第一章", "url": "https://s1.example/ch1"}],
    )
    key = f"detail:s1:{c._abs_url(_src(), detail.url)}"
    c._cache_set_detail(key, detail)
    c._fetch_detail_page = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network"))

    got = c.fetch_detail(_src(), detail.url)
    assert got.title == "缓存书"
    assert got.chapters[0].url.endswith("/ch1")


def test_settings_apply_defers_cover_cache_refresh_and_coalesces_latest(_qapp, monkeypatch):
    import gui.app as app_module
    import framework.cache_service as cache_service_module
    from PySide6.QtCore import QCoreApplication
    from gui.components.cover_loader import CoverLoader

    class _Settings:
        value = 128

        def get(self, section, key, default=None):
            if (section, key) == ("ui", "cover_cache_size_mb"):
                return self.value
            return default

    class _Loader:
        def __init__(self):
            self.calls = []

        def configure(self, size, **kwargs):
            self.calls.append(size)

    loader = _Loader()
    window = app_module.MainWindow.__new__(app_module.MainWindow)
    window.settings = _Settings()
    window.source_manager = type("Sources", (), {})()
    window.event_bus = type("Bus", (), {})()
    window.theme_manager = type("Theme", (), {"current_key": lambda self: "sakura"})()
    window.reader = None
    window._apply_theme_qss = lambda _key: None
    monkeypatch.setattr(CoverLoader, "instance", staticmethod(lambda: loader))
    monkeypatch.setattr(cache_service_module, "get_shelf_cache", lambda *_args: None)
    monkeypatch.setattr(app_module, "_sync_source_visibility", lambda *args: None)

    window._on_settings_applied()
    window.settings.value = 256
    window._on_settings_applied()

    assert loader.calls == []
    QCoreApplication.processEvents()
    assert loader.calls == [256]
