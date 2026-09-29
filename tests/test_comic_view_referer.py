# -*- coding: utf-8 -*-
"""漫画阅读器正文图 Referer 透传测试（test_comic_view_referer.py）。

覆盖：正文图防盗链——manben 等站图床校验精确章节页 Referer，阅读器须把
当前章节页 URL 透传给 CoverLoader（显式 referer 优先于域名规则兜底）。
- _render_images / _render_incremental 创建 _ComicImageLabel 时传 referer=当前章节 URL
- data URI / 本地文件字节分支在 load() 内短路过 CoverLoader，不受 Referer 影响
"""
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtWidgets import QLabel as _QLabel

from framework.content import Chapter, Detail


class _Sig:
    def connect(self, *_a, **_k):
        pass


def _make_view():
    from gui.pages.reader.comic_view import ComicView

    view = ComicView(object())
    detail = Detail(
        source_id="demo", content_type="comic", url="http://c.example/m/1",
        title="漫画",
        chapters=[
            Chapter("第1话", "http://c.example/m/1/1.html"),
            Chapter("第2话", "http://c.example/m/1/2.html"),
        ],
    )
    view._detail = detail
    view._chapters = detail.chapters
    view._source = SimpleNamespace(source_id="demo")
    return view


class _FakeLabel(_QLabel):
    """记录构造参数 + load() 调用（替代真实 _ComicImageLabel）。"""

    def __init__(self, calls, url, referer="", parent=None, source=None):
        super().__init__(parent)
        self.calls = calls
        self.url = url
        self.referer = referer
        self.loaded = _Sig()

    def load(self):
        self.calls.append((self.url, self.referer))


def test_render_images_passes_chapter_url_as_referer(_qapp):
    """首屏渲染：正文图全部携带当前章节页 URL 作 Referer。"""
    from gui.pages.reader import comic_view as cv

    view = _make_view()
    view._current_idx = 1  # 第2话
    view._images = ["http://img/1.jpg", "http://img/2.jpg"]
    view._mode = "gallery"
    calls = []

    with patch.object(cv, "_ComicImageLabel") as klazz:
        klazz.side_effect = lambda *a, **k: _FakeLabel(calls, *a, **k)
        view._render_images()
    assert calls == [
        ("http://img/1.jpg", "http://c.example/m/1/2.html"),
        ("http://img/2.jpg", "http://c.example/m/1/2.html"),
    ]


def test_render_incremental_passes_chapter_url_as_referer(_qapp):
    """滚动懒加载增量渲染：同样携带当前章节页 URL 作 Referer。"""
    from gui.pages.reader import comic_view as cv

    view = _make_view()
    view._current_idx = 0  # 第1话
    view._images = ["http://img/a.jpg", "http://img/b.jpg", "http://img/c.jpg"]
    view._rendered_count = 0
    view._rendered_header = True  # 跳过话头，专注图片创建
    view._mode = "flip"
    calls = []

    with patch.object(cv, "_ComicImageLabel") as klazz:
        klazz.side_effect = lambda *a, **k: _FakeLabel(calls, *a, **k)
        view._render_incremental(force_full=True)
    assert calls == [
        ("http://img/a.jpg", "http://c.example/m/1/1.html"),
        ("http://img/b.jpg", "http://c.example/m/1/1.html"),
        ("http://img/c.jpg", "http://c.example/m/1/1.html"),
    ]


def test_data_uri_label_skips_coverloader(_qapp):
    """data URI（Playwright canvas base64）只做后台解码，不触达 CoverLoader/Referer。"""
    import base64

    from gui.components.cover_loader import CoverLoader
    from gui.pages.reader.comic_view import _ComicImageLabel

    lbl = _ComicImageLabel(
        "data:image/png;base64," + base64.b64encode(b"\x89PNG-fake").decode(),
        referer="http://c.example/m/1/1.html",
    )
    decoded = []
    lbl._decode_async = lambda data: decoded.append(data)
    with patch.object(CoverLoader, "instance", side_effect=AssertionError("data URI 不应请求网络")):
        lbl.load()
    assert decoded, "data URI 应走后台字节解码分支"


def test_local_file_label_skips_coverloader(_qapp):
    """本地文件字节同样短路过 CoverLoader（不进 Referer 流程）。"""
    import pathlib

    from gui.components.cover_loader import CoverLoader
    from gui.pages.reader.comic_view import _ComicImageLabel

    fd, path = tempfile.mkstemp()
    with os.fdopen(fd, "wb") as f:
        f.write(b"fake-jpeg-bytes")
    lbl = _ComicImageLabel(
        str(pathlib.Path(path)), referer="http://c.example/m/1/1.html",
    )
    decoded = []
    lbl._decode_async = lambda data: decoded.append(data)
    try:
        with patch.object(CoverLoader, "instance", side_effect=AssertionError("本地文件不应请求网络")):
            lbl.load()
        assert decoded, "本地文件应走读字节 + 后台解码分支"
    finally:
        os.unlink(path)


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))