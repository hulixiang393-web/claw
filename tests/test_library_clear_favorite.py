from framework.library_store import LibraryStore
from framework.shelf_service import ShelfService


class _FakeRepo:
    def __init__(self):
        self.cleared = []
        self.cached = {"u1", "u2", "u3", "unrelated"}

    def clear_content_cache(self, url):
        self.cleared.append(url)
        self.cached.discard(url)
        return True


def _service(tmp_path):
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("漫画")
    store.add("s1", "u1", "A", "comic", folder="漫画")
    store.add("s1", "u2", "B", "comic", folder="漫画")
    store.add("s1", "u3", "C", "novel", folder="")
    repo = _FakeRepo()
    svc = ShelfService(
        str(tmp_path / "output"),
        library_store=store,
        reading_progress=None,
        repository=repo,
    )
    return svc, store, repo


def test_clear_folder_syncs_repository(tmp_path):
    svc, store, repo = _service(tmp_path)
    n = svc.favorite_clear_folder("漫画")
    assert n == 2
    assert sorted(repo.cleared) == ["u1", "u2"]
    assert store.folder_items("漫画") == []


def test_clear_all_favorites_syncs_repository(tmp_path):
    svc, store, repo = _service(tmp_path)
    n = svc.clear_all_favorites()
    assert n == 3
    assert sorted(repo.cleared) == ["u1", "u2", "u3"]
    assert store.list_all() == []


def test_clear_all_favorites_is_safe_when_repository_missing(tmp_path):
    store = LibraryStore(tmp_path / "library.json")
    store.add("s1", "u1", "A", "comic")
    svc = ShelfService(
        str(tmp_path / "output"),
        library_store=store,
        reading_progress=None,
        repository=None,
    )
    assert svc.clear_all_favorites() == 1


def test_clear_button_stays_enabled(_qapp, tmp_path):
    from gui.pages.library_page import LibraryPage

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("漫画")
    page = LibraryPage(library_store=store)
    assert page.clear_fav_btn.isEnabled() is True
    page._sync_combos()
    assert page.clear_fav_btn.isEnabled() is True
    assert "全部" in page.clear_fav_btn.text()


def test_sync_combos_without_store_keeps_button_enabled(_qapp):
    from gui.pages.library_page import LibraryPage

    page = LibraryPage(library_store=None)
    page._sync_combos()
    assert page.clear_fav_btn.isEnabled() is True


def test_clear_folder_preserves_folder_and_unrelated_cache(tmp_path):
    svc, store, repo = _service(tmp_path)

    assert svc.favorite_clear_folder("漫画") == 2

    assert store.list_folders() == ["漫画"]
    assert store.list_all() == [store.get("u3")]
    assert repo.cached == {"u3", "unrelated"}


def test_clear_button_text_and_enabled_state_for_all_and_folder(_qapp, tmp_path):
    from gui.pages.library_page import LibraryPage

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("漫画")
    store.add("s1", "u1", "A", "comic", folder="漫画")
    page = LibraryPage(library_store=store)

    page.folder_combo.setCurrentText("全部")
    page._sync_combos()
    assert page.clear_fav_btn.isEnabled() is True
    assert page.clear_fav_btn.text() == "一键清空全部收藏(1)"

    page.folder_combo.setCurrentText("漫画")
    page._sync_combos()
    assert page.clear_fav_btn.isEnabled() is True
    assert page.clear_fav_btn.text() == "清空收藏夹「漫画」(1)"


def test_clear_button_confirmation_routes_to_selected_folder(_qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from gui.pages.library_page import LibraryPage

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("漫画")
    store.add("s1", "u1", "A", "comic", folder="漫画")
    store.add("s1", "u2", "B", "novel", folder="")
    page = LibraryPage(library_store=store)
    page.folder_combo.setCurrentText("漫画")
    page._sync_combos()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *args, **kwargs: QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *args, **kwargs: None))
    calls = []
    original = page._shelf.favorite_clear_folder
    page._shelf.favorite_clear_folder = lambda folder: calls.append(folder) or original(folder)
    page._rebuild = lambda: None

    page._clear_folder()

    assert calls == ["漫画"]
    assert store.has("u2")


def test_clear_button_confirmation_routes_to_all_favorites(_qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from gui.pages.library_page import LibraryPage

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("漫画")
    store.add("s1", "u1", "A", "comic", folder="漫画")
    store.add("s1", "u2", "B", "novel", folder="")
    page = LibraryPage(library_store=store)
    page.folder_combo.setCurrentText("全部")
    page._sync_combos()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *args, **kwargs: QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *args, **kwargs: None))
    calls = []
    original = page._shelf.clear_all_favorites
    page._shelf.clear_all_favorites = lambda: calls.append(True) or original()
    page._rebuild = lambda: None

    page._clear_folder()

    assert calls == [True]
    assert store.list_all() == []


def _page(tmp_path, items=(), folders=("漫画",)):
    """建一个带收藏数据的 LibraryPage，返回 (page, store)。"""
    from gui.pages.library_page import LibraryPage

    store = LibraryStore(tmp_path / "library.json")
    for name in folders:
        store.create_folder(name)
    for source_id, url, title, ctype, folder in items:
        store.add(source_id, url, title, ctype, folder=folder)
    page = LibraryPage(library_store=store)
    page._rebuild = lambda: None
    return page, store


def test_clear_button_click_clears_folder_and_keeps_other_favorites(_qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    page, store = _page(
        tmp_path,
        items=(
            ("s1", "u1", "A", "comic", "漫画"),
            ("s1", "u2", "B", "comic", "漫画"),
            ("s1", "u3", "C", "novel", ""),
        ),
    )
    page.folder_combo.setCurrentText("漫画")
    page._sync_combos()
    prompts = []
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *args, **kwargs: prompts.append(args[2]) or QMessageBox.Yes),
    )
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *args, **kwargs: None))

    page.clear_fav_btn.click()

    assert len(prompts) == 1
    assert "收藏夹「漫画」" in prompts[0]
    assert "2 本收藏" in prompts[0]
    assert store.folder_items("漫画") == []
    assert {r["url"] for r in store.list_all()} == {"u3"}
    assert store.list_folders() == ["漫画"]


def test_clear_button_declined_keeps_every_favorite(_qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    page, store = _page(
        tmp_path,
        items=(
            ("s1", "u1", "A", "comic", "漫画"),
            ("s1", "u2", "B", "novel", ""),
        ),
    )
    page.folder_combo.setCurrentText("全部")
    page._sync_combos()
    informed = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *args, **kwargs: QMessageBox.No))
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *args, **kwargs: informed.append(args[2])),
    )
    calls = []
    page._shelf.favorite_clear_folder = lambda folder: calls.append(folder) or 0
    page._shelf.clear_all_favorites = lambda: calls.append("all") or 0

    page.clear_fav_btn.click()

    assert calls == []
    assert informed == []
    assert {r["url"] for r in store.list_all()} == {"u1", "u2"}


def test_clear_button_on_empty_scope_skips_confirmation(_qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    page, store = _page(tmp_path, items=())
    page.folder_combo.setCurrentText("漫画")
    page._sync_combos()
    assert page.clear_fav_btn.text() == "清空收藏夹「漫画」(0)"
    asked = []
    informed = []
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *args, **kwargs: asked.append(args[2]) or QMessageBox.Yes),
    )
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *args, **kwargs: informed.append(args[2])),
    )
    calls = []
    page._shelf.favorite_clear_folder = lambda folder: calls.append(folder) or 0
    page._shelf.clear_all_favorites = lambda: calls.append("all") or 0

    page.clear_fav_btn.click()

    assert asked == []
    assert len(informed) == 1
    assert "为空" in informed[0]
    assert calls == []
    assert store.list_folders() == ["漫画"]


def test_library_page_forwards_repository_into_shelf_service(_qapp, tmp_path):
    from gui.pages.library_page import LibraryPage

    repo = _FakeRepo()
    page = LibraryPage(
        library_store=LibraryStore(tmp_path / "library.json"),
        shelf_cache_repository=repo,
    )
    store = page._store
    store.create_folder("漫画")
    store.add("s1", "u1", "A", "comic", folder="漫画")

    assert page._shelf.favorite_clear_folder("漫画") == 1
    assert repo.cleared == ["u1"]
    assert repo.cached == {"u2", "u3", "unrelated"}


def test_main_window_build_library_injects_repository(_qapp, tmp_path, monkeypatch):
    import gui.app as app_module
    import gui.pages.library_page as library_module

    class _Settings:
        def get(self, section, key, default=None):
            return default

    class _Signal:
        def connect(self, callback):
            return None

    class _Page:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.open_epub_requested = _Signal()
            self.open_online_requested = _Signal()
            self.download_requested = _Signal()
            self.play_local_video_requested = _Signal()

    app = app_module.MainWindow.__new__(app_module.MainWindow)
    app.settings = _Settings()
    app.library_store = object()
    app.reading_progress = object()
    app.shelf_cache_repository = object()
    app.tabs = type("Tabs", (), {"setCurrentIndex": lambda self, index: None})()
    app._tab_index = {"reader": 0}
    app._ensure_library_store = lambda: app.library_store
    app._backfill_favorite_covers = lambda *args, **kwargs: None
    monkeypatch.setattr(app_module, "_app_base_dir", lambda: tmp_path)
    monkeypatch.setattr(library_module, "LibraryPage", _Page)

    app._build_library()

    assert app.library_page.kwargs["shelf_cache_repository"] is app.shelf_cache_repository
