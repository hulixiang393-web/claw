from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.config import SourceConfig
from framework.content import Content
from framework.parser import Parser
from framework.settings_manager import DEFAULTS, SettingsManager
from PySide6.QtWidgets import QLabel
from gui.pages.reader.novel_view import NovelView, _LoadChapterTask


def _source(raw=None):
    data = raw or {
        "id": "novel",
        "name": "Novel",
        "base_url": "https://example.com/",
        "content_type": "novel",
        "endpoints": {
            "content": {
                "chapter": {
                    "body": {"selector": {"css": ".content"}},
                    "pagination": {"enabled": False},
                }
            }
        },
    }
    return SourceConfig(
        source_id="novel",
        source_name="Novel",
        content_type="novel",
        base_url="https://example.com/",
        raw=data,
    )


def test_fetch_chapter_with_images_scopes_urls_and_preserves_old_api(monkeypatch):
    source = _source()
    content = Content(object(), Parser(), object())
    html = """
    <div class='outside'><img src='/outside.jpg'></div>
    <article class='content'>
      <p>chapter text</p>
      <img src='/one.jpg'>
      <img data-src='two.jpg'>
      <img data-original='https://img.example/three.jpg'>
      <img data-lazy-src='two.jpg'>
      <img src='data:image/png;base64,abc'>
      <img src='/ads/banner.jpg'>
    </article>
    """
    monkeypatch.setattr(content, "_get", lambda source, url: html)
    monkeypatch.setattr(content, "_bg_check", lambda *args: None)

    assert isinstance(content.fetch_chapter(source, "/chapter.html"), str)
    text, images = content.fetch_chapter_with_images(source, "/chapter.html")
    assert text == "chapter text"
    assert [entry["url"] for entry in images if entry.get("type") == "image"] == [
        "https://example.com/one.jpg",
        "https://example.com/two.jpg",
        "https://img.example/three.jpg",
    ]


def test_image_extraction_uses_selector_fallback_and_lazy_attribute_priority(monkeypatch):
    source = _source({
        "id": "novel",
        "name": "Novel",
        "base_url": "https://example.com/",
        "content_type": "novel",
        "endpoints": {"content": {"chapter": {
            "body": {"selector": [
                {"css": ".missing"},
                {"css": ".content"},
            ]}
        }}}
    })
    content = Content(object(), Parser(), object())
    html = "<article class='content'><p>x</p><img src='' data-src='lazy.jpg' data-original='original.jpg'></article>"
    monkeypatch.setattr(content, "_get", lambda source, url: html)
    monkeypatch.setattr(content, "_bg_check", lambda *args: None)
    _, images = content.fetch_chapter_with_images(source, "/chapter.html")
    assert [entry["url"] for entry in images if entry.get("type") == "image"] == ["https://example.com/lazy.jpg"]


def test_playwright_path_extracts_images_from_static_body_html(monkeypatch):
    source = _source({
        "id": "novel",
        "name": "Novel",
        "base_url": "https://example.com/",
        "content_type": "novel",
        "endpoints": {"content": {"chapter": {
            "body": {"selector": {"css": ".content"}, "render": "playwright"}
        }}}
    })
    content = Content(object(), Parser(), object())
    monkeypatch.setattr(content, "_get", lambda source, url: "<div class='content'><p>static</p><img src='/static.jpg'></div>")
    monkeypatch.setattr(content, "_bg_check", lambda *args: None)
    monkeypatch.setattr("framework.playwright_helper.fetch_rendered_text_sync", lambda *args, **kwargs: "rendered")
    text, images = content.fetch_chapter_with_images(source, "/chapter.html")
    assert text == "rendered"
    assert [entry["url"] for entry in images if entry.get("type") == "image"] == ["https://example.com/static.jpg"]


def test_fetch_chapter_with_images_returns_inline_fragments_and_cross_page_order(monkeypatch):
    source = _source({
        "id": "novel", "name": "Novel", "base_url": "https://example.com/", "content_type": "novel",
        "endpoints": {"content": {"chapter": {
            "body": {
                "selector": {"css": ".content"},
                "paginator": {"next_link": {"selector": {"css": "a.next", "attr": "href"}}},
            },
            "pagination": {"enabled": True},
        }}}
    })
    content = Content(object(), Parser(), object())
    pages = {
        "/chapter.html": "<div class='content'><p>前<img src='/one.jpg'>后</p></div><a class='next' href='/chapter_1.html'>下一页</a>",
        "https://example.com/chapter_1.html": "<div class='content'><p>二前<img src='/two.jpg'>二后</p></div>",
    }
    monkeypatch.setattr(content, "_get", lambda source, url: pages[url])
    monkeypatch.setattr(content, "_bg_check", lambda *args: None)
    text, fragments = content.fetch_chapter_with_images(source, "/chapter.html")
    assert text == "前后\n二前二后"
    assert [(item["type"], item.get("url") or item.get("text")) for item in fragments] == [
        ("text", "前"), ("image", "https://example.com/one.jpg"), ("text", "后"),
        ("text", "二前"), ("image", "https://example.com/two.jpg"), ("text", "二后"),
    ]


def test_malformed_image_entries_are_skipped_without_failing_chapter(monkeypatch):
    source = _source()
    content = Content(object(), Parser(), object())
    monkeypatch.setattr(content, "_get", lambda source, url: "<div class='content'><p>ok</p><img src='/ok.jpg'></div>")
    monkeypatch.setattr(content, "_bg_check", lambda *args: None)
    monkeypatch.setattr(content, "_extract_body_fragments", lambda *args: [{"type": "image"}, {"type": "image", "url": None}, {"type": "image", "url": "/ok.jpg"}])
    text, fragments = content.fetch_chapter_with_images(source, "/chapter.html")
    assert text == "ok"
    assert fragments == [{"type": "image", "url": "https://example.com/ok.jpg"}]


def test_settings_reader_show_images_defaults_and_persists(tmp_path):
    path = tmp_path / "settings.json"
    settings = SettingsManager(path)
    assert DEFAULTS["reader"]["show_images"] is True
    assert settings.get("reader", "show_images") is True
    settings.set("reader", "show_images", False)
    settings.save()
    assert json.loads(path.read_text(encoding="utf-8"))["reader"]["show_images"] is False


def test_load_chapter_task_uses_image_api_when_enabled():
    calls = []

    class _Content:
        def fetch_chapter_with_images(self, source, url):
            calls.append((source, url))
            return "body", [{"type": "image", "url": "https://img.example/a.jpg"}]

    chapter = SimpleNamespace(url="chapter", _cached_image_urls=[])
    task = _LoadChapterTask(_Content(), "source", chapter, load_images=True)
    task.run()
    assert calls == [("source", "chapter")]
    assert chapter._cached_image_urls == [{"type": "image", "url": "https://img.example/a.jpg"}]


def test_prefetch_tasks_request_images_when_enabled(_qapp, monkeypatch):
    view = NovelView(object())
    view._source = object()
    view._show_images = True
    view._chapters = [SimpleNamespace(url="one")]
    view._next_prefetch_queue = [0]
    started = []
    monkeypatch.setattr("gui.pages.reader.novel_view.QThreadPool.globalInstance", lambda: SimpleNamespace(start=lambda task: started.append(task)))
    view._pump_next_prefetch()
    assert started[0]._load_images is True


def test_cached_text_without_images_retries_image_api_when_enabled(_qapp, monkeypatch):
    view = NovelView(object())
    view._source = object()
    view._show_images = True
    view._chapters = [SimpleNamespace(url="one", _cached_text="body", _cached_image_urls=[])]
    view._current_idx = 0
    calls = []
    monkeypatch.setattr(view, "_load_chapter", lambda idx: calls.append(idx))
    view._load_chapter(0)
    assert calls == [0]


def test_set_show_images_same_value_is_noop(_qapp, monkeypatch):
    view = NovelView(object())
    view._show_images = True
    old_generation = view._image_generation
    monkeypatch.setattr(view, "_clear_images", lambda: (_ for _ in ()).throw(AssertionError("unexpected clear")))
    view.set_show_images(True)
    assert view._image_generation == old_generation


def test_load_chapter_task_falls_back_to_old_api_when_images_disabled():
    calls = []

    class _Content:
        def fetch_chapter(self, source, url):
            calls.append((source, url))
            return "body"

    chapter = SimpleNamespace(url="chapter")
    task = _LoadChapterTask(_Content(), "source", chapter, load_images=False)
    task.run()
    assert calls == [("source", "chapter")]


def test_novel_view_places_ordered_images_between_paragraphs(_qapp, monkeypatch):
    view = NovelView(object())
    view._source = SimpleNamespace(source_id="sid")
    view._load_generation = 1
    view._show_images = True
    calls = []
    monkeypatch.setattr(view, "_load_image", lambda image, ig, lg: calls.append((image, ig, lg)))
    chapter = SimpleNamespace(title="第一章", url="chapter", _cached_image_urls=[
        {"type": "text", "text": "前"}, {"type": "image", "url": "img-1"},
        {"type": "text", "text": "后"}, {"type": "image", "url": "img-2"}
    ])
    view._chapters = [chapter]
    view._current_idx = 0
    view._display_chapter(chapter, "p1\\np2\\np3")
    assert [item[0] for item in calls] == ["img-1", "img-2"]


def test_novel_mixed_text_segments_follow_font_style(_qapp, monkeypatch):
    view = NovelView(object())
    view._source = SimpleNamespace(source_id="sid")
    view._show_images = True
    monkeypatch.setattr(view, "_load_image", lambda *args: None)
    chapter = SimpleNamespace(
        title="第一章",
        url="chapter",
        _cached_image_urls=[{"type": "text", "text": "正文"}],
    )
    view._chapters = [chapter]
    view._current_idx = 0
    view._display_chapter(chapter, "正文")
    segment = view.scroll_body.findChildren(QLabel, "readerBody")[0]
    initial_style = segment.styleSheet()
    assert "font-family: Georgia" in initial_style
    assert "font-size: 17px" in initial_style
    assert "text-indent: 2em" in initial_style
    assert "margin-bottom: 0.9em" in initial_style
    assert segment.maximumWidth() == 960
    view._adjust_font(3)
    assert "font-size: 20px" in segment.styleSheet()
    view.deleteLater()


def test_novel_view_removes_failed_image_placeholder(_qapp):
    view = NovelView(object())
    view._source = SimpleNamespace(source_id="sid", transports=lambda: {})
    view._load_generation = 1
    view._image_generation = 1
    view._show_images = True
    view._load_image("img", 1, 1)
    assert view._on_image_loaded("img", 1, 1, None) is False
    assert not any(view.scroll_layout.itemAt(i).widget() and view.scroll_layout.itemAt(i).widget().property("image_url") == "img" for i in range(view.scroll_layout.count()))


def test_novel_view_chapter_load_invalidates_images_before_async_load(_qapp):
    view = NovelView(object())
    view._source = object()
    view._chapters = [SimpleNamespace(title="一", url="one"), SimpleNamespace(title="二", url="two")]
    view._current_idx = 0
    view._load_generation = 1
    old_image_generation = view._image_generation
    view._load_chapter(1)
    assert view._image_generation > old_image_generation


def test_novel_view_accepts_legacy_image_strings(_qapp, monkeypatch):
    view = NovelView(object())
    view._show_images = True
    view._load_generation = 1
    calls = []
    monkeypatch.setattr(view, "_load_image", lambda image, ig, lg: calls.append(image))
    chapter = SimpleNamespace(title="第一章", url="chapter", _cached_image_urls=["legacy.jpg"])
    view._chapters = [chapter]
    view._current_idx = 0
    view._display_chapter(chapter, "body")
    assert calls == ["legacy.jpg"]


def test_novel_view_image_setting_rejects_stale_callbacks(_qapp):
    view = NovelView(object())
    view._load_generation = 3
    view._image_generation = 2
    view._cached_image_urls = []
    view._on_image_loaded("old.jpg", 2, 3)
    assert view._cached_image_urls == []


def test_novel_view_cover_loader_options_and_disabled_generation(_qapp, monkeypatch):
    view = NovelView(object())
    view._source = SimpleNamespace(source_id="sid")
    view._current_chapter_url = "https://example.com/chapter"
    view._load_generation = 4
    view._image_generation = 5
    calls = []

    class _Loader:
        def load(self, url, callback, **kwargs):
            calls.append((url, kwargs))

    monkeypatch.setattr("gui.components.cover_loader.CoverLoader.instance", lambda: _Loader())
    view._load_image("https://img.example/a.jpg", 5, 4)
    assert calls == [("https://img.example/a.jpg", {
        "referer": "https://example.com/chapter",
        "cache": False,
        "persist": False,
        "source_id": "sid",
        "source": view._source,
    })]
    view.set_show_images(False)
    assert view._on_image_loaded("late.jpg", 5, 4) is False


def test_reader_page_font_scale_does_not_toggle_images(_qapp, monkeypatch):
    from gui.pages.reader_page import ReaderPage
    calls = []
    page = SimpleNamespace()
    page._font_scale = 1.0
    page.settings = SimpleNamespace(get=lambda *args: True)
    page.novel_view = SimpleNamespace(set_font_scale=lambda value: calls.append(("font", value)))
    page.epub_view = SimpleNamespace(set_font_scale=lambda value: None)
    ReaderPage.apply_font_scale(page, 1.2)
    assert calls == [("font", 1.2)]
