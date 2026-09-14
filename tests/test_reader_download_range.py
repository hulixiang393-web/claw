# -*- coding: utf-8 -*-
"""阅读器下载章节范围选择测试（test_reader_download_range.py）。

需求：阅读界面点下载时弹章节范围框，默认「当前章→末章」。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QObject, Signal


# ------------------------------------------------------------------- #
# DownloadRangeDialog 默认范围
# ------------------------------------------------------------------- #
def test_default_start_shows_range_with_correct_start(_qapp):
    from gui.components.download_range_dialog import DownloadRangeDialog

    d = DownloadRangeDialog("书", 10, default_start=4)
    assert d.range_radio.isChecked()
    assert d.start_spin.value() == 4
    assert d.end_spin.value() == 10
    assert not d.all_radio.isChecked()
    d._on_accept()
    assert d.selection() == (4, 10)


def test_no_default_start_all_selected(_qapp):
    from gui.components.download_range_dialog import DownloadRangeDialog

    d = DownloadRangeDialog("书", 10)
    assert d.all_radio.isChecked()
    d._on_accept()
    assert d.selection() is None


def test_build_selection_from_range():
    from gui.components.download_range_dialog import build_selection

    sel = build_selection(5, (2, 4))
    assert sel == [False, True, True, True, False]


def test_build_selection_all():
    from gui.components.download_range_dialog import build_selection

    assert build_selection(3, None) == [True, True, True]


def test_default_start_clamped_within_total(_qapp):
    from gui.components.download_range_dialog import DownloadRangeDialog

    d = DownloadRangeDialog("书", 5, default_start=99)
    assert d.start_spin.value() == 5
    assert d.end_spin.value() == 5
    d._on_accept()
    assert d.selection() == (5, 5)


# ------------------------------------------------------------------- #
# reader_page 下载信号携带当前章 1 基索引
# ------------------------------------------------------------------- #
def test_reader_on_download_emits_current_index():
    from gui.pages.reader_page import ReaderPage

    class FakeView:
        _current_idx = 2  # 0 基 → 第 3 章

    class FakeReader(QObject):
        download_requested = Signal(object)
        _current_book_url = "http://x/b"
        _current_source_id = "s1"
        _current_content_type = "novel"

        def __init__(self):
            super().__init__()
            self.stack = type("S", (), {"currentWidget": lambda s: FakeView()})()

    fr = FakeReader()
    fr._current_chapter_index = ReaderPage._current_chapter_index.__get__(fr)
    fr._on_download_clicked = ReaderPage._on_download_clicked.__get__(fr)
    got = []
    fr.download_requested.connect(lambda p: got.append(p))
    fr._on_download_clicked()
    assert got == [("s1", "http://x/b", "novel", 3)]


# ------------------------------------------------------------------- #
# _download_from_reader 构建 range_dialog item
# ------------------------------------------------------------------- #
def test_download_from_reader_builds_range_item():
    """阅读器下载 → item 带 range_dialog flag + 当前章索引。"""
    from types import SimpleNamespace

    from gui.app import MainWindow

    class FA:
        pass

    app = FA()
    captured = {}
    app._on_batch_download = lambda items: captured.update(items=items)
    app.download_queue = None
    bound = MainWindow._download_from_reader.__get__(app)
    bound(("s1", "http://x/b", "novel", 3))
    it = captured["items"][0]
    assert it.range_dialog is True
    assert it.current_idx == 3
    assert it.source_id == "s1"
    assert it.url == "http://x/b"


def test_download_from_reader_default_idx():
    """payload 第 4 项缺失时默认 idx=1。"""
    from types import SimpleNamespace

    from gui.app import MainWindow

    class FA:
        pass

    app = FA()
    captured = {}
    app._on_batch_download = lambda items: captured.update(items=items)
    MainWindow._download_from_reader.__get__(app)(("s1", "http://x/b", "novel"))
    it = captured["items"][0]
    assert it.current_idx == 1


# ------------------------------------------------------------------- #
# video_view 下载信号携带当前集索引
# ------------------------------------------------------------------- #
def test_video_on_download_emits_current_episode():
    from types import SimpleNamespace

    from gui.pages.reader.video_view import VideoView

    view = VideoView(object())
    view._source = SimpleNamespace(source_id="s1")
    view._detail = SimpleNamespace(url="http://x/v", content_type="video", chapters=[])
    view._episodes = [SimpleNamespace(url="e1"), SimpleNamespace(url="e2"),
                      SimpleNamespace(url="e3")]
    view._current_idx = 1  # 0 基 → 第 2 集
    got = []
    view.download_requested.connect(lambda p: got.append(p))
    view._on_download_clicked()
    assert got == [("s1", "http://x/v", "video", 2)]


def test_video_on_download_no_eps_falls_back_to_1():
    from types import SimpleNamespace

    from gui.pages.reader.video_view import VideoView

    view = VideoView(object())
    view._source = SimpleNamespace(source_id="s1")
    view._detail = SimpleNamespace(url="http://x/v", content_type="video", chapters=[])
    view._episodes = []
    view._current_idx = -1
    got = []
    view.download_requested.connect(lambda p: got.append(p))
    view._on_download_clicked()
    assert got == [("s1", "http://x/v", "video", 1)]