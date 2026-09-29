import json

import pytest

from framework.library_store import LibraryStore


def _write_backup(tmp_path, data, name="backup.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_inspect_rejects_non_dict(tmp_path):
    st = LibraryStore(tmp_path / "library.json")
    bad = _write_backup(tmp_path, ["not", "a", "dict"])
    res = st.inspect_backup(bad)
    assert res["ok"] is False
    assert res["error"]


def test_inspect_rejects_missing_favorites(tmp_path):
    st = LibraryStore(tmp_path / "library.json")
    bad = _write_backup(tmp_path, {"folders": []})
    assert st.inspect_backup(bad)["ok"] is False


def test_inspect_rejects_bad_json(tmp_path):
    st = LibraryStore(tmp_path / "library.json")
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    assert st.inspect_backup(p)["ok"] is False


def test_inspect_counts_valid(tmp_path):
    st = LibraryStore(tmp_path / "library.json")
    p = _write_backup(tmp_path, {
        "favorites": {"u1": {"url": "u1", "title": "A"}},
        "folders": ["X"],
    })
    res = st.inspect_backup(p)
    assert res["ok"] is True
    assert res["favorites"] == 1
    assert res["folders"] == 1


def test_export_import_roundtrip_preserves_records_and_locks(tmp_path):
    st = LibraryStore(tmp_path / "library.json")
    st.create_folder("漫画")
    st.create_folder("私密", locked=True, pw="h", salt="s")
    st.add("s1", "u1", "A", "comic", folder="漫画")
    out = tmp_path / "out.json"
    st.export_backup(out)

    st2 = LibraryStore(tmp_path / "library2.json")
    res = st2.import_backup(out, mode="replace")
    assert res["imported"] == 1
    rec = st2.get("u1")
    assert rec["title"] == "A"
    assert st2.list_folders() == sorted(["私密", "漫画"])
    info = st2.folder_info("私密")
    assert info is not None
    assert info["locked"] is True
    assert info["pw"] == "h"
    assert info["salt"] == "s"


def test_import_accepts_legacy_string_folders(tmp_path):
    st = LibraryStore(tmp_path / "library.json")
    p = _write_backup(tmp_path, {"favorites": {}, "folders": ["旧夹", "默认"]})
    res = st.import_backup(p, mode="merge")
    assert res["folders_added"] == 2
    assert sorted(st.list_folders()) == sorted(["旧夹", "默认"])
    assert st.folder_info("旧夹")["locked"] is False


def test_merge_keeps_earlier_favorited_at(tmp_path):
    st = LibraryStore(tmp_path / "library.json")
    st.add("s1", "u1", "旧标题", "comic")
    st._data["u1"]["favorited_at"] = "2020-01-01T00:00:00"

    p = _write_backup(tmp_path, {
        "favorites": {"u1": {
            "url": "u1", "source_id": "s1", "title": "新标题",
            "content_type": "comic", "favorited_at": "2026-01-01T00:00:00",
        }},
        "folders": ["新夹"],
    })
    res = st.import_backup(p, mode="merge")
    assert res["imported"] == 1
    rec = st.get("u1")
    assert rec["title"] == "新标题"
    assert rec["favorited_at"] == "2020-01-01T00:00:00"
    assert "新夹" in st.list_folders()


def test_merge_does_not_overwrite_existing_locked_folder_metadata(tmp_path):
    st = LibraryStore(tmp_path / "library.json")
    st.create_folder("私密", locked=True, pw="local", salt="local-salt")
    st.add("s1", "u1", "本地", "comic", folder="私密")
    p = _write_backup(tmp_path, {
        "favorites": {"u1": {"url": "u1", "title": "导入", "folder": "公开"}},
        "folders": [{"name": "私密", "locked": False, "pw": None, "salt": None}],
    })

    st.import_backup(p, mode="merge")
    assert st.folder_info("私密") == {
        "name": "私密", "locked": True, "pw": "local", "salt": "local-salt",
    }
    assert st.get("u1")["folder"] == "私密"


def test_replace_clears_existing(tmp_path):
    st = LibraryStore(tmp_path / "library.json")
    st.add("s1", "old", "旧书", "comic")
    p = _write_backup(tmp_path, {
        "favorites": {"new": {"url": "new", "title": "新书", "content_type": "comic"}},
        "folders": [],
    })
    st.import_backup(p, mode="replace")
    assert st.list_all() == [st.get("new")]


def test_import_failure_leaves_data_intact(tmp_path):
    st = LibraryStore(tmp_path / "library.json")
    st.add("s1", "u1", "A", "comic")
    p = _write_backup(tmp_path, {"favorites": "not-a-dict"})
    with pytest.raises(ValueError):
        st.import_backup(p, mode="replace")
    assert st.get("u1") is not None


def test_import_skips_bad_records_without_partial_mutation(tmp_path):
    st = LibraryStore(tmp_path / "library.json")
    st.add("s1", "u1", "本地", "comic")
    p = _write_backup(tmp_path, {
        "favorites": {"u2": "bad", "u3": {"url": "u3", "title": "新"}},
        "folders": [{"name": "新夹"}],
    })

    result = st.import_backup(p, mode="replace")
    assert result == {"imported": 1, "skipped": 1, "folders_added": 1}
    assert st.get("u1") is None
    assert st.get("u3")["title"] == "新"


def test_import_rejects_unknown_mode_without_changes(tmp_path):
    st = LibraryStore(tmp_path / "library.json")
    st.add("s1", "u1", "A", "comic")
    with pytest.raises(ValueError):
        st.import_backup(tmp_path / "missing.json", mode="other")
    assert st.get("u1") is not None
