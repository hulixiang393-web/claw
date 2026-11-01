# -*- coding: utf-8 -*-
"""图片片段路径的段落排版（「整章只有一段」的第二处根因）。

GUI 抓正文走 `fetch_chapter_with_images`：返回的片段表里既有图片也有文本片段，
阅读器只要看到片段就改走「片段渲染」。这条路径上文本片段曾经是：
  1. `_extract_body_fragments` 把 root 段落节点依次拼接、**不补换行** → 整章一行；
  2. 阅读器把文本片段当纯文本 setText → 没有段距/缩进，看着就是一整段。
哔哩轻小说 / 全本 / timo / 纵横都踩到这条路径。
"""
from types import SimpleNamespace

from framework.config import SourceConfig
from framework.content import Chapter, Content
from framework.parser import Parser
from gui.pages.reader.novel_view import NovelView

PAGE = """<html><body>
<div class="jm-rd-read">
  <p>第一段正文。</p><p>第二段正文。</p>
  <p><img src="/img/1.jpg" /></p>
  <p>第三段正文。</p><p>第四段正文。</p>
</div></body></html>"""


def _source():
    return SourceConfig.from_dict({
        "$schema_version": 2,
        "$id": "demo-frag",
        "$type": "novel",
        "$name": "演示",
        "transports": {"base_url": "https://example.com", "retries": 0},
        "endpoints": {
            "content": {
                "chapter": {
                    "list": {
                        "root_selector": {"css": "ul li a"},
                        "fields": {
                            "title": {"css": "a"},
                            "url": {"css": "a", "attr": "href"},
                        },
                    },
                    "body": {"selector": {"css": ".jm-rd-read p"}},
                }
            }
        },
    })


class _Http:
    defaults = SimpleNamespace(timeout=5, retries=0, interval_ms=0)

    def get_text(self, url, **kwargs):
        return PAGE

    def close(self):
        pass


def _content():
    return Content(_Http(), Parser(), None)


def test_text_fragment_keeps_paragraph_breaks():
    text, images = _content().fetch_chapter_with_images(
        _source(), "https://example.com/novel/1/1.html"
    )
    fragments = [e for e in images if e.get("type") == "text"]
    assert fragments, images
    merged = "".join(str(e.get("text") or "") for e in fragments)
    lines = [line for line in merged.splitlines() if line.strip()]
    assert lines == ["第一段正文。", "第二段正文。", "第三段正文。", "第四段正文。"]

    # 纯文本正文这条链路同样要有多段
    assert len([line for line in text.splitlines() if line.strip()]) >= 4


def test_reader_renders_text_fragment_as_paragraphs(_qapp):
    text, images = _content().fetch_chapter_with_images(
        _source(), "https://example.com/novel/1/1.html"
    )
    chapter = Chapter(title="第1章 开端", url="https://example.com/novel/1/1.html")
    chapter._cached_text = text
    chapter._cached_image_urls = images  # GUI 就是把整张片段表存在这里
    assert any(e.get("type") == "text" for e in chapter._cached_image_urls)

    view = NovelView(object())
    view._source = _source()
    view._chapters = [chapter]
    view._current_idx = 0
    view._show_images = True
    view._mode = "scroll"
    view._display_chapter(chapter, text)

    labels = [
        widget for widget in view.scroll_body.findChildren(type(view.text))
        if widget.objectName() == "readerBody" and widget is not view.text
    ]
    bodies = [label.text() for label in labels if "<p " in label.text()]
    assert len(bodies) == 2, bodies  # 图片把正文切成前后两段文本
    assert sum(body.count("<p ") for body in bodies) == 4
    assert "第一段正文。" in bodies[0]
    view.deleteLater()


def test_merged_fragment_text_is_split_before_render(_qapp):
    """老数据/异常源给来的一行片段也要在阅读器里拆成多段。"""
    chapter = Chapter(title="第1章", url="https://example.com/novel/1/2.html")
    merged = "　　第一段正文，讲了一点事。" + "".join(
        f"　　第{i}段正文，继续讲故事。" for i in range(2, 7)
    )
    chapter._cached_text = merged
    chapter._cached_image_urls = [{"type": "text", "text": merged}]

    view = NovelView(object())
    view._source = _source()
    view._chapters = [chapter]
    view._current_idx = 0
    view._show_images = True
    view._mode = "scroll"
    view._display_chapter(chapter, merged)

    labels = [
        widget for widget in view.scroll_body.findChildren(type(view.text))
        if widget.objectName() == "readerBody" and widget is not view.text
    ]
    bodies = [label.text() for label in labels if "<p " in label.text()]
    assert bodies and bodies[0].count("<p ") >= 6
    view.deleteLater()


# --------------------------------------------------------------------------- #
# 字体混淆（反爬子集字体）：图文片段必须与主正文走同一条解码链
# timotxt 实测：正文链路已解码，但片段原文来自 DOM，阅读器渲染片段时露出
# 「雍正十三뎃的冬天」这类假字。
# --------------------------------------------------------------------------- #
FAKE_PAGE = """<html><body>
<div class="jm-rd-read">
  <p>第1章 开端</p>
  <p>雍正十三뎃的冬天，比往뎃冷得早。</p>
  <p><img src="/img/1.jpg" /></p>
  <p>臘梅剛冒눕花苞，就被北風裹上白霜。</p>
</div></body></html>"""


class _FontDecrypter:
    """最小解密器：记录调用，并把假字换回真字。"""

    TABLE = {"뎃": "年", "눕": "出"}

    def __init__(self):
        self.calls = []

    def decrypt(self, source, text, target="content", html="", font_urls=None):
        self.calls.append(str(text))
        out = str(text)
        for fake, real in self.TABLE.items():
            out = out.replace(fake, real)
        return out


class _HttpFake(_Http):
    def get_text(self, url, **kwargs):
        return FAKE_PAGE


def _fake_count(text):
    return sum(1 for ch in str(text) if 0xAC00 <= ord(ch) <= 0xD7A3)


def test_text_fragments_are_decoded_like_body():
    decrypter = _FontDecrypter()
    content = Content(_HttpFake(), Parser(), None, decrypter=decrypter)
    text, fragments = content.fetch_chapter_with_images(
        _source(), "https://example.com/novel/1/1.html"
    )
    assert _fake_count(text) == 0, text
    texts = [str(e.get("text") or "") for e in fragments if e.get("type") == "text"]
    assert texts, fragments
    assert all(_fake_count(item) == 0 for item in texts), texts
    # 片段里的假字确实被还原成真字，而不是被删掉
    joined = "".join(texts)
    assert "雍正十三年的冬天" in joined and "冒出花苞" in joined
    # 主正文与片段都经过解密链（不是只解了其中一条）
    assert any("雍正十三" in call for call in decrypter.calls)
