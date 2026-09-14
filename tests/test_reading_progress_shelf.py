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
        return "正文。" * 100

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


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
