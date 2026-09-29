# -*- coding: utf-8 -*-
"""阅读进度跨重启记忆 + 退出落盘测试（test_reading_progress_shelf.py）。

背景：书架进入的书，重启后应从上次章节/位置续读（不得从头开始）。修复：
- ReaderPage.flush_progress() 供 App 在退出/切走阅读 Tab 时落盘最后位置；
- ReadingProgress 写盘后，新实例（模拟重启）可 resume 到同一记录。

离线（offscreen）：mock content，不触达网络。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtCore import QEventLoop, QTimer

import framework.reading_progress as reading_progress_module
from framework.reading_progress import ReadingProgress
from framework.content import Detail, Chapter

BOOK = "https://example.com/book/1"
CH2 = "https://example.com/book/1/2.html"


# --------------------------------------------------------------------- #
# 纯逻辑：写盘 → 新实例（重启）→ resume
# --------------------------------------------------------------------- #
def test_progress_persists_across_restart(tmp_path):
    path = tmp_path / "reading_progress.json"
    rp = ReadingProgress(path)
    rp.save("demo", BOOK, "novel", CH2, "第二章", position=0.42)

    rp2 = ReadingProgress(path)  # 模拟重启：重新加载文件
    rec = rp2.resume(BOOK)
    assert rec is not None
    assert rec["chapter_url"] == CH2
    assert abs(rec["position"] - 0.42) < 1e-9


def test_progress_persists_optional_location_across_restart(tmp_path):
    path = tmp_path / "reading_progress.json"
    location = {"version": "v2", "path": "chapters/2", "offset": 17}
    rp = ReadingProgress(path)
    rp.save("demo", BOOK, "novel", CH2, "第二章", location=location)

    location["offset"] = 99
    rec = ReadingProgress(path).resume(BOOK)

    assert rec is not None
    assert rec["location"] == {"version": "v2", "path": "chapters/2", "offset": 17}


def test_resume_returns_defensive_location_copy(tmp_path):
    rp = ReadingProgress(tmp_path / "reading_progress.json")
    rp.save("demo", BOOK, "novel", CH2, "第二章", location={"version": "v2"})

    rec = rp.resume(BOOK)
    assert rec is not None
    rec["location"]["version"] = "changed"

    assert rp.resume(BOOK)["location"] == {"version": "v2"}


def test_legacy_record_remains_readable_without_location(tmp_path):
    path = tmp_path / "reading_progress.json"
    path.write_text(
        '{"%s": {"source_id": "demo", "book_url": "%s", '
        '"content_type": "novel", "chapter_url": "%s", '
        '"chapter_title": "第二章", "position": 0.42, "page": null, '
        '"updated_at": "2026-09-28T12:00:00"}}'
        % (BOOK, BOOK, CH2),
        encoding="utf-8",
    )

    rec = ReadingProgress(path).resume(BOOK)

    assert rec is not None
    assert rec["chapter_url"] == CH2
    assert rec["position"] == 0.42
    assert "location" not in rec


def test_invalid_location_is_not_saved(tmp_path):
    rp = ReadingProgress(tmp_path / "reading_progress.json")

    with pytest.raises(TypeError):
        rp.save("demo", BOOK, "novel", CH2, "第二章", location=["not", "an", "object"])


def test_nested_non_json_location_does_not_mutate_existing_progress(tmp_path):
    path = tmp_path / "reading_progress.json"
    rp = ReadingProgress(path)
    rp.save("demo", BOOK, "novel", CH2, "第二章", position=0.42)

    with pytest.raises(TypeError, match="JSON"):
        rp.save(
            "demo",
            BOOK,
            "novel",
            CH2,
            "第二章",
            position=0.84,
            location={"video": {"segment": object()}},
        )

    rec = rp.resume(BOOK)
    assert rec is not None
    assert rec["position"] == 0.42
    assert rec["location"] is None
    assert ReadingProgress(path).resume(BOOK)["position"] == 0.42


def test_same_chapter_legacy_signal_preserves_location(tmp_path):
    rp = ReadingProgress(tmp_path / "reading_progress.json")
    location = {"version": "v2", "anchor": "beta", "scroll_ratio": 0.5}
    rp.save("demo", BOOK, "novel", CH2, "第二章", location=location)

    rp.save("demo", BOOK, "novel", CH2, "第二章")

    assert rp.resume(BOOK)["location"] == location


def test_new_chapter_legacy_signal_resets_location(tmp_path):
    rp = ReadingProgress(tmp_path / "reading_progress.json")
    location = {"version": "v2", "anchor": "beta", "scroll_ratio": 0.5}
    rp.save("demo", BOOK, "novel", CH2, "第二章", location=location)

    rp.save("demo", BOOK, "novel", "https://example.com/book/1/3.html", "第三章")

    assert rp.resume(BOOK)["location"] is None


def test_failed_atomic_write_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "reading_progress.json"
    rp = ReadingProgress(path)
    rp.save("demo", BOOK, "novel", CH2, "第二章", position=0.42)
    previous = path.read_text(encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(reading_progress_module.os, "replace", fail_replace)
    rp.save("demo", BOOK, "novel", CH2, "第二章", position=0.84)

    assert path.read_text(encoding="utf-8") == previous
    assert ReadingProgress(path).resume(BOOK)["position"] == 0.42


# --------------------------------------------------------------------- #
# ReaderPage.flush_progress 落盘 + 重开续读
# --------------------------------------------------------------------- #
class _MockContent:
    def __init__(self, chapters):
        self._chapters = chapters

    def fetch_detail(self, source, url):
        return Detail(source_id="demo", content_type="novel", url=BOOK,
                      title="测试书", chapters=self._chapters)

    def fetch_chapter(self, source, url):
        return "正文。" * 1000

    def precache_chapters(self, *a, **k):
        pass

    _cache = None


class _FakeManager:
    def get(self, sid):
        from types import SimpleNamespace

        return SimpleNamespace(source_id=sid, source_name="演示站", content_type="novel")


@pytest.fixture(scope="module")
def app(_qapp):
    return _qapp


def _wait(app, ms=100, times=1):
    for _ in range(times):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
        app.processEvents()


def test_flush_progress_saves_current_position(app, tmp_path):
    from gui.pages.reader_page import ReaderPage

    path = tmp_path / "rp.json"
    rp = ReadingProgress(path)
    chapters = [Chapter(f"第{i}章", f"https://example.com/book/1/{i}.html") for i in range(1, 5)]
    content = _MockContent(chapters)

    reader = ReaderPage(_FakeManager(), content, reading_progress=rp)
    reader.resize(800, 600)
    reader.show()
    reader.open("demo", BOOK, "novel")
    _wait(app, 100, 15)

    # 模拟用户翻到第 3 章并滚动到中段
    nv = reader.novel_view
    nv._current_idx = 2
    nv.scroll.verticalScrollBar().setRange(0, 1000)
    nv.scroll.verticalScrollBar().setValue(500)
    reader.flush_progress()

    # 重启：新 ReadingProgress 读同一文件
    rp2 = ReadingProgress(path)
    rec = rp2.resume(BOOK)
    assert rec is not None
    assert rec["chapter_url"] == chapters[2].url, rec
    assert rec["position"] is not None and rec["position"] > 0
    assert rec["location"]["anchor"]
    assert rec["location"]["scroll_value"] == 500

    reader2 = ReaderPage(_FakeManager(), content, reading_progress=ReadingProgress(path))
    reader2.resize(1100, 700)
    reader2.show()
    reader2.open("demo", BOOK, "novel")
    _wait(app, 100, 15)
    assert reader2.novel_view._current_idx == 2
    assert reader2.novel_view.scroll.verticalScrollBar().value() > 0

    reader.deleteLater()
    reader2.deleteLater()
    app.processEvents()


def test_flush_progress_does_not_save_video_playback_position(app, tmp_path):
    from gui.pages.reader_page import ReaderPage

    path = tmp_path / "rp.json"
    rp = ReadingProgress(path)
    reader = ReaderPage(_FakeManager(), _MockContent([]), reading_progress=rp)
    reader.video_view._detail = Detail(
        source_id="demo",
        content_type="video",
        url=BOOK,
        title="视频",
        chapters=[Chapter("第一集", "https://example.com/video/1")],
    )
    reader.video_view._episodes = reader.video_view._detail.chapters
    reader.video_view._current_idx = 0
    reader.video_view._has_played = True
    reader.video_view._player = type("Player", (), {"get_position": lambda self: 0.75})()
    reader.stack.setCurrentWidget(reader.video_view)
    reader._current_book_url = BOOK

    reader.flush_progress()

    assert rp.resume(BOOK) is None
    reader.deleteLater()
    app.processEvents()


def test_open_resumes_saved_chapter(app, tmp_path):
    from gui.pages.reader_page import ReaderPage

    path = tmp_path / "rp.json"
    rp = ReadingProgress(path)
    chapters = [Chapter(f"第{i}章", f"https://example.com/book/1/{i}.html") for i in range(1, 5)]
    rp.save("demo", BOOK, "novel", chapters[2].url, "第三章")

    content = _MockContent(chapters)
    reader = ReaderPage(_FakeManager(), content, reading_progress=rp)
    reader.resize(800, 600)
    reader.show()
    reader.open("demo", BOOK, "novel")  # 不带 start_chapter_url → 用记忆
    _wait(app, 100, 15)

    nv = reader.novel_view
    assert nv._current_idx == 2, f"应续读到第三章，got idx={nv._current_idx}"
    assert nv._chapters[nv._current_idx].url == chapters[2].url

    reader.deleteLater()
    app.processEvents()


def test_shelf_open_resumes_canonical_progress_when_cached_detail_url_differs(app, tmp_path):
    from gui.pages.reader_page import ReaderPage

    path = tmp_path / "rp.json"
    rp = ReadingProgress(path)
    chapters = [Chapter(f"第{i}章", f"https://example.com/book/1/{i}.html") for i in range(1, 5)]
    rp.save("demo", "https://EXAMPLE.com/book/1/", "novel", chapters[2].url, "第三章")

    content = _MockContent(chapters)
    reader = ReaderPage(_FakeManager(), content, reading_progress=rp)
    reader.resize(800, 600)
    reader.show()
    reader.open("demo", BOOK, "novel")
    _wait(app, 100, 15)

    assert reader.novel_view._current_idx == 2
    reader.deleteLater()
    app.processEvents()


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
