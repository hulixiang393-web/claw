# -*- coding: utf-8 -*-
"""正文容器提取必须保留段落边界（keep_breaks）。

背景（用户反馈「整章只有一段」）：正文选择器常直接指向容器（`.jm-rd-text`、
`div.content`…），站点用 `<br>` 分段。`text_content()` 会丢掉 `<br>`，整章被
粘成一行；正文抽取统一打开 keep_breaks，把 `<br>`/块级边界还原成换行。
"""
from types import SimpleNamespace

from framework.content.chapter import ChapterMixin
from framework.parser import Parser

CONTAINER_HTML = """<html><body>
<div class="jm-rd-text">
  第一段正文，讲了一点事。<br><br>
  第二段正文。<br>
  　　第三段带缩进。<p>第四段是 p 标签。</p>
  尾巴文字
</div></body></html>"""


def _doc():
    return Parser().parse(CONTAINER_HTML)


def test_container_text_keeps_paragraph_breaks():
    parser = Parser()
    values = parser.extract(_doc(), {"css": ".jm-rd-text", "keep_breaks": True})
    assert len(values) == 1
    lines = [line for line in values[0].split("\n") if line.strip()]
    assert lines == [
        "第一段正文，讲了一点事。",
        "第二段正文。",
        "第三段带缩进。",
        "第四段是 p 标签。",
        "尾巴文字",
    ]


def test_without_keep_breaks_text_is_one_line():
    """默认行为保持不变（其它字段仍走 text_content）：压缩 HTML 会被粘成一行。"""
    minified = (
        '<html><body><div class="jm-rd-text">第一段正文。<br>第二段正文。'
        '<br>第三段正文。</div></body></html>'
    )
    parser = Parser()
    values = parser.extract(parser.parse(minified), {"css": ".jm-rd-text"})
    assert len([line for line in values[0].split("\n") if line.strip()]) == 1
    assert values[0] == "第一段正文。第二段正文。第三段正文。"


def test_consecutive_breaks_collapse_to_single_blank_line():
    html = '<div id="c">甲<br><br><br><br>乙</div>'
    parser = Parser()
    text = parser.extract(parser.parse(html), {"css": "#c", "keep_breaks": True})[0]
    assert text.split("\n") == ["甲", "", "乙"]


def test_with_keep_breaks_adds_flag_and_recurses_fallback():
    base = {"css": ".x"}
    assert ChapterMixin._with_keep_breaks(base) == {"css": ".x", "keep_breaks": True}
    # 调用方显式关闭时保持原值
    assert ChapterMixin._with_keep_breaks({"css": ".x", "keep_breaks": False}) == {
        "css": ".x",
        "keep_breaks": False,
    }
    chain = {"fallback": [{"css": "a"}, {"css": "b"}]}
    merged = ChapterMixin._with_keep_breaks(chain)
    assert all(item["keep_breaks"] is True for item in merged["fallback"])


def test_fetch_chapter_splits_br_container_into_paragraphs():
    """整条链路：容器正文 → fetch_chapter → 多段文本。"""
    from framework.config import SourceConfig
    from framework.content import Content

    config = {
        "$schema_version": 2,
        "$id": "demo-novel",
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
                    "body": {"selector": {"css": ".jm-rd-text"}},
                }
            }
        },
    }
    source = SourceConfig.from_dict(config)

    class _Http:
        defaults = SimpleNamespace(timeout=5, retries=0, interval_ms=0)

        def get_text(self, url, **kwargs):
            return CONTAINER_HTML

        def close(self):
            pass

    content = Content(_Http(), Parser(), None)
    text = content.fetch_chapter(source, "https://example.com/novel/1/1.html")
    lines = [line for line in text.split("\n") if line.strip()]
    assert len(lines) >= 4, lines
    assert lines[0].startswith("第一段正文")
    assert "第四段是 p 标签。" in text
