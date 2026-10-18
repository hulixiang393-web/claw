from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt

from framework.epub_reader import split_single_document
from gui.pages.reader import epub_view as ev


def test_split_single_document_preserves_legacy_shape_by_default():
    html = '<h1>第一章</h1><p>before<img src="images/a.jpg">after</p>'

    result = split_single_document(html)

    assert result == [("第一章", "", ["images/a.jpg"])]
    assert all(len(item) == 3 for item in result)


def test_split_single_document_returns_ordered_mixed_parts():
    html = '<h1>第一章</h1><p>before<img src="images/a.jpg">after</p><p>tail</p>'

    result = split_single_document(html, include_parts=True)

    assert result == [
        (
            "第一章",
            "before\nafter\ntail",
            ["images/a.jpg"],
            [("text", "before"), ("image", "images/a.jpg"), ("text", "after"), ("text", "tail")],
        )
    ]


def test_populate_toc_sets_zero_based_user_roles(_qapp):
    view = ev.EpubView()
    view._chapters = [ev._Chapter("一"), ev._Chapter("二")]

    view._populate_toc()

    assert [view.toc_list.item(i).data(Qt.UserRole) for i in range(2)] == [0, 1]
    view.deleteLater()


def test_toc_click_loads_valid_index_for_novel_and_comic_and_ignores_invalid(_qapp, monkeypatch):
    view = ev.EpubView()
    view._chapters = [ev._Chapter("一"), ev._Chapter("二")]
    calls = []
    monkeypatch.setattr(view, "_load_chapter", lambda index: calls.append(index))

    for kind in ("novel", "comic"):
        item = view.toc_list.item(0) if view.toc_list.count() else None
        view._populate_toc()
        item = view.toc_list.item(1)
        item.setData(Qt.UserRole, 1)
        view._on_toc_clicked(item)
        view._on_toc_clicked(SimpleNamespace(data=lambda role: None))
        view._on_toc_clicked(SimpleNamespace(data=lambda role: 99))

    assert calls == [1, 1]
    view.deleteLater()


def test_load_task_emits_mixed_kind_and_ordered_parts(monkeypatch):
    html = '<p>before<img src="images/a.jpg">after</p>'
    epub = SimpleNamespace()
    ch = ev._Chapter("一", src="text/chapter.xhtml")
    task = ev._LoadChapterTask(0, ch, epub)
    got = []
    task.signals.done.connect(lambda *args: got.append(args))
    monkeypatch.setattr(ev.EpubView, "_read_doc", staticmethod(lambda *_: html))
    monkeypatch.setattr(ev.EpubView, "_read_bytes", staticmethod(lambda *_: b"image"))

    task.run()

    assert got == [
        (0, "mixed", "before\nafter", [b"image"], [
            ("text", "before"), ("image", b"image"), ("text", "after")
        ])
    ]


def test_load_task_classifies_text_only_merged_chapter_as_novel(monkeypatch):
    ch = ev._Chapter("一", text="正文", content_parts=[("text", "正文")])
    task = ev._LoadChapterTask(0, ch, SimpleNamespace())
    got = []
    task.signals.done.connect(lambda *args: got.append(args))

    task.run()

    assert got == [(0, "novel", "正文", [], [("text", "正文")])]


def test_text_only_merged_chapter_uses_text_label(_qapp):
    view = ev.EpubView()
    view._chapters = [ev._Chapter("一")]
    view._current_idx = 0

    view._on_chapter_loaded(0, "novel", "正文", [], [("text", "正文")])

    assert view._is_comic is False
    assert view.scroll.widget() is view.text
    assert view.text.text() == "【一】\n\n正文"
    view.deleteLater()


def test_split_single_document_keeps_preface_without_repeating_first_h1():
    html = "<p>前言</p><h1>第一章</h1><p>正文</p><h1>第二章</h1><p>后文</p>"

    result = split_single_document(html, include_parts=True)

    assert result == [
        ("第一章", "前言\n正文", [], [("text", "前言"), ("text", "正文")]),
        ("第二章", "后文", [], [("text", "后文")]),
    ]


def test_load_task_keeps_text_parts_when_image_read_fails(monkeypatch):
    ch = ev._Chapter(
        "一",
        src="text/chapter.xhtml",
        content_parts=[("text", "before"), ("image", "images/missing.jpg"), ("text", "after")],
    )
    task = ev._LoadChapterTask(0, ch, SimpleNamespace())
    got = []
    task.signals.done.connect(lambda *args: got.append(args))
    monkeypatch.setattr(ev.EpubView, "_read_bytes", staticmethod(lambda *_: None))

    task.run()

    assert got == [(0, "mixed", "before\nafter", [], [("text", "before"), ("text", "after")])]
