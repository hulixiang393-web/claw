# -*- coding: utf-8 -*-
"""跨书阅读位置串书回归测试（test_read_restore_cross_book.py）。

修复的 bug：快速换书时，上一本书残留的续读位置（_pending_restore /
_pending_position）被新书的章节显示/滚动回调消费，导致滚动条错滚到
旧书位置。根因是各视图 load() 只在新书有续读记录时才设置、但从不显式
清零残留值。

验证的不变量：
- load() 换书后 _pending_restore / _pending_position 处于「清零后按新书
  记录重新设置」的干净状态：换一本无续读记录的书，残留值必须消失；
- comic 的恢复重试链在换书后终止（不再继续滚动新书）。

通过 monkeypatch 屏蔽章节加载（_load_episode / _load_chapter 置为 no-op），
让 load() 只执行「换书状态重置 + 续读位置设置」逻辑——正是修复点本体，
不触碰渲染/网络路径。
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.content import Chapter, Detail


def _detail(source_id: str = "demo", content_type: str = "comic", base: str = "http://c.example/m/1") -> Detail:
    return Detail(
        source_id=source_id,
        content_type=content_type,
        url=base,
        title="书",
        chapters=[
            Chapter("第1话", f"{base}/1.html"),
            Chapter("第2话", f"{base}/2.html"),
        ],
    )


def _comic_view():
    from gui.pages.reader.comic_view import ComicView

    view = ComicView(object())
    view._load_episode = lambda idx: None  # 屏蔽网络/渲染路径，只测状态逻辑
    return view


def _novel_view():
    from gui.pages.reader.novel_view import NovelView

    view = NovelView(object())
    view._load_chapter = lambda *a, **k: None  # 屏蔽网络/渲染路径
    return view


def _video_view():
    from gui.pages.reader.video_view import VideoView

    view = VideoView(object())
    return view


def test_comic_load_clears_stale_pending_position(_qapp):
    """换一本无续读记录的书：上一本书残留的 _pending_position 必须清零。"""
    view = _comic_view()
    src = SimpleNamespace(source_id="demo")
    # 残留场景：旧书 load 带续读记录 → _pending_position 被设置
    view.load(src, _detail(), restore_position=0.5)
    assert view._pending_position == 0.5
    # 换一本无续读记录的书 → 残留必须被清零（修复前此处残留 0.5）
    view.load(src, _detail(base="http://c.example/m/2"))
    assert view._pending_position is None


def test_comic_load_sets_position_only_with_record(_qapp):
    """有续读记录时正确设置；无记录保持 None（清零后的干净态）。"""
    view = _comic_view()
    src = SimpleNamespace(source_id="demo")
    view.load(src, _detail(), restore_position=0.3)
    assert view._pending_position == 0.3
    view.load(src, _detail(base="http://c.example/m/3"))
    assert view._pending_position is None


def test_comic_retry_chain_stops_after_book_switch(_qapp):
    """换书后旧书恢复重试链终止：不再对 vbar/hbar 调用 setValue。"""
    from gui.pages.reader import comic_view as cv

    view = _comic_view()
    view._detail = _detail()
    # 空调用（无 maximum）本就不动滚动条；核心是换书后 _retry 直接 return。
    # 制造「换书后再触发重试」场景：先发起重试链，再换书，等 500ms 后触发。
    from PySide6.QtCore import QTimer, QEventLoop

    view._restore_position_with_retry(0.5)
    # 换书（无续读）：清理 + detail 替换
    view._detail = _detail(base="http://c.example/m/2")
    loop = QEventLoop()
    hit_vbar = []

    vbar = view.scroll.verticalScrollBar()

    def _patched_set_value(v):
        hit_vbar.append(v)

    with patch.object(vbar, "setValue", side_effect=_patched_set_value):
        QTimer.singleShot(600, loop.quit)  # 越过 500ms 触发点
        loop.exec()
    # 换书后旧书重试链不应再触发 setValue；若无守卫，第 2 次 setValue(0.5*max) 会执行
    assert hit_vbar == [], f"换书后旧书恢复重试链仍试图滚动新书: {hit_vbar}"


def test_novel_load_clears_stale_pending_restore(_qapp):
    """换一本无续读记录的小说：上一本残留 _pending_restore 必须清零。"""
    view = _novel_view()
    src = SimpleNamespace(source_id="demo")
    view.load(src, _detail(content_type="novel", base="http://n.example/b/1"),
              restore_position=0.4)
    assert view._pending_restore == (0.4, None)
    view.load(src, _detail(content_type="novel", base="http://n.example/b/2"))
    assert view._pending_restore is None


def test_novel_load_sets_restore_only_with_record(_qapp):
    """小说有续读记录时正确设置；无记录保持 None。"""
    view = _novel_view()
    src = SimpleNamespace(source_id="demo")
    view.load(src, _detail(content_type="novel", base="http://n.example/b/3"),
              restore_position=0.2, restore_page=5)
    assert view._pending_restore == (0.2, 5)
    view.load(src, _detail(content_type="novel", base="http://n.example/b/4"))
    assert view._pending_restore is None


def test_video_load_clears_stale_pending_position(_qapp):
    """视频同 bug 家族：换一本无续读记录的视频，残留 _pending_position 清零。"""
    view = _video_view()
    # 残留场景：旧视频 load 带续读 → _pending_position 被设置
    view._pending_position = 0.6
    view.load(
        SimpleNamespace(source_id="demo"),
        _detail(content_type="video", base="http://v.example/v/1"),
    )
    assert view._pending_position is None


def test_video_load_sets_position_only_with_record(_qapp):
    """视频有续读记录时正确设置。"""
    view = _video_view()
    view.load(
        SimpleNamespace(source_id="demo"),
        _detail(content_type="video", base="http://v.example/v/2"),
        restore_position=0.7,
    )
    assert view._pending_position == 0.7