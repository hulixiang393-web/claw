"""视频阅读封面三级兜底测试（detail.cover → 章节封面 → 后台重取）。

纯逻辑级：object.__new__(VideoView)，避开 VLC/QTimer 初始化，
patch _apply_cover / _fetch_cover_in_background 收集调用。
"""
import os
import sys
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

QAPP = None


@pytest.fixture(scope="session", autouse=True)
def _qapp():
    global QAPP
    from PySide6.QtWidgets import QApplication

    if QAPP is None:
        QAPP = QApplication([])
    return QAPP


def _make_view(detail, cover="", content=None, source=object()):
    """构造轻量 VideoView（不跑 __init__ 的播放器部分）。"""
    from PySide6.QtWidgets import QLabel

    from gui.pages.reader.video_view import VideoView

    view = VideoView.__new__(VideoView)
    view.cover_label = QLabel()
    view._source = source
    view._content = content
    view._detail = detail
    return view


def _detail_with_cover(cover, chapter_covers=None):
    from framework.content import Chapter, Detail

    chapters = [
        Chapter(f"第{i+1}集", f"http://e/{i}", cover=ch_c or "")
        for i, ch_c in enumerate(chapter_covers or [])
    ]
    return Detail(
        source_id="demo",
        content_type="video",
        url="http://example.com/v/1",
        title="作品",
        cover=cover,
        chapters=chapters,
    )


def test_detail_cover_priority(_qapp):
    """detail.cover 存在 → 直接用 detail.cover。"""
    from gui.pages.reader.video_view import VideoView

    detail = _detail_with_cover("http://c/detail.jpg", ["http://c/ep1.jpg"])
    view = _make_view(detail)
    with patch.object(VideoView, "_apply_cover") as apply_cover, \
         patch.object(VideoView, "_fetch_cover_in_background") as bg:
        view._load_ep_cover(detail)
    apply_cover.assert_called_once_with("http://c/detail.jpg")
    bg.assert_not_called()


def test_fallback_to_first_ep_cover(_qapp):
    """detail.cover 空 → 用第一集非空缩略图。"""
    from gui.pages.reader.video_view import VideoView

    detail = _detail_with_cover("", ["http://c/ep1.jpg", "http://c/ep2.jpg"])
    view = _make_view(detail)
    with patch.object(VideoView, "_apply_cover") as apply_cover, \
         patch.object(VideoView, "_fetch_cover_in_background") as bg:
        view._load_ep_cover(detail)
    apply_cover.assert_called_once_with("http://c/ep1.jpg")
    bg.assert_not_called()


def test_first_ep_cover_skips_empty(_qapp):
    """_first_ep_cover 跳过空章节封面，返回第一张非空。"""
    from gui.pages.reader.video_view import VideoView

    detail = _detail_with_cover("", ["", "", "http://c/ep3.jpg"])
    view = _make_view(detail)
    assert view._first_ep_cover(detail) == "http://c/ep3.jpg"


def test_all_empty_triggers_background(_qapp):
    """全部无封面 → 隐藏封面并触发后台重取。"""
    from gui.pages.reader.video_view import VideoView

    detail = _detail_with_cover("", ["", ""])
    view = _make_view(detail, content=object())
    with patch.object(VideoView, "_apply_cover") as apply_cover, \
         patch.object(VideoView, "_fetch_cover_in_background") as bg:
        view._load_ep_cover(detail)
    apply_cover.assert_not_called()
    bg.assert_called_once_with(detail)
    assert view.cover_label.isHidden()


def test_no_content_no_background(_qapp):
    """无 _source/_content 引用时后台重取直接返回（防 NPE）。"""
    from gui.pages.reader.video_view import VideoView

    detail = _detail_with_cover("", ["", ""])
    view = VideoView.__new__(VideoView)
    from PySide6.QtWidgets import QLabel

    view.cover_label = QLabel()
    view._source = None
    view._content = None
    view._detail = detail
    view._fetch_cover_in_background(detail)  # 不应抛异常