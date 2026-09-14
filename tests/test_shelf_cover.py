# -*- coding: utf-8 -*-
"""书架封面修复测试（test_shelf_cover.py）。

覆盖：
- LibraryStore.set_cover：后台补写封面只改 cover、保留收藏时间（旧收藏缺封面兜底）
- 阅读器收藏：_on_favorite_clicked 复用当前完整详情（cover/author/tags），
  不再构造空封面 Detail（书架卡片空白根因）；详情未就绪时降级为最小 Detail 不报错
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------- #
# LibraryStore.set_cover
# ---------------------------------------------------------------------- #
def test_store_set_cover_preserves_meta(tmp_path):
    from framework.library_store import LibraryStore

    store = LibraryStore(tmp_path / "lib.json")
    store.add("src", "http://a/1", "标题", content_type="novel", cover="",
              author="作者", tags=["x"])
    rec = store.get("http://a/1")
    favorited_at = rec["favorited_at"]

    ok = store.set_cover("http://a/1", "http://a/1/c.jpg")
    assert ok
    rec = store.get("http://a/1")
    assert rec["cover"] == "http://a/1/c.jpg"
    assert rec["title"] == "标题"
    assert rec["author"] == "作者"
    # 补封面不刷新收藏时间（书架排序「最近添加」不受补写影响）
    assert rec["favorited_at"] == favorited_at


def test_store_set_cover_missing_noop(tmp_path):
    from framework.library_store import LibraryStore

    store = LibraryStore(tmp_path / "lib.json")
    assert store.set_cover("http://n/x", "http://c/1.jpg") is False
    assert store.set_cover("", "http://c/1.jpg") is False
    assert store.set_cover("http://n/x", "") is False


# ---------------------------------------------------------------------- #
# 阅读器收藏带完整详情
# ---------------------------------------------------------------------- #
_HOLDERS = []


def _mk_reader(title: str):
    """构造轻量 ReaderPage：__new__ 跳过 VLC/Qt 初始化，用 holder 保活信号源。"""
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QLabel

    from gui.pages.reader_page import ReaderPage

    class _SigHolder(QObject):
        sig = Signal(object)

    holder = _SigHolder()
    _HOLDERS.append(holder)  # 无 parent 的 QObject 须保持 Python 引用防 GC

    reader = ReaderPage.__new__(ReaderPage)
    reader.title_label = QLabel(title)
    reader.favorite_requested = holder.sig
    return reader


def test_favorite_clicked_emits_full_detail(_qapp):
    """阅读器收藏 → Detail 带 cover/author/tags（书架卡片不再空白）。"""
    from framework.content import Detail

    full = Detail(
        source_id="demo",
        content_type="comic",
        url="http://demo/book.html",
        title="作品",
        author="作者A",
        cover="http://demo/c.jpg",
        tags=["a", "b"],
    )
    reader = _mk_reader(full.title)
    reader._current_source_id = full.source_id
    reader._current_book_url = full.url
    reader._current_content_type = full.content_type
    reader._current_detail = full

    got = []
    reader.favorite_requested.connect(got.append)
    reader._on_favorite_clicked()
    assert got, "应发出收藏信号"
    d = got[0]
    assert d.url == full.url
    assert d.cover == "http://demo/c.jpg"
    assert d.author == "作者A"
    assert d.tags == ["a", "b"]


def test_favorite_clicked_fallback_minimal(_qapp):
    """详情未就绪（无 _current_detail）→ 退化构造最小 Detail，不抛错。"""
    reader = _mk_reader("作品")
    reader._current_source_id = "demo"
    reader._current_book_url = "http://demo/book.html"
    reader._current_content_type = "novel"
    reader._current_detail = None

    got = []
    reader.favorite_requested.connect(got.append)
    reader._on_favorite_clicked()
    assert got, "应发出收藏信号"
    d = got[0]
    assert d.url == "http://demo/book.html"
    assert d.cover == ""
    assert d.title == "作品"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))