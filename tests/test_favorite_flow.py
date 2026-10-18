"""收藏流程测试：分类弹窗 helper。"""

from __future__ import annotations

from pathlib import Path

import pytest

import gui.app as app_module
from framework.library_store import LibraryStore
from framework.content import Detail


@pytest.fixture()
def store(tmp_path: Path):
    return LibraryStore(tmp_path / "lib.json")


@pytest.fixture()
def make_app(monkeypatch):
    """构造轻量 App（object.__new__ 跳过 __init__），共享真实 _ensure_library_store。"""

    def build(store):
        app = app_module.MainWindow.__new__(app_module.MainWindow)

        class _FakeReader:
            def __init__(self):
                self.calls = []
                self._store = store

            def refresh_favorite_state(self):
                self.calls.append("refresh")

        class _FakeLibrary:
            def refresh(self):
                pass

        app.library_store = store
        app.library_page = _FakeLibrary()
        app.reader = _FakeReader()
        return app

    return build


def _detail(url="http://x/1", folder_hint=None):
    return Detail(
        source_id="src",
        content_type="novel",
        url=url,
        title="书",
        author="作者",
        tags=["免费"],
    )


# ------------------------------------------------------------------ #
# 新增收藏 → 分类弹窗
# ------------------------------------------------------------------ #
def test_add_into_folder(make_app, store, monkeypatch):
    app = make_app(store)
    monkeypatch.setattr(app_module, "_pick_favorite_folder", lambda *a, **k: "玄幻")
    app._on_favorite(_detail())
    assert store.has("http://x/1")
    assert store.get("http://x/1")["folder"] == "玄幻"
    assert store.list_folders() == ["玄幻"]


def test_add_default_folder(make_app, store, monkeypatch):
    app = make_app(store)
    monkeypatch.setattr(app_module, "_pick_favorite_folder", lambda *a, **k: "")
    app._on_favorite(_detail())
    assert store.has("http://x/1")
    assert store.get("http://x/1")["folder"] == ""


def test_add_cancelled_folder(make_app, store, monkeypatch):
    app = make_app(store)
    # 取消 = 返回 "" → 未归类
    monkeypatch.setattr(app_module, "_pick_favorite_folder", lambda *a, **k: "")
    app._on_favorite(_detail())
    assert store.get("http://x/1")["folder"] == ""


def test_remove_does_not_open_dialog(make_app, store, monkeypatch):
    app = make_app(store)
    store.add("src", "http://x/1", "书")
    calls = []

    def fake(*a, **k):
        calls.append("called")
        return "玄幻"

    monkeypatch.setattr(app_module, "_pick_favorite_folder", fake)
    app._on_favorite(_detail())
    assert not store.has("http://x/1")  # remove，不弹窗
    assert calls == []


def test_preserves_author_tags(make_app, store, monkeypatch):
    app = make_app(store)
    monkeypatch.setattr(app_module, "_pick_favorite_folder", lambda *a, **k: "玄幻")
    detail = _detail()
    app._on_favorite(detail)
    rec = store.get("http://x/1")
    assert rec["author"] == "作者"
    assert rec["tags"] == ["免费"]
    assert rec["content_type"] == "novel"
