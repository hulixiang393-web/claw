# -*- coding: utf-8 -*-
"""通用阅读排版回归（所有纯文本小说源共用一套排版）。

背景：正文原来按源各写一套（qk17/timotxt/linovelib 有段落块，其它源是一整块
<br/> 或纯文本），默认档位下段间没有留白、行距不生效，长章节看起来像文字墙。
现在统一走 `_reader_body_html`：一段一块 <p>、段间距 0.9em、行距跟随阅读设置
（默认 2.0）、首行缩进两个全角空格、两端对齐，章节标题单独居中。
"""
from types import SimpleNamespace

from PySide6.QtGui import QTextDocument

from gui.pages.reader.novel_view import NovelView

SAMPLE = "【第1章 序章】\n\n第一段正文。\n第二段正文。\n\n第三段正文。"


def _view(source_id="quanben", **opts):
    view = NovelView(object())
    view._source = SimpleNamespace(source_id=source_id, base_url="https://example.com")
    for key, value in opts.items():
        setattr(view, key, value)
    return view


def _doc(html: str) -> QTextDocument:
    doc = QTextDocument()
    doc.setHtml(html)
    return doc


def test_generic_source_renders_one_block_per_paragraph(_qapp):
    view = _view()
    html = view._reader_body_html(SAMPLE, has_title=True)
    assert html.count("<p ") == 3
    assert "chapter-marker" in html          # 章节标题单独成块
    assert "第一段正文。</p>" in html
    assert '<div class="chapter-marker"' in html.split("<p ")[0]
    view.deleteLater()


def test_part_blocks_get_spacing_line_height_and_indent(_qapp):
    view = _view()
    doc = _doc(view._reader_body_html(SAMPLE, has_title=True))
    body = doc.begin().next()                # 跳过标题块
    fmt = body.blockFormat()
    assert fmt.lineHeight() == 200.0         # 默认档位 → 2.0 倍行高
    assert fmt.bottomMargin() > 0            # 段间距真实生效
    assert body.text().startswith("　　")     # 两字缩进（U+3000 实体化）
    view.deleteLater()


def test_line_spacing_setting_reaches_paragraph_blocks(_qapp):
    view = _view(_line_spacing=2.4)
    doc = _doc(view._reader_body_html(SAMPLE, has_title=True))
    assert doc.begin().next().blockFormat().lineHeight() == 240.0
    view.deleteLater()


def test_indent_setting_off_keeps_paragraphs_flush_left(_qapp):
    view = _view(_paragraph_indent=False)
    doc = _doc(view._reader_body_html(SAMPLE, has_title=True))
    assert not doc.begin().next().text().startswith("　　")
    assert "第一段正文。" in doc.begin().next().text()
    view.deleteLater()


def test_no_title_means_every_line_is_a_paragraph(_qapp):
    view = _view()
    html = view._reader_body_html("第一段\n第二段", has_title=False)
    assert html.count("<p ") == 2
    assert "chapter-marker" not in html
    view.deleteLater()


def test_blank_lines_do_not_produce_empty_paragraphs(_qapp):
    view = _view()
    html = view._reader_body_html("甲\n\n\n乙\n\n", has_title=False)
    assert html.count("<p ") == 2
    assert "<p style" in html and "></p>" not in html.replace("<p style=\"margin", "")
    view.deleteLater()


def test_set_reader_text_uses_generic_layout_for_new_sources(_qapp):
    """新源/未单独适配的源（如 shuyous、18mh）也开箱即用同一套排版。"""
    view = _view("shuyous")
    view._set_reader_text(view.text, SAMPLE, has_title=True)
    html = view.text.text()
    assert html.count("<p ") == 3
    assert "line-height:2.0" in html
    view.deleteLater()
