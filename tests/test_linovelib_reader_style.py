"""哔哩轻小说阅读器重排（源内样式，不影响其它源）。"""

from gui.pages.reader.novel_view import NovelView


class _Source:
    source_id = "linovelib"
    base_url = "https://www.linovelib.com"


class _Other:
    source_id = "quanben"
    base_url = "https://quanben-xiaoshuo.com"


def _view(source):
    view = NovelView(object())
    view._source = source
    return view


def test_linovelib_body_is_paragraphized(_qapp):
    view = _view(_Source())
    html = view._linovelib_reader_html(
        "【第1章 序章】\n「眼前是悬崖。」\n　　第二段正文内容。\n请记住本站域名 linovelib\n",
        has_title=True,
    )
    assert "<p " in html and "</p>" in html
    # 章节标记单独居中，不再挤进段落
    assert 'text-align:center' in html
    # 站点广告行被过滤
    assert "请记住本站" not in html
    # 首段保留正文，尾段段落化
    assert "第二段正文内容" in html
    assert html.count("<p ") == 2
    view.deleteLater()


def test_linovelib_strips_duplicate_chapter_title(_qapp):
    view = _view(_Source())

    class _Ch:
        title = "第1章 序章"

    body = view._prepare_reader_body(_Ch(), "第1章 序章\n正文第一段\n正文第二段\n")
    assert body.splitlines()[0] == "正文第一段"
    view.deleteLater()


def test_other_sources_keep_default_plain_render(_qapp):
    view = _view(_Other())
    view.text.setText("")
    view._set_reader_text(view.text, "第一段\n第二段", has_title=False)
    assert "<p " not in view.text.text(), "其它源不应被哔哩轻小说的重排波及"
    view.deleteLater()
