# -*- coding: utf-8 -*-
"""排版设置必须真的改变渲染结果（不只是写盘）。

覆盖两条渲染路径：
- 纯文本路径：正文在 `self.text`（无图章节）；
- 片段路径：正文在若干独立 QLabel（带图片的章节，哔哩轻小说/全本/timo/纵横）。
旧实现里 `_apply_font` 只重排 `self.text`，片段路径上的段间距/行距/字间距
点了完全没反应。
"""
from framework.config import SourceConfig
from framework.content import Chapter
from gui.pages.reader.novel_view import NovelView

TEXT = "\n".join(
    [
        "第一段正文，讲的是主角在雨夜走进一间旧书店，遇到一位不说话的店主。",
        "第二段正文，书架上摆着一本没有书名的册子，封皮已经磨得发白。",
        "第三段正文，他翻开第一页，纸上有淡淡的水痕，字迹却清晰得像刚写上去。",
        "第四段正文，窗外雷声滚过，店主终于抬头，说了一句他听不懂的话。",
        "第五段正文，他把册子合上，决定买下它，尽管他不知道价格是多少。",
        "第六段正文，回到住处，他在灯下一字一字读，读到天亮才合眼。",
    ]
)
WIDTH = 760


def _source():
    return SourceConfig.from_dict({
        "$schema_version": 2,
        "$id": "demo-apply",
        "$type": "novel",
        "$name": "演示",
        "transports": {"base_url": "https://example.com"},
        "endpoints": {
            "content": {
                "chapter": {
                    "list": {"root_selector": {"css": "a"},
                             "fields": {"title": {"css": "a"},
                                        "url": {"css": "a", "attr": "href"}}},
                    "body": {"selector": {"css": ".c"}},
                }
            }
        },
    })


def _view(fragment: bool):
    view = NovelView(object())
    view._source = _source()
    chapter = Chapter(title="第1章 开端", url="https://example.com/1.html")
    chapter._cached_text = TEXT
    chapter._cached_image_urls = [{"type": "text", "text": TEXT}] if fragment else []
    view._chapters = [chapter]
    view._current_idx = 0
    view._show_images = fragment
    view._mode = "scroll"
    view._display_chapter(chapter, TEXT)
    return view


def _body(view):
    """当前可见的正文控件（片段路径取最后一个文本片段，否则取 self.text）。"""
    if view._fragment_text_labels:
        return view._fragment_text_labels[-1][0]
    return view.text


def _height(view):
    return _body(view).heightForWidth(WIDTH)


def test_paragraph_spacing_changes_rendered_height(_qapp):
    for fragment in (False, True):
        view = _view(fragment)
        view._set_paragraph_spacing(0.2)
        low = _height(view)
        view._set_paragraph_spacing(2.0)
        high = _height(view)
        assert high > low + 20, (fragment, low, high)
        view.deleteLater()


def test_line_spacing_changes_rendered_height(_qapp):
    for fragment in (False, True):
        view = _view(fragment)
        view._set_line_spacing(1.5)
        low = _height(view)
        view._set_line_spacing(2.4)
        high = _height(view)
        assert high > low + 20, (fragment, low, high)
        view.deleteLater()


def test_letter_spacing_changes_rendered_height(_qapp):
    for fragment in (False, True):
        view = _view(fragment)
        view._set_letter_spacing(0.0)
        low = _height(view)
        view._set_letter_spacing(0.3)
        high = _height(view)
        assert high > low + 10, (fragment, low, high)
        view.deleteLater()


def test_indent_width_reaches_fragment_body(_qapp):
    view = _view(fragment=True)
    view._set_indent_width(0)
    assert "　" not in _body(view).text().split("</div>")[-1][:40]
    view._set_indent_width(3)
    html = _body(view).text()
    assert html.count("　　　") == len(TEXT.splitlines())
    view.deleteLater()


def test_fragment_labels_keep_same_paragraph_count_after_restyle(_qapp):
    view = _view(fragment=True)
    before = _body(view).text().count("<p ")
    view._set_paragraph_spacing(1.4)
    view._set_line_spacing(2.1)
    view._set_text_align("left")
    after = _body(view).text().count("<p ")
    assert before == after == len(TEXT.splitlines())
    assert "text-align:left" in _body(view).text()
    view.deleteLater()


def test_pager_mode_also_applies_settings(_qapp):
    view = _view(fragment=False)
    view._set_mode("pager")
    assert view._mode == "pager"
    view._set_paragraph_spacing(0.2)
    low = view.paged_label.heightForWidth(WIDTH)
    view._set_paragraph_spacing(2.0)
    high = view.paged_label.heightForWidth(WIDTH)
    assert high > low, (low, high)
    assert "<p " in view.paged_label.text()
    view.deleteLater()
