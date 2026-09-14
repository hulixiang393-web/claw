# -*- coding: utf-8 -*-
"""爱丽丝书屋源可用性回归测试（test_alicesw_source.py）。

背景：原域名 www.alicesw.com 在国内被墙/超时，页面加载不出来。官方发布页
（alicesw.net / alicesw.quest / alicesw.click）给出大陆可用域名：
  - 大陆发布：https://www.alicesw1.homes
  - 备用：    https://alicesw.quest / https://alicesw.click
  - 海外永久：https://www.alicesw.com（国内不可达）
修复：base_url 指向国内可达域名 + transports.direct=true（屏蔽系统代理直连）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_SRC = json.loads(
    (Path(__file__).resolve().parents[1] / "sources" / "alicesw.json").read_text("utf-8")
)


def test_base_url_is_domestic_reachable_domain():
    """base_url 指向国内可达域名（非被墙的 www.alicesw.com）。"""
    assert _SRC["transports"]["base_url"] == "https://www.alicesw1.homes"
    assert "alicesw.com" not in _SRC["transports"]["base_url"]


def test_transports_direct_enabled():
    """启用 direct 直连（屏蔽系统代理，国内源在加速器代理下也能开）。"""
    assert _SRC["transports"].get("direct") is True


def test_alicesw_source_config_loads():
    from framework.config import SourceConfig

    cfg = SourceConfig.from_dict(_SRC, "<alicesw-test>")
    assert cfg.source_id == "alicesw"
    assert cfg.base_url == "https://www.alicesw1.homes"


def test_list_selector_parses_novel_links():
    """列表 li.two 解析作品链接（真实 HTML 切片）。"""
    from framework.parser import Parser

    html = (
        '<ul class="mulu_list">'
        '<li class="two"><a href="/novel/51816.html">李富贵的幸福生活</a></li>'
        '<li class="two"><a href="/novel/52854.html">随笔</a></li>'
        "</ul>"
    )
    wi = _SRC["endpoints"]["discovery"]["works_list_item"]
    doc = Parser().parse(html)
    items = Parser().parse_items(
        doc, wi["root_selector"], wi["fields"], "https://www.alicesw1.homes/"
    )
    assert len(items) == 2
    assert items[0]["url"] == "https://www.alicesw1.homes/novel/51816.html"


def test_detail_cover_and_title_selectors():
    """详情页封面（data-src 懒加载）+ 标题提取。"""
    from framework.parser import Parser

    html = (
        '<div class="novel_title">李富贵的幸福生活</div>'
        '<img class="lazyload_book_cover" '
        'data-src="https://img.321cdn.com/uploads/x.webp" src="data:image/gif"/>'
    )
    fields = _SRC["endpoints"]["detail"]["fields"]
    doc = Parser().parse(html)
    p = Parser()
    assert p.extract_first(doc, fields["title"]) == "李富贵的幸福生活"
    assert p.extract_first(doc, fields["cover"]) == "https://img.321cdn.com/uploads/x.webp"


def test_chapter_body_selector_extracts_paragraphs():
    """正文 div.read-content.j_readContent p 段落提取。"""
    from framework.parser import Parser

    html = (
        '<div class="read-content j_readContent">'
        "<p>　　第一段正文。</p><p>　　第二段正文。</p>"
        "</div>"
    )
    sel = _SRC["endpoints"]["content"]["chapter"]["body"]["selector"]
    doc = Parser().parse(html)
    paras = Parser().extract(doc, sel)
    assert paras == ["第一段正文。", "第二段正文。"]