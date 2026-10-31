# -*- coding: utf-8 -*-
"""阅读器段落兜底：上游把整章挤成一行时，前端仍要分成多段。

背景（用户反馈「整章只有一段，所有段落都塞进一段里」）：正文选择器指向容器
时，站点用 `<br>` 分段，`text_content()` 会把整章粘成一行；有些源则用段首
「　　两个全角空格」当段标记、完全没有换行。阅读器必须先按标记切开，才能段落化。
"""
from types import SimpleNamespace

from gui.pages.reader.novel_view import NovelView


def _view(source_id="czbooks"):
    view = NovelView(object())
    view._source = SimpleNamespace(source_id=source_id, base_url="https://czbooks.net")
    return view


def _lines(text):
    return [line for line in text.splitlines() if line.strip()]


def test_fullwidth_indent_marks_split_one_line_chapter(_qapp):
    view = _view()
    one_line = "　　第一段正文，讲了一点事。" + "".join(
        f"　　第{i}段正文，继续讲故事，做一点事。" for i in range(2, 8)
    )
    assert len(_lines(one_line)) == 1
    out = view._split_merged_paragraphs(one_line)
    assert len(_lines(out)) == 7, _lines(out)
    assert all(line.startswith("第") for line in _lines(out))
    view.deleteLater()


def test_br_markers_split_one_line_chapter(_qapp):
    view = _view()
    one_line = "第一段正文。<br>第二段正文。<br/><br>第三段正文。"
    out = view._split_merged_paragraphs(one_line)
    assert len(_lines(out)) == 3
    assert "第三段正文。" in out
    view.deleteLater()


def test_carriage_return_only_text_splits(_qapp):
    view = _view()
    text = "\r".join(["第一段正文。" * 5, "第二段正文。" * 5])
    out = view._split_merged_paragraphs(text)
    assert len(_lines(out)) == 2
    view.deleteLater()


def test_markerless_wall_is_packed_into_readable_paragraphs(_qapp):
    view = _view()
    wall = "".join(
        "他抬起头看向远处的山脊，风把尘土吹得满地都是。" for _ in range(20)
    )
    assert len(_lines(wall)) == 1
    out = view._split_merged_paragraphs(wall)
    lines = _lines(out)
    assert len(lines) > 1, "无任何段标记时也要按句子聚合分段"
    assert all(NovelView._PACK_MIN_CHARS - 40 <= len(line) for line in lines[:-1])
    assert all(len(line) <= NovelView._PACK_MAX_CHARS + 40 for line in lines)
    view.deleteLater()


def test_multi_line_text_is_left_alone(_qapp):
    view = _view()
    text = "第一段。\n第二段。\n第三段。"
    assert view._split_merged_paragraphs(text) == text
    view.deleteLater()


def test_short_single_line_is_not_split(_qapp):
    view = _view()
    assert view._split_merged_paragraphs("很短的一行。") == "很短的一行。"
    view.deleteLater()


def test_prepare_body_then_render_yields_paragraph_blocks(_qapp):
    view = _view()
    one_line = "　　第一段正文，讲了一点事。" + "".join(
        f"　　第{i}段正文，继续讲故事。" for i in range(2, 6)
    )
    body = view._prepare_reader_body(SimpleNamespace(title="第1章"), one_line)
    html = view._reader_body_html(f"【第1章】\n\n{body}", has_title=True)
    assert html.count("<p ") == 5
    view._set_reader_text(view.text, f"【第1章】\n\n{body}")
    assert view.text.text().count("<p ") == 5
    view.deleteLater()
