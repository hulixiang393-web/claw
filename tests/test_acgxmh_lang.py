# -*- coding: utf-8 -*-
"""ACG漫画网「汉化中文」语言分类回归测试（test_acgxmh_lang.py）。

需求：acgxmh 站有语言版本（汉化中文 / 日语 / 英文），用户希望能选简体
中文（汉化中文）作品阅读。站点以独立分类页 /language/chinese-{page}.html
提供，且作品列表复用同一 ul#list 结构。修复：在 discovery.list_item.categories
首位加「汉化中文」分类（{page} 分页占位）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_SRC = json.loads(
    (Path(__file__).resolve().parents[1] / "sources" / "acgxmh.json").read_text("utf-8")
)
_CATS = _SRC["endpoints"]["discovery"]["list_item"]["categories"]


def test_chinese_category_present_and_first():
    """汉化中文分类存在且排在首位（用户优先看到简中作品）。"""
    titles = [c["title"] for c in _CATS]
    assert "汉化中文" in titles
    assert titles[0] == "汉化中文"


def test_chinese_category_url_has_page_placeholder():
    """汉化中文分类 URL 带 {page} 分页占位（第1页 chinese-1.html）。"""
    cat = next(c for c in _CATS if c["title"] == "汉化中文")
    assert cat["url"] == "/language/chinese-{page}.html"


def test_chinese_page_works_parsed():
    """汉化中文页可用 works_list_item 规则解析出作品（真实 HTML 切片）。"""
    from framework.parser import Parser

    html = (
        '<ul id="list">'
        '<li><a class="thumb" href="/h/866647.html" '
        'title="[SeaFox] Succubus [Chinese]">'
        '<img src="https://file3.acgnngca.com/thumb.webp"/></a></li>'
        '<li><a class="thumb" href="/h/866646.html" '
        'title="[Hijinzou] BAD ENDING [Chinese]">'
        '<img src="https://file3.acgnngca.com/t2.webp"/></a></li>'
        "</ul>"
    )
    wi = _SRC["endpoints"]["discovery"]["works_list_item"]
    doc = Parser().parse(html)
    items = Parser().parse_items(
        doc, wi["root_selector"], wi["fields"], "https://www.acgxmh.com/"
    )
    assert len(items) == 2
    assert items[0]["url"] == "https://www.acgxmh.com/h/866647.html"
    assert "Chinese" in items[0]["title"]
    assert items[0]["cover"].endswith("thumb.webp")


def test_acgxmh_source_config_loads():
    from framework.config import SourceConfig

    cfg = SourceConfig.from_dict(_SRC, "<acgxmh-test>")
    assert cfg.source_id == "acgxmh"