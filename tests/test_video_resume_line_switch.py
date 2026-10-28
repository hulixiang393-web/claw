# -*- coding: utf-8 -*-
"""换线路后的续播回归（2026-10-28）。

现场：omofuna 这类多线路源，分集 URL 里带线路号（`/play/1/11.html?sid=1`）。
用户在第 1 条线路看到 314s，重开时详情页按默认线路（sid=3）重新抓分集，于是
URL 变成 `/play/3/11.html?sid=3`。旧逻辑用 `record["chapter_url"] == 当前集 URL`
判断「是不是同一集」，换线路后恒为假 → 位置被丢弃，VLC 从头播。

本文件锁住三件事：
1. 同一集换线路（URL 变、集号不变）仍能取回续播位置；
2. 单集视频的签名 URL（每次新 auth_key）仍能取回续播位置；
3. 不同集不能互相串位置（防止「为了能续播」把判断放宽到错集）。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

pytest.importorskip("PySide6")

from framework.content import Chapter, Detail  # noqa: E402
from framework.reading_progress import ReadingProgress  # noqa: E402
from gui.pages.reader_page import ReaderPage  # noqa: E402
from types import SimpleNamespace  # noqa: E402

BOOK = "https://demo.test/anime/abc123.html"


def _reader(tmp_path, monkeypatch):
    from PySide6.QtCore import QThreadPool

    import gui.pages.reader_page as reader_module

    progress = ReadingProgress(tmp_path / "progress.json")
    manager = SimpleNamespace(get=lambda sid: SimpleNamespace(
        source_id=sid, source_name="站点", raw={}))
    reader = ReaderPage(manager, SimpleNamespace(), reading_progress=progress)
    reader.set_favorite_checker(lambda url: False)
    monkeypatch.setattr(
        reader_module, "QThreadPool",
        SimpleNamespace(globalInstance=lambda: SimpleNamespace(start=lambda task: None)),
    )
    monkeypatch.setattr(
        QThreadPool, "globalInstance",
        lambda: SimpleNamespace(start=lambda task: None),
    )
    return reader, progress


def _episodes(sid: str):
    return [
        Chapter(f"第{i:02d}集", f"https://demo.test/anime/abc123/play/{sid}/{i}.html?sid={sid}")
        for i in range(1, 25)
    ]


def _bind(reader, detail):
    view = reader.video_view
    view._detail = detail
    view._episodes = detail.chapters
    view.library_url = BOOK
    return view


def test_resume_survives_line_switch(_qapp, tmp_path, monkeypatch):
    reader, progress = _reader(tmp_path, monkeypatch)
    progress.save(
        "omofuna", BOOK, "video",
        "https://demo.test/anime/abc123/play/1/11.html?sid=1", "11集",
        position=314 / 1431,
        location={"seconds": 314.0, "duration": 1431.0},
        title="齐木楠雄的灾难第一季", library_url=BOOK,
    )
    detail = Detail(source_id="omofuna", content_type="video", url=BOOK,
                    title="齐木楠雄的灾难第一季", chapters=_episodes(3))
    view = _bind(reader, detail)
    view._current_idx = 10  # 第 11 集，本轮默认线路上
    options = view._progress_options()
    assert options["resume_positions"][10]["seconds"] == 314.0
    reader.deleteLater()
    _qapp.processEvents()


def test_load_locates_saved_episode_on_another_line(_qapp, tmp_path, monkeypatch):
    """续读 URL 与本轮分集 URL 不同线 → 仍定位到同一集并自动续播。"""
    reader, progress = _reader(tmp_path, monkeypatch)
    saved_url = "https://demo.test/anime/abc123/play/1/11.html?sid=1"
    progress.save(
        "omofuna", BOOK, "video", saved_url, "11集",
        position=0.22, location={"seconds": 314.0, "duration": 1431.0},
        title="齐木楠雄的灾难第一季", library_url=BOOK,
        detail_url=BOOK,
    )
    detail = Detail(source_id="omofuna", content_type="video", url=BOOK,
                    title="齐木楠雄的灾难第一季", chapters=_episodes(3))
    loaded = []
    monkeypatch.setattr(reader.video_view, "_load_episode", loaded.append)
    reader.open("omofuna", BOOK, "video")
    reader._on_detail(detail, None, "video", reader._current_start_url, "omofuna")
    assert reader.video_view._current_idx == 10
    assert loaded == [10]
    assert reader.video_view._progress_options()["resume_positions"][10]["seconds"] == 314.0
    reader.deleteLater()
    _qapp.processEvents()


def test_single_episode_signed_url_keeps_position(_qapp, tmp_path, monkeypatch):
    """单集视频：m3u8 每次重新签名（auth_key 变）也算同一集。"""
    reader, progress = _reader(tmp_path, monkeypatch)
    progress.save(
        "51cg1", BOOK, "video",
        "https://hls.demo.test/videos4/aa/bb.m3u8?auth_key=111-222&v=3&time=0",
        "标题001",
        position=0.84,
        location={"seconds": 1434.0, "duration": 1703.0},
        title="标题", library_url=BOOK,
    )
    detail = Detail(source_id="51cg1", content_type="video", url=BOOK, title="标题",
                    chapters=[Chapter("标题001",
                                      "https://hls.demo.test/videos4/aa/bb.m3u8?auth_key=999-888&v=3&time=0")])
    view = _bind(reader, detail)
    view._current_idx = 0
    assert view._progress_options()["resume_positions"][0]["seconds"] == 1434.0
    reader.deleteLater()
    _qapp.processEvents()


def test_other_episode_never_inherits_position(_qapp, tmp_path, monkeypatch):
    """不同集不串位置：看到第 11 集，播第 12 集必须从头开始。"""
    reader, progress = _reader(tmp_path, monkeypatch)
    progress.save(
        "omofuna", BOOK, "video",
        "https://demo.test/anime/abc123/play/1/11.html?sid=1", "11集",
        position=0.9, location={"seconds": 1287.0, "duration": 1431.0},
        title="作品", library_url=BOOK,
    )
    detail = Detail(source_id="omofuna", content_type="video", url=BOOK,
                    title="作品", chapters=_episodes(1))
    view = _bind(reader, detail)
    view._current_idx = 11  # 第 12 集
    assert view._progress_options()["resume_positions"][11] == {}
    reader.deleteLater()
    _qapp.processEvents()


@pytest.mark.parametrize("url,title,expect", [
    ("https://x/play/1/11.html?sid=1", "第11集", 11),
    ("https://x/play/3/11.html?sid=3", "11集", 11),
    ("https://x/play/3/11.html", "第01集", 1),
    ("https://x/a/b.m3u8?auth_key=1", "标题001", None),
])
def test_episode_signature(url, title, expect):
    from gui.pages.reader.video_view import _episode_signature

    assert _episode_signature(url, title)[0] == expect


def test_same_episode_ignores_line_but_not_episode():
    from gui.pages.reader.video_view import _same_episode

    a = "https://x/anime/h/play/1/11.html?sid=1"
    b = "https://x/anime/h/play/3/11.html?sid=3"
    c = "https://x/anime/h/play/3/12.html?sid=3"
    assert _same_episode(a, "第11集", b, "第11集")
    assert not _same_episode(a, "第11集", c, "第12集")
    assert not _same_episode(a, "", c, "")
    # 单集作品的放宽不能盖过集号冲突：连载型单集换集不继承上一集秒数
    assert _same_episode(a, "第11集", c, "第12集", single=True) is False
    # 集号取不到时才允许按「只有一集」放宽
    assert _same_episode("https://x/a.m3u8", "a", "https://x/b.m3u8", "b", single=True)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
