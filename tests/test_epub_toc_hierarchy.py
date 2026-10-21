# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt

from framework import epub_builder
from framework.epub_reader import FastEpub
from gui.pages.reader import epub_view as ev


def _build(tmp_path):
    out = tmp_path / "toc.epub"
    epub_builder.build_novel_epub(
        "书", out,
        [("卷一", "a"), ("第一章", "b"), ("第一节", "c"), ("卷二", "d")],
        depths=[0, 1, 2, 0],
    )
    return out


def test_fast_epub_reads_chapter_depths(tmp_path):
    epub = FastEpub(str(_build(tmp_path)))
    try:
        chapters = ev.EpubView._build_chapters(epub, epub.path)
    finally:
        epub.close()

    assert [ch.title for ch in chapters] == ["卷一", "第一章", "第一节", "卷二"]
    assert [ch.depth for ch in chapters] == [0, 1, 2, 0]


def test_toc_tree_nests_chapters_by_depth(_qapp, tmp_path):
    epub = FastEpub(str(_build(tmp_path)))
    try:
        chapters = ev.EpubView._build_chapters(epub, epub.path)
    finally:
        epub.close()

    view = ev.EpubView()
    view._chapters = chapters
    view._populate_toc()

    roots = [view.toc_list.topLevelItem(i) for i in range(view.toc_list.topLevelItemCount())]
    assert [node.text(0) for node in roots] == ["卷一", "卷二"]
    first = roots[0]
    assert first.childCount() == 1
    section = first.child(0)
    assert section.text(0) == "第一章"
    assert section.childCount() == 1
    assert section.child(0).text(0) == "第一节"
    assert first.data(0, Qt.UserRole) == 0
    assert section.data(0, Qt.UserRole) == 1
    assert section.child(0).data(0, Qt.UserRole) == 2
    assert roots[1].data(0, Qt.UserRole) == 3
    view.deleteLater()


def test_nav_depths_from_nested_ol():
    epub = FastEpub.__new__(FastEpub)
    nav = (
        "<html xmlns='http://www.w3.org/1999/xhtml' "
        "xmlns:epub='http://www.idpf.org/2007/ops'><body>"
        "<nav epub:type='toc'><ol>"
        "<li><a href='a.xhtml'>A</a><ol>"
        "<li><a href='b.xhtml'>B</a><ol><li><a href='c.xhtml'>C</a></li></ol></li>"
        "</ol></li>"
        "<li><a href='d.xhtml'>D</a></li>"
        "</ol></nav></body></html>"
    )

    depths = epub._nav_depths(nav, "EPUB")

    assert depths["EPUB/a.xhtml"] == 0
    assert depths["a.xhtml"] == 0
    assert depths["EPUB/b.xhtml"] == 1
    assert depths["EPUB/c.xhtml"] == 2
    assert depths["EPUB/d.xhtml"] == 0
