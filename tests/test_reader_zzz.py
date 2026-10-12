import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEventLoop, QTimer, Qt

from framework.content import Chapter
from framework.settings_manager import reader_prefetch_settings
from gui.pages.reader.epub_view import EpubView
from gui.pages.reader.novel_view import NovelView
from gui.pages.settings_page import SettingsPage


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


def _chapters(count):
    return [Chapter(f"第{i + 1}章", f"chapter-{i + 1}") for i in range(count)]


def test_novel_toc_batches_large_directory_and_completes(_qapp):
    view = NovelView(object())
    view._chapters = _chapters(501)
    view._populate_toc()
    assert view.toc_list.count() == 200
    loop = QEventLoop()
    QTimer.singleShot(100, loop.quit)
    loop.exec()
    assert view.toc_list.count() == 501
    assert view.toc_list.item(500).data(Qt.UserRole) == 500


def test_novel_toc_old_generation_cannot_append(_qapp):
    view = NovelView(object())
    view._chapters = _chapters(250)
    view._populate_toc()
    old_generation = view._toc_generation
    view._chapters = _chapters(2)
    view._populate_toc()
    view._append_toc_batch(old_generation)
    assert view.toc_list.count() == 2


def test_novel_prefetch_config_is_safe_and_defaults_are_small(_qapp):
    view = NovelView(object())
    assert (view._prefetch_ahead, view._prefetch_behind) == (1, 0)
    view.set_prefetch_config(True, 999, 999)
    assert (view._prefetch_ahead, view._prefetch_behind) == (1, 0)
    view.set_prefetch_config(True, "bad", None)
    assert (view._prefetch_ahead, view._prefetch_behind) == (1, 0)


def test_reader_prefetch_settings_has_small_defaults_and_caps():
    class _Settings:
        def __init__(self, values):
            self.values = values

        def get(self, section, key, default=None):
            return self.values.get(key, default)

    defaults = reader_prefetch_settings(_Settings({}))
    assert defaults == {"enabled": True, "ahead": 1, "behind": 0}
    capped = reader_prefetch_settings(_Settings({"prefetch_ahead": 99, "prefetch_behind": 99}))
    assert capped == {"enabled": True, "ahead": 1, "behind": 0}


def test_novel_current_load_and_prefetch_do_not_duplicate(_qapp, monkeypatch):
    view = NovelView(object())
    view._source = object()
    view._chapters = _chapters(3)
    view._current_idx = 0
    view.set_prefetch_config(True, 1, 0)
    started = []
    monkeypatch.setattr(view, "_pump_next_prefetch", lambda: started.append(view._next_prefetch_queue[0]))
    view._prefetch_next(0)
    assert view._next_prefetch_queue == [1]
    assert view._prefetch_idx != 0
    assert started == [1]


def test_plain_novel_style_is_serif_and_width_limited(_qapp):
    view = NovelView(object())
    css = view._plain_body_css()
    assert "Georgia" in css
    assert "text-indent: 0" in css
    assert "margin-bottom: 0.9em" in css
    assert view.text.maximumWidth() == 960
    assert view.paged_label.maximumWidth() == 960
    view.deleteLater()


def test_settings_reader_font_range_is_48(_qapp):
    class _Settings:
        def get(self, section, key, default=None):
            return default

        def set(self, section, key, value):
            pass

        def save(self):
            pass

    page = SettingsPage(_Settings(), SimpleNamespace())
    assert page._ui_reading_font.maximum() == 48
    page.deleteLater()


def test_global_font_override_clears_manual_delta_and_clamps_at_48(_qapp):
    novel = NovelView(object())
    novel._adjust_font(20)
    novel.set_font_scale(3)
    assert novel._font_delta == 0
    assert novel._base_font == 48
    assert "font-size: 48px" in novel.text.styleSheet()

    epub = EpubView()
    epub._adjust_font(20)
    epub.set_font_scale(3)
    assert epub._font_delta == 0
    assert epub._base_font == 48
    assert "font-size: 48px" in epub.text.styleSheet()
    novel.deleteLater()
    epub.deleteLater()
