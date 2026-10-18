from __future__ import annotations

from types import SimpleNamespace

from gui.pages.reader.novel_view import NovelView


def test_novel_reader_header_shows_book_and_chapter(_qapp):
    view = NovelView(object())
    view._detail = SimpleNamespace(title="混沌圣地")
    chapter = SimpleNamespace(title="第一章 混沌圣地收徒")
    view._chapters = [chapter]
    view._current_idx = 0

    view._update_reader_header(chapter)

    assert view.book_title_label.text() == "混沌圣地"
    assert view.chapter_title_label.text() == "当前章节：第一章 混沌圣地收徒"
    view.deleteLater()


def test_reader_formats_body_with_exactly_two_character_indent(_qapp):
    view = NovelView(object())
    view._paragraph_indent = True
    text = "【第一章】\n\n　　第一段\n        第二段"

    assert view._format_reader_text(text) == "【第一章】\n\n　　第一段\n　　第二段"
    assert view._format_reader_text("第二页正文", has_title=False) == "　　第二页正文"

    view._paragraph_indent = False
    assert view._format_reader_text(text) == "【第一章】\n\n第一段\n第二段"
    view.deleteLater()


def test_novel_reader_header_wraps_long_titles_without_fixed_height(_qapp):
    view = NovelView(object())
    view._detail = SimpleNamespace(
        title="这是一个很长很长的作品标题，用来验证窄窗口下不会被裁切"
    )
    chapter = SimpleNamespace(
        title="第一章 这是一个很长很长的章节标题，用来验证章节行不会和正文错位"
    )
    view._chapters = [chapter]
    view._current_idx = 0
    view.resize(360, 640)
    view.show()
    view._update_reader_header(chapter)
    _qapp.processEvents()

    assert view.book_title_label.wordWrap() is True
    assert view.chapter_title_label.wordWrap() is True
    assert view.reader_header.maximumHeight() > 52
    assert view.reader_header.height() >= view.reader_header.sizeHint().height()
    view.deleteLater()


def test_qk17_reader_renders_each_line_as_a_paragraph(_qapp):
    view = NovelView(object())
    view._source = SimpleNamespace(source_id="qk17")

    view._set_reader_text(view.text, "【第001章】\n\n第一段\n第二段")

    html = view.text.text()
    assert html.count("<p ") == 2
    assert "第一段" in html
    assert "第二段" in html
    assert "chapter-marker" in html
    view.deleteLater()


def test_timotxt_reader_removes_separator_and_uses_paragraph_layout(_qapp):
    view = NovelView(object())
    view._source = SimpleNamespace(source_id="timotxt")
    chapter = SimpleNamespace(title="第一章")
    body = view._prepare_reader_body(
        chapter, "第一章\n_______________\n第一段\n第二段"
    )
    assert body == "第一段\n第二段"

    view._set_reader_text(
        view.text,
        f"【第一章】\n\n{body}",
    )

    html = view.text.text()
    assert "_______________" not in html
    assert html.count("<p ") == 2
    assert "line-height:2.0" in html
    view.deleteLater()
