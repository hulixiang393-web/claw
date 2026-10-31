# -*- coding: utf-8 -*-
"""阅读器的段落/文字排版设置（段间距、首行缩进、对齐、字间距）。

常见小说阅读器的可调项：字号、字体、行距、段间距、首行缩进宽度、对齐方式、
字间距、版心宽度、背景色、翻页方式。这里覆盖段落与文字这一组：既要能改，
也要落到渲染出的 HTML/QTextDocument 上，并且能持久化。
"""
from types import SimpleNamespace

from PySide6.QtGui import QTextCursor

from framework.settings_manager import SettingsManager
from gui.pages.reader.novel_view import NovelView


SAMPLE = "【第1章 开端】\n\n第一段正文，讲了一点事。\n第二段正文，继续讲。"


def _view(settings=None):
    view = NovelView(object(), settings=settings)
    view._source = SimpleNamespace(source_id="czbooks", base_url="https://czbooks.net")
    return view


def _doc(html):
    from PySide6.QtGui import QTextDocument

    doc = QTextDocument()
    doc.setHtml(html)
    return doc


def _first_body_block(doc):
    block = doc.begin().next()  # 跳过章节标题块
    return block


def test_defaults_are_reader_friendly(_qapp):
    view = _view()
    assert view._paragraph_spacing == 0.9
    assert view._indent_width == 2
    assert view._text_align == "justify"
    assert view._letter_spacing == 0.0
    view.deleteLater()


def test_paragraph_spacing_changes_block_margin(_qapp):
    view = _view()
    view._set_paragraph_spacing(1.4)
    assert view._paragraph_spacing == 1.4
    html = view._reader_body_html(SAMPLE, has_title=True)
    assert "margin:0 0 1.4em 0" in html
    doc = _doc(html)
    assert _first_body_block(doc).blockFormat().bottomMargin() > 0
    view.deleteLater()


def test_indent_width_is_materialized_as_fullwidth_spaces(_qapp):
    view = _view()
    view._set_indent_width(1)
    html = view._reader_body_html(SAMPLE, has_title=True)
    assert ">\u3000第一段正文" in html
    assert "\u3000\u3000第一段正文" not in html

    view._set_indent_width(3)
    html = view._reader_body_html(SAMPLE, has_title=True)
    assert ">\u3000\u3000\u3000第一段正文" in html

    # 0 = 不缩进，同时关掉「段首缩进」开关
    view._set_indent_width(0)
    assert view._paragraph_indent is False
    html = view._reader_body_html(SAMPLE, has_title=True)
    assert ">\u3000第一段正文" not in html
    assert ">第一段正文" in html
    view.deleteLater()


def test_alignment_setting_reaches_block_format(_qapp):
    from PySide6.QtCore import Qt

    view = _view()
    view._set_text_align("left")
    html = view._reader_body_html(SAMPLE, has_title=True)
    assert "text-align:left;" in html
    doc = _doc(html)  # 必须持有文档引用：临时 QTextDocument 释放后 QTextBlock 会崩
    block = _first_body_block(doc)
    assert block.blockFormat().alignment() & Qt.AlignLeft

    view._set_text_align("justify")
    html = view._reader_body_html(SAMPLE, has_title=True)
    assert "text-align:justify;" in html
    view.deleteLater()


def test_letter_spacing_setting_reaches_char_format(_qapp):
    view = _view()
    view._set_letter_spacing(0.1)
    html = view._reader_body_html(SAMPLE, has_title=True)
    assert "letter-spacing:0.1em" in html
    doc = _doc(html)
    cursor = QTextCursor(doc)
    cursor.movePosition(QTextCursor.Start)
    cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor)
    assert cursor.charFormat().fontLetterSpacing() > 100

    view._set_letter_spacing(0.0)
    html = view._reader_body_html(SAMPLE, has_title=True)
    assert "letter-spacing:0" not in html.replace("letter-spacing:0.18em", "")
    view.deleteLater()


def test_settings_persist_and_reload(_qapp, tmp_path):
    settings = SettingsManager(tmp_path / "settings.json")
    view = _view(settings)
    view._set_paragraph_spacing(2.0)
    view._set_indent_width(1)
    view._set_text_align("left")
    view._set_letter_spacing(0.15)
    view.deleteLater()

    saved = SettingsManager(tmp_path / "settings.json")
    assert saved.get("reader", "paragraph_spacing") == 2.0
    assert saved.get("reader", "paragraph_indent_width") == 1
    assert saved.get("reader", "text_align") == "left"
    assert saved.get("reader", "letter_spacing") == 0.15

    reloaded = _view(saved)
    assert reloaded._paragraph_spacing == 2.0
    assert reloaded._indent_width == 1
    assert reloaded._text_align == "left"
    assert reloaded._letter_spacing == 0.15
    assert reloaded._para_actions[2.0].isChecked()
    assert reloaded._indent_actions[1].isChecked()
    assert reloaded._align_actions["left"].isChecked()
    assert reloaded._letter_actions[0.15].isChecked()
    reloaded.deleteLater()


def test_reset_restores_paragraph_defaults(_qapp):
    view = _view()
    view._set_paragraph_spacing(2.0)
    view._set_indent_width(3)
    view._set_text_align("left")
    view._set_letter_spacing(0.15)
    view._reset_reader_options()
    assert view._paragraph_spacing == 0.9
    assert view._indent_width == 2
    assert view._text_align == "justify"
    assert view._letter_spacing == 0.0
    view.deleteLater()


def test_out_of_range_values_are_clamped(_qapp):
    view = _view()
    view._set_paragraph_spacing(9.0)
    assert view._paragraph_spacing == 2.4
    view._set_indent_width(-3)
    assert view._indent_width == 0
    view._set_letter_spacing(5.0)
    assert view._letter_spacing == 0.3
    view._set_text_align("center")
    assert view._text_align == "justify"
    view.deleteLater()


def test_rendered_body_reflects_spacing_and_indent(_qapp):
    """整体接管：改设置后正文 HTML 里的段距与缩进同时生效。"""
    view = _view()
    view._set_paragraph_spacing(1.4)
    view._set_indent_width(1)
    text = "【第1章】\n\n第一段正文。\n第二段正文。"
    view._set_reader_text(view.text, text)
    html = view.text.text()
    assert html.count("<p ") == 2
    assert "margin:0 0 1.4em 0" in html
    assert html.count("\u3000第一段正文") == 1
    view.deleteLater()
