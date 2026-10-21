# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework import epub_builder
from framework.downloader import Downloader
from framework.epub_builder import EpubBuilder, _para_html
from framework.download_queue import DownloadTask


class _Settings:
    def __init__(self, output_dir="."):
        self.output_dir = output_dir

    def get(self, section, key, default=None):
        if section == "download" and key == "output_dir":
            return self.output_dir
        return default


def test_paragraph_html_strips_source_indent_to_avoid_double_indent():
    assert _para_html("　　第一段\n\n第二段　") == "<p>第一段</p>\n<p>第二段</p>"


def test_novel_chapter_html_keeps_body_and_style_has_two_character_indent():
    b = EpubBuilder("书")
    b.add_novel_chapter("第一章", "　　第一段\n第二段", 0)

    html = b.chapters[0]["html"]
    assert "<p>第一段</p>" in html
    assert "<p>第二段</p>" in html
    assert "<h1>第一章</h1>" in html
    assert "text-indent: 2em" in epub_builder._CHAPTER_STYLE
    assert "text-indent: 0" in epub_builder._CHAPTER_STYLE


def test_toc_tree_nests_chapters_by_depth():
    chapters = [
        {"title": "一", "file": "a", "depth": 0},
        {"title": "一甲", "file": "b", "depth": 1},
        {"title": "一甲一", "file": "c", "depth": 2},
        {"title": "一乙", "file": "d", "depth": 1},
        {"title": "二", "file": "e", "depth": 0},
    ]

    tree = EpubBuilder._toc_tree(chapters)

    def shape(nodes):
        return [
            (n["chapter"]["title"], shape(n["children"])) for n in nodes
        ]

    assert shape(tree) == [
        ("一", [("一甲", [("一甲一", [])]), ("一乙", [])]),
        ("二", []),
    ]


def test_built_epub_writes_nested_toc_and_indent_style(tmp_path):
    import pytest

    epub = pytest.importorskip("ebooklib.epub")
    out = tmp_path / "book.epub"

    epub_builder.build_novel_epub(
        "书", out,
        [("一", "　　甲"), ("一甲", "乙"), ("二", "丙")],
        depths=[0, 1, 0],
    )

    book = epub.read_epub(str(out))
    titles = []
    for entry in book.toc:
        if isinstance(entry, tuple):
            titles.append((entry[0].title, [c.title for c in entry[1]]))
        else:
            titles.append(entry.title)
    assert titles == [("一", ["一甲"]), "二"]

    import zipfile

    with zipfile.ZipFile(out) as zf:
        first = zf.read("EPUB/ch_0001.xhtml").decode("utf-8")
        style = zf.read("EPUB/style.css").decode("utf-8")
    assert "<p>甲</p>" in first
    assert "text-indent: 2em" in style


def _task(count):
    chapters = [
        SimpleNamespace(title=f"Chapter {index}", url=f"https://example.test/{index}",
                        depth=0)
        for index in range(count)
    ]
    return DownloadTask(
        task_id="task-1",
        source_id="source-1",
        content_type="novel",
        url="https://example.test/book",
        title="Book",
        chapters=chapters,
        selected=[True] * count,
        total=count,
    )


def test_downloader_keeps_chapter_depth_aligned_with_materials():
    task = _task(3)
    downloader = Downloader(None, None, _Settings())

    downloader._store_epub_chapter(task, 0, "一", "b0", depth=0)
    downloader._store_epub_chapter(task, 1, "一甲", "b1", depth=1)
    downloader._store_epub_chapter(task, 2, "二", "b2", depth=0)

    assert downloader._ordered_epub_depths(task) == [0, 1, 0]


def test_finalize_passes_depths_to_novel_builder(tmp_path, monkeypatch):
    task = _task(3)
    task.epub_chapters = [("一", "b0"), ("一甲", "b1"), ("二", "b2")]
    task.epub_depths = [0, 1, 0]
    task.done_chapters = [0, 1, 2]
    downloader = Downloader(None, None, _Settings(tmp_path))
    captured = {}

    def fake_builder(title, output, chapters, merge=False, depths=None):
        captured["depths"] = depths

    monkeypatch.setattr(epub_builder, "build_novel_epub", fake_builder)

    downloader.finalize_epub(task)

    assert captured["depths"] == [0, 1, 0]
