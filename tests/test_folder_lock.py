import json

from framework.library_store import LibraryStore


def _store(tmp_path):
    return LibraryStore(tmp_path / "data" / "library.json")


def test_legacy_string_folders_load_as_unlocked(tmp_path):
    path = tmp_path / "data" / "library.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"favorites": {}, "folders": ["默认", "漫画"]}, ensure_ascii=False),
        encoding="utf-8",
    )

    store = LibraryStore(path)

    assert sorted(store.list_folders()) == ["漫画", "默认"]
    assert store.folder_info("默认")["locked"] is False
    assert store.folder_info("默认")["pw"] is None
    assert store.set_folder_lock("默认", True, "H", "S") is True
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["folders"] == [
        {"name": "漫画", "locked": False, "pw": None, "salt": None},
        {"name": "默认", "locked": True, "pw": "H", "salt": "S"},
    ]


def test_new_format_roundtrips(tmp_path):
    store = _store(tmp_path)

    store.create_folder("私密", locked=True, pw="HASH", salt="SALT")

    data = json.loads(
        (tmp_path / "data" / "library.json").read_text(encoding="utf-8")
    )
    assert data["folders"] == [
        {"name": "私密", "locked": True, "pw": "HASH", "salt": "SALT"}
    ]
    reloaded = _store(tmp_path)
    assert reloaded.folder_info("私密") == data["folders"][0]


def test_folder_info_unknown_returns_none(tmp_path):
    assert _store(tmp_path).folder_info("nope") is None


def test_folder_info_returns_defensive_copy(tmp_path):
    store = _store(tmp_path)
    store.create_folder("A", locked=True, pw="H", salt="S")

    info = store.folder_info("A")
    info["locked"] = False
    info["pw"] = "changed"

    assert store.folder_info("A") == {
        "name": "A",
        "locked": True,
        "pw": "H",
        "salt": "S",
    }


def test_load_normalizes_malformed_lock_schema(tmp_path):
    path = tmp_path / "data" / "library.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "favorites": {},
                "folders": [
                    {
                        "name": "A",
                        "locked": "yes",
                        "pw": {"hash": "H"},
                        "salt": ["S"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = LibraryStore(path)

    assert store.folder_info("A") == {
        "name": "A",
        "locked": False,
        "pw": None,
        "salt": None,
    }


def test_create_normalizes_malformed_lock_schema(tmp_path):
    store = _store(tmp_path)

    assert store.create_folder(" A ", locked=1, pw=["H"], salt={"value": "S"})
    assert store.folder_info("A") == {
        "name": "A",
        "locked": False,
        "pw": None,
        "salt": None,
    }


def test_set_normalizes_malformed_lock_schema(tmp_path):
    store = _store(tmp_path)
    store.create_folder("A", locked=True, pw="H", salt="S")

    assert store.set_folder_lock(" A ", "yes", {"hash": "H"}, ["S"])
    assert store.folder_info("A") == {
        "name": "A",
        "locked": False,
        "pw": None,
        "salt": None,
    }


def test_legacy_folder_names_are_canonicalized(tmp_path):
    path = tmp_path / "data" / "library.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "favorites": {},
                "folders": [
                    " A ",
                    "   ",
                    {"name": " B ", "locked": True, "pw": "H", "salt": "S"},
                    {"name": "  ", "locked": True, "pw": "bad", "salt": "bad"},
                ],
            }
        ),
        encoding="utf-8",
    )

    store = LibraryStore(path)

    assert store.list_folders() == ["A", "B"]
    assert store.folder_info(" A ")["name"] == "A"
    assert store.folder_info(" B ")["name"] == "B"
    assert store.delete_folder(" A ") is True
    reloaded = LibraryStore(path)
    assert reloaded.list_folders() == ["B"]
    assert reloaded.folder_info("A") is None


def test_mixed_legacy_folder_state_canonicalizes_favorites_and_persists(tmp_path):
    path = tmp_path / "data" / "library.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "favorites": {
                    "u1": {
                        "url": "u1",
                        "title": "One",
                        "folder": " A ",
                        "tags": ["keep"],
                    },
                    "u2": {
                        "url": "u2",
                        "title": "Two",
                        "folder": " A ",
                        "cover": "cover.jpg",
                    },
                },
                "folders": [
                    {"name": " A ", "locked": True, "pw": "H", "salt": "S"}
                ],
            }
        ),
        encoding="utf-8",
    )

    store = LibraryStore(path)

    assert store.list_folders() == ["A"]
    assert {item["url"] for item in store.folder_items("A")} == {"u1", "u2"}
    assert store.get("u1")["tags"] == ["keep"]
    assert store.get("u2")["cover"] == "cover.jpg"

    assert store.rename_folder(" A ", " B ") is True
    renamed = LibraryStore(path)
    assert renamed.list_folders() == ["B"]
    assert renamed.folder_info("B") == {
        "name": "B",
        "locked": True,
        "pw": "H",
        "salt": "S",
    }
    assert renamed.get("u1")["folder"] == "B"
    assert renamed.get("u2")["folder"] == "B"
    assert renamed.get("u1")["tags"] == ["keep"]
    assert renamed.get("u2")["cover"] == "cover.jpg"

    assert renamed.delete_folder(" B ") is True
    deleted = LibraryStore(path)
    assert deleted.list_folders() == []
    assert deleted.get("u1")["folder"] == ""
    assert deleted.get("u2")["folder"] == ""
    assert deleted.get("u1")["tags"] == ["keep"]
    assert deleted.get("u2")["cover"] == "cover.jpg"


def test_set_and_clear_folder_lock(tmp_path):
    store = _store(tmp_path)
    store.create_folder("A")

    assert store.set_folder_lock("A", True, "H", "S") is True
    info = store.folder_info("A")
    assert (info["locked"], info["pw"], info["salt"]) == (True, "H", "S")
    assert store.clear_folder_lock("A") is True
    info = store.folder_info("A")
    assert (info["locked"], info["pw"], info["salt"]) == (False, None, None)


def test_set_folder_lock_unknown_returns_false(tmp_path):
    assert _store(tmp_path).set_folder_lock("nope", True, "H", "S") is False


def test_rename_folder_preserves_lock(tmp_path):
    store = _store(tmp_path)
    store.create_folder("旧", locked=True, pw="H", salt="S")
    store.add("src", "https://example.test/1", "书", folder="旧")

    assert store.rename_folder("旧", "新") is True
    reloaded = _store(tmp_path)
    assert reloaded.folder_info("旧") is None
    info = reloaded.folder_info("新")
    assert info["locked"] is True and info["pw"] == "H"
    assert reloaded.get("https://example.test/1")["folder"] == "新"


def test_delete_folder_removes_lock_state(tmp_path):
    store = _store(tmp_path)
    store.create_folder("X", locked=True, pw="H", salt="S")
    store.add("src", "https://example.test/1", "书", folder="X")

    assert store.delete_folder("X") is True
    reloaded = _store(tmp_path)
    assert reloaded.folder_info("X") is None
    assert reloaded.get("https://example.test/1")["folder"] == ""
    assert reloaded.delete_folder("X") is False


def test_delete_implicit_folder_preserves_favorite_and_persists(tmp_path):
    store = _store(tmp_path)
    store.add("src", "https://example.test/1", "书", folder="implicit")

    assert store.list_folders() == ["implicit"]
    assert store.delete_folder("implicit") is True
    reloaded = _store(tmp_path)
    assert reloaded.get("https://example.test/1")["folder"] == ""
    assert reloaded.list_folders() == []
    assert reloaded.delete_folder("implicit") is False


def test_export_backup_emits_folder_objects_with_lock_metadata(tmp_path):
    store = _store(tmp_path)
    store.create_folder("公开")
    store.create_folder("私密", locked=True, pw="H", salt="S")

    output = store.export_backup(tmp_path / "backup" / "library.json")

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["folders"] == [
        {"name": "公开", "locked": False, "pw": None, "salt": None},
        {"name": "私密", "locked": True, "pw": "H", "salt": "S"},
    ]


def test_relocking_folder_discards_process_unlock(_qapp, tmp_path, monkeypatch):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("私密")
    page = LibraryPage(library_store=store)
    page._unlocked.add("私密")
    monkeypatch.setattr(page, "_prompt_unlock", lambda name: False)
    monkeypatch.setattr("PySide6.QtWidgets.QInputDialog.getText", lambda *args: ("secret", True))

    assert page._set_folder_lock("私密") is True
    assert page.is_folder_unlocked("私密") is False


def test_clear_locked_folder_requires_unlock_and_cancel_preserves_items(_qapp, tmp_path, monkeypatch):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage
    from PySide6.QtWidgets import QMessageBox

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("私密", locked=True, pw="H", salt="S")
    store.add("src", "u1", "书一", "novel", folder="私密")
    page = LibraryPage(library_store=store)
    page.folder_combo.setCurrentText("私密")
    monkeypatch.setattr(page, "_prompt_unlock", lambda name: False)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)

    page._clear_folder()

    assert len(store.folder_items("私密")) == 1


def test_selecting_locked_folder_requires_unlock_and_restores_previous_selection(
    _qapp, tmp_path, monkeypatch
):
    from framework.folder_lock import hash_password, new_salt
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    salt = new_salt()
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("公开")
    store.create_folder("私密", locked=True, pw=hash_password("secret", salt), salt=salt)
    store.add("src", "u1", "秘密", "novel", folder="私密")
    page = LibraryPage(library_store=store)
    page.folder_combo.setCurrentText("公开")
    rebuilds = []
    page._rebuild = lambda: rebuilds.append(page.folder_combo.currentText())
    monkeypatch.setattr(page, "_prompt_unlock", lambda name: False)

    page.folder_combo.setCurrentText("私密")

    assert page.folder_combo.currentText() == "公开"
    assert rebuilds == []


def test_delete_locked_folder_requires_unlock_and_moves_favorites_to_unfiled(
    _qapp, tmp_path, monkeypatch
):
    from framework.folder_lock import hash_password, new_salt
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage
    from PySide6.QtWidgets import QMessageBox

    salt = new_salt()
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("私密", locked=True, pw=hash_password("secret", salt), salt=salt)
    store.add("src", "u1", "秘密", "novel", folder="私密")
    page = LibraryPage(library_store=store)
    monkeypatch.setattr(page, "_prompt_unlock", lambda name: True)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    monkeypatch.setattr(page, "_rebuild", lambda: None)
    page.folder_combo.setCurrentText("私密")

    page._delete_folder()

    assert store.folder_info("私密") is None
    assert store.get("u1")["folder"] == ""


def test_successful_combo_unlock_reuses_session_unlock(_qapp, tmp_path, monkeypatch):
    from framework.folder_lock import hash_password, new_salt
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    salt = new_salt()
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("私密", locked=True, pw=hash_password("secret", salt), salt=salt)
    store.add("src", "u1", "秘密", "novel", folder="私密")
    page = LibraryPage(library_store=store)
    prompts = []
    rebuilds = []
    page._rebuild = lambda: rebuilds.append(page.folder_combo.currentText())
    def unlock(name):
        prompts.append(name)
        page._unlocked.add(name)
        return True
    monkeypatch.setattr(page, "_prompt_unlock", unlock)

    page.folder_combo.setCurrentText("私密")
    page.folder_combo.setCurrentText("全部")
    page.folder_combo.setCurrentText("私密")

    assert prompts == ["私密"]
    assert rebuilds == ["私密", "全部", "私密"]
    assert page.is_folder_unlocked("私密") is True


def test_locked_target_move_does_not_prompt_but_locked_source_does(
    _qapp, tmp_path, monkeypatch
):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("源", locked=True, pw="H", salt="S")
    store.create_folder("目标", locked=True, pw="H", salt="S")
    store.add("src", "u1", "书", "novel", folder="")
    page = LibraryPage(library_store=store)
    prompts = []
    monkeypatch.setattr(page, "_prompt_unlock", lambda name: prompts.append(name) or False)
    monkeypatch.setattr(page, "_rebuild", lambda: None)

    page._move_favorite({"url": "u1", "folder": ""}, "目标")

    assert store.get("u1")["folder"] == "目标"
    assert prompts == []

    page._move_favorite({"url": "u1", "folder": "源"}, "目标")

    assert store.get("u1")["folder"] == "目标"
    assert prompts == ["源"]


def test_delete_cancel_happens_before_unlock_and_preserves_data(_qapp, tmp_path, monkeypatch):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage
    from PySide6.QtWidgets import QMessageBox

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("私密", locked=True, pw="H", salt="S")
    store.add("src", "u1", "秘密", "novel", folder="私密")
    page = LibraryPage(library_store=store)
    prompts = []
    monkeypatch.setattr(page, "_prompt_unlock", lambda name: prompts.append(name) or True)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.No)
    page.folder_combo.blockSignals(True)
    page.folder_combo.setCurrentText("私密")
    page.folder_combo.blockSignals(False)

    page._delete_folder()

    assert prompts == []
    assert store.folder_info("私密") is not None
    assert store.get("u1")["folder"] == "私密"


def test_delete_unlock_cancel_preserves_data_after_confirmation(_qapp, tmp_path, monkeypatch):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage
    from PySide6.QtWidgets import QMessageBox

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("私密", locked=True, pw="H", salt="S")
    store.add("src", "u1", "秘密", "novel", folder="私密")
    page = LibraryPage(library_store=store)
    monkeypatch.setattr(page, "_prompt_unlock", lambda name: False)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    page.folder_combo.blockSignals(True)
    page.folder_combo.setCurrentText("私密")
    page.folder_combo.blockSignals(False)

    page._delete_folder()

    assert store.folder_info("私密") is not None
    assert store.get("u1")["folder"] == "私密"


def test_clear_all_requires_unlock_for_locked_folders(_qapp, tmp_path, monkeypatch):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage
    from PySide6.QtWidgets import QMessageBox

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("私密", locked=True, pw="H", salt="S")
    store.add("src", "u1", "秘密", "novel", folder="私密")
    store.add("src", "u2", "公开", "novel", folder="")
    page = LibraryPage(library_store=store)
    prompts = []
    monkeypatch.setattr(page, "_prompt_unlock", lambda name: prompts.append(name) or False)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    page.folder_combo.blockSignals(True)
    page.folder_combo.setCurrentText("全部")
    page.folder_combo.blockSignals(False)

    page._clear_folder()

    assert prompts == ["私密"]
    assert store.get("u1") is not None
    assert store.get("u2") is not None


def test_all_view_hides_locked_folder_placeholder_title_count_and_contents(_qapp, tmp_path):
    from framework.folder_lock import hash_password, new_salt
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    salt = new_salt()
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("私密", locked=True, pw=hash_password("secret", salt), salt=salt)
    store.add("src", "u1", "秘密", "novel", folder="私密")
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
        {"kind": "local", "title": "本地秘密", "folder": "私密"},
        {"kind": "favorite", "title": "收藏秘密", "folder": "私密"},
    ])

    rendered = [item["rec"]["title"] for _, items in captured for item in items]
    assert rendered == []
    assert captured == []
    assert page.count_label.text() == "书架还空着"


def test_clear_locked_folder_requires_unlock_and_success_clears_items(_qapp, tmp_path, monkeypatch):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage
    from PySide6.QtWidgets import QMessageBox

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("私密", locked=True, pw="H", salt="S")
    store.add("src", "u1", "书一", "novel", folder="私密")
    page = LibraryPage(library_store=store)
    page.folder_combo.setCurrentText("私密")
    monkeypatch.setattr(page, "_prompt_unlock", lambda name: True)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)

    page._clear_folder()

    assert store.folder_items("私密") == []


def test_locked_placeholder_respects_folder_type_and_search_filters(_qapp, tmp_path):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage
    from framework.folder_lock import hash_password, new_salt

    store = LibraryStore(tmp_path / "library.json")
    for name, title, content_type in (("小说", "秘密小说", "novel"), ("漫画", "秘密漫画", "comic")):
        salt = new_salt()
        store.create_folder(name, locked=True, pw=hash_password("s", salt), salt=salt)
        store.add("src", name, title, content_type, folder=name)
    page = LibraryPage(library_store=store)
    page._unlocked.clear()
    captured = []
    page._add_group = lambda title, items: captured.append((title, items))
    page._maybe_backfill_covers = lambda favorites: None
    page._last_ctype = "novel"
    page._last_folder = "小说"
    page._last_kw = "秘密"
    page._last_sort = "recent"
    page.folder_combo.setCurrentText("小说")
    page.type_combo.setCurrentText("小说")
    page.search_edit.setText("秘密")
    page._visible_combo_state = lambda: True

    page._render([])

    rendered = [item["rec"]["title"] for _, items in captured for item in items]
    assert rendered == ["🔒 小说"]


def test_password_hash_is_deterministic_and_salt_dependent():
    from framework.folder_lock import hash_password, new_salt, verify_password

    salt = new_salt()
    h1 = hash_password("pw", salt)
    h2 = hash_password("pw", salt)
    h3 = hash_password("pw", new_salt())
    assert h1 == h2
    assert h1 != h3
    assert verify_password("pw", h1, salt) is True
    assert verify_password("wrong", h1, salt) is False


def test_verify_password_rejects_empty_inputs():
    from framework.folder_lock import verify_password

    assert verify_password("pw", None, "abc") is False
    assert verify_password("pw", "x", None) is False


def test_unlock_flow_offscreen(_qapp, tmp_path):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage
    from framework.folder_lock import new_salt, hash_password

    salt = new_salt()
    pw = hash_password("secret", salt)
    store = LibraryStore(tmp_path / "library.json")
    store.add("src", "u1", "书一", "novel", folder="私密")
    store.create_folder("私密", locked=True, pw=pw, salt=salt)

    page = LibraryPage(library_store=store)
    page._unlocked.clear()
    assert page.is_folder_unlocked("私密") is False
    store.add("src", "u2", "书二", "novel", folder="私密")
    assert len(store.folder_items("私密")) == 2
    page._unlocked.add("私密")
    assert page.is_folder_unlocked("私密") is True


def test_locked_folder_renders_placeholder_not_contents(_qapp, tmp_path):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage
    from framework.folder_lock import new_salt, hash_password

    salt = new_salt()
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("私密", locked=True, pw=hash_password("s", salt), salt=salt)
    store.add("src", "u1", "书一", "novel", folder="私密")

    page = LibraryPage(library_store=store)
    page._unlocked.clear()
    assert page._has_locked_folders() is True
    captured = []
    page._add_group = lambda title, items: captured.append((title, items))
    page._maybe_backfill_covers = lambda favorites: None
    page._last_ctype = ""
    page._last_folder = "私密"
    page._last_kw = ""
    page._last_sort = "recent"
    page.folder_combo.blockSignals(True)
    page.folder_combo.setCurrentText("私密")
    page.folder_combo.blockSignals(False)
    page._render([{
        "kind": "favorite",
        "title": "书一",
        "folder": "私密",
        "source_id": "src",
        "url": "u1",
    }])
    rendered = [item["rec"] for _, items in captured for item in items]
    assert [rec["title"] for rec in rendered] == ["🔒 私密"]
    assert store.folder_items("私密")[0]["title"] == "书一"


def test_all_view_hides_locked_folder_placeholder_and_contents(_qapp, tmp_path):
    from framework.folder_lock import hash_password, new_salt
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    store = LibraryStore(tmp_path / "library.json")
    salt = new_salt()
    store.create_folder("私密", locked=True, pw=hash_password("s", salt), salt=salt)
    store.add("src", "locked-1", "秘密书", "novel", folder="私密")

    page = LibraryPage(library_store=store)
    page._unlocked.clear()
    page._last_ctype = ""
    page._last_folder = "全部"
    page._last_kw = ""
    page._last_sort = "recent"
    page._visible_combo_state = lambda: True
    captured = []
    page._add_group = lambda title, items: captured.append((title, items))
    page._maybe_backfill_covers = lambda favorites: None

    page._render([{
        "kind": "favorite",
        "title": "秘密书",
        "folder": "私密",
        "content_type": "novel",
    }])

    assert captured == []
    assert page.count_label.text() == "书架还空着"


def test_locked_contents_do_not_inflate_visible_count(_qapp, tmp_path):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage
    from framework.folder_lock import hash_password, new_salt

    store = LibraryStore(tmp_path / "library.json")
    salt = new_salt()
    store.create_folder("私密", locked=True, pw=hash_password("s", salt), salt=salt)
    store.add("src", "locked-1", "私密一", "novel", folder="私密")
    store.add("src", "locked-2", "私密二", "novel", folder="私密")
    store.add("src", "visible", "公开书", "novel")

    page = LibraryPage(library_store=store)
    page._unlocked.clear()
    page._last_ctype = ""
    page._last_folder = "全部"
    page._last_kw = ""
    page._last_sort = "recent"
    page._visible_combo_state = lambda: True
    page._add_group = lambda title, items: None
    page._maybe_backfill_covers = lambda favorites: None

    page._render([
        {"kind": "favorite", "title": "私密一", "folder": "私密", "content_type": "novel"},
        {"kind": "favorite", "title": "私密二", "folder": "私密", "content_type": "novel"},
        {"kind": "favorite", "title": "公开书", "folder": "", "content_type": "novel"},
    ])

    assert page.count_label.text() == "共 1 本 · 本地0 / 收藏1"


def test_set_password_requires_matching_confirmation(_qapp, tmp_path, monkeypatch):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("A")
    page = LibraryPage(library_store=store)

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    entered = iter(["aaa", "bbb"])
    monkeypatch.setattr(
        QInputDialog, "getText",
        staticmethod(lambda *a, **k: (next(entered), True)),
    )
    assert page._prompt_set_password("A", require_old=False) is False
    assert store.folder_info("A")["locked"] is False

    entered = iter(["ccc", "ccc"])
    assert page._prompt_set_password("A", require_old=False) is True
    assert store.folder_info("A")["locked"] is True


def test_unlock_then_relock_roundtrip(_qapp, tmp_path, monkeypatch):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage
    from framework.folder_lock import new_salt, hash_password
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    salt = new_salt()
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("P", locked=True, pw=hash_password("k", salt), salt=salt)
    page = LibraryPage(library_store=store)

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(
        QInputDialog, "getText",
        staticmethod(lambda *a, **k: ("k", True)),
    )
    assert page._prompt_unlock("P") is True
    assert page.is_folder_unlocked("P") is True
    page._lock_folder_now("P")
    assert page.is_folder_unlocked("P") is False


def test_new_locked_folder_uses_injected_shelf_store(_qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QCheckBox, QDialog, QInputDialog
    from framework.folder_lock import verify_password
    from framework.library_store import LibraryStore
    from framework.shelf_service import ShelfService
    from gui.pages.library_page import LibraryPage

    store = LibraryStore(tmp_path / "library.json")
    shelf = ShelfService(tmp_path / "downloads", library_store=store)
    page = LibraryPage(shelf_service=shelf)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(
        lambda *a, **k: ("secret", True)
    ))

    def accept_locked(dialog):
        for checkbox in dialog.findChildren(QCheckBox):
            checkbox.setChecked(True)
        return QDialog.Accepted

    monkeypatch.setattr(QDialog, "exec", accept_locked)
    page._new_folder()

    info = store.folder_info("secret")
    assert info["locked"] is True
    assert verify_password("secret", info["pw"], info["salt"]) is True


def test_context_menu_set_password_action(_qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    class Action:
        def __init__(self, text):
            self.text = text
            self.callback = None
            self.triggered = self

        def connect(self, callback):
            self.callback = callback

        def trigger(self):
            self.callback()

    class Menu:
        last = None

        def __init__(self, parent=None):
            self.actions = []
            Menu.last = self

        def addAction(self, text):
            action = Action(text)
            self.actions.append(action)
            return action

        def addSeparator(self):
            return None

        def addMenu(self, text):
            return self

        def exec(self, pos):
            return None

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("A")
    page = LibraryPage(library_store=store)
    monkeypatch.setattr(QMenu, "__new__", staticmethod(lambda cls, *a, **k: Menu()))
    monkeypatch.setattr(page, "_prompt_set_password", lambda name, require_old: calls.append((name, require_old)))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    calls = []

    page._show_card_menu({"kind": "favorite", "folder": "A", "url": "u"}, None)
    next(action for action in Menu.last.actions if action.text == "设置密码").trigger()
    assert calls == [("A", False)]


def test_context_menu_unlock_action(_qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMenu
    from framework.folder_lock import hash_password, new_salt
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    class Action:
        def __init__(self, text):
            self.text = text
            self.callback = None
            self.triggered = self

        def connect(self, callback):
            self.callback = callback

        def trigger(self):
            self.callback()

    class Menu:
        last = None

        def __init__(self, parent=None):
            self.actions = []
            Menu.last = self

        def addAction(self, text):
            action = Action(text)
            self.actions.append(action)
            return action

        def addSeparator(self):
            return None

        def exec(self, pos):
            return None

    salt = new_salt()
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("A", locked=True, pw=hash_password("k", salt), salt=salt)
    page = LibraryPage(library_store=store)
    calls = []
    monkeypatch.setattr(QMenu, "__new__", staticmethod(lambda cls, *a, **k: Menu()))
    monkeypatch.setattr(page, "_prompt_unlock", lambda name: calls.append(name) or True)

    page._show_card_menu({"kind": "favorite", "folder": "A", "url": "u"}, None)
    next(action for action in Menu.last.actions if action.text == "🔓 解锁收藏夹").trigger()
    assert calls == ["A"]
    assert [action.text for action in Menu.last.actions] == ["🔓 解锁收藏夹"]


def test_context_menu_remove_password_action(_qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMenu
    from framework.folder_lock import hash_password, new_salt
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    class Action:
        def __init__(self, text):
            self.text = text
            self.callback = None
            self.triggered = self

        def connect(self, callback):
            self.callback = callback

        def trigger(self):
            self.callback()

    class Menu:
        last = None

        def __init__(self, parent=None):
            self.actions = []
            Menu.last = self

        def addAction(self, text):
            action = Action(text)
            self.actions.append(action)
            return action

        def addSeparator(self):
            return None

        def addMenu(self, text):
            return self

        def exec(self, pos):
            return None

    salt = new_salt()
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("A", locked=True, pw=hash_password("k", salt), salt=salt)
    page = LibraryPage(library_store=store)
    page._unlocked.add("A")
    calls = []
    monkeypatch.setattr(QMenu, "__new__", staticmethod(lambda cls, *a, **k: Menu()))
    monkeypatch.setattr(page, "_remove_folder_password", lambda name: calls.append(name))

    page._show_card_menu({"kind": "favorite", "folder": "A", "url": "u"}, None)
    next(action for action in Menu.last.actions if action.text == "移除密码").trigger()
    assert calls == ["A"]


def test_prompt_unlock_wrong_password_does_not_unlock(_qapp, tmp_path, monkeypatch):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage
    from framework.folder_lock import new_salt, hash_password
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    salt = new_salt()
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("P", locked=True, pw=hash_password("k", salt), salt=salt)
    page = LibraryPage(library_store=store)

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(
        QInputDialog, "getText",
        staticmethod(lambda *a, **k: ("wrong", True)),
    )
    assert page._prompt_unlock("P") is False
    assert page.is_folder_unlocked("P") is False


def test_move_favorite_locked_folder_requires_unlock(_qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    from framework.folder_lock import hash_password, new_salt
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    salt = new_salt()
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("Private", locked=True, pw=hash_password("secret", salt), salt=salt)
    page = LibraryPage(library_store=store)
    calls = []
    monkeypatch.setattr(page._shelf, "favorite_move", lambda *args: calls.append(args))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    for answer in [("", False), ("wrong", True)]:
        page._unlocked.clear()
        monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, answer=answer, **k: answer))
        page._move_favorite({"url": "u1", "folder": "Private"}, "Public")

    assert calls == []


def test_remove_favorite_locked_folder_requires_unlock(_qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    from framework.folder_lock import hash_password, new_salt
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    salt = new_salt()
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("Private", locked=True, pw=hash_password("secret", salt), salt=salt)
    page = LibraryPage(library_store=store)
    calls = []
    monkeypatch.setattr(page._shelf, "favorite_remove", lambda *args: calls.append(args))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    for answer in [("", False), ("wrong", True)]:
        page._unlocked.clear()
        monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, answer=answer, **k: answer))
        page._remove_favorite({"url": "u1", "folder": "Private"})

    assert calls == []


def test_direct_favorite_mutations_preserve_unlocked_behavior(_qapp, tmp_path, monkeypatch):
    from framework.library_store import LibraryStore
    from gui.pages.library_page import LibraryPage

    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("Private", locked=True, pw="H", salt="S")
    page = LibraryPage(library_store=store)
    page._unlocked.add("Private")
    moved = []
    removed = []
    monkeypatch.setattr(page._shelf, "favorite_move", lambda *args: moved.append(args))
    monkeypatch.setattr(page._shelf, "favorite_remove", lambda *args: removed.append(args))

    page._move_favorite({"url": "u1", "folder": "Private"}, "Public")
    page._remove_favorite({"url": "u1", "folder": "Private"})

    assert moved == [("u1", "Public")]
    assert removed == [("u1",)]
