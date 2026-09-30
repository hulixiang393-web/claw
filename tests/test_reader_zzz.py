import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEventLoop, QTimer

from framework.content import Chapter
from gui.pages.reader.novel_view import NovelView


def test_novel_location_helpers():
    snapshot = NovelView._build_location_snapshot("【第1章】\n\nalpha beta gamma", 0.5, 100, 200, "chapter-1")
    assert snapshot["content_hash"]
    assert snapshot["chapter_url"] == "chapter-1"
    assert NovelView._resolve_location_scroll(
        "【第1章】\n\nalpha beta gamma", {"anchor": "beta", "scroll_ratio": 0.9}, 100
    ) == 38


def test_novel_location_requires_current_chapter_identity():
    assert NovelView._location_matches_chapter({"chapter_url": "chapter-1"}, "chapter-1")
    assert not NovelView._location_matches_chapter({"chapter_url": "chapter-1"}, "chapter-2")
    assert not NovelView._location_matches_chapter({}, "chapter-1")


def test_novel_user_scroll_invalidates_pending_restore_retry(_qapp):
    view = NovelView(object())
    view._detail = object()
    view._chapters = [Chapter("第一章", "chapter-1")]
    view._current_idx = 0
    view.text.setText("【第一章】\n\nalpha beta gamma")
    view.scroll.verticalScrollBar().setRange(0, 1000)
    book = view._detail
    generation = view._restore_generation
    location = {"chapter_url": "chapter-1", "anchor": "target", "offset": 0, "scroll_ratio": 0.2}
    view._restore_location_if_book(200, location, book, generation)
    view.scroll.verticalScrollBar().setValue(800)
    view._on_scrollbar_user_interaction()
    view._restore_generation = generation
    loop = QEventLoop()
    QTimer.singleShot(150, loop.quit)
    loop.exec()
    assert view.scroll.verticalScrollBar().value() == 800
