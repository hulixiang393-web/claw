# -*- coding: utf-8 -*-
"""全本小说网封面回填回归测试（test_quanben_cover.py）。

根因：quanben 站列表页/搜索结果 HTML 完全无 <img>（纯文本列表），
封面只在详情页 div.pic img。框架的 cover_backfill 机制（发现页
discover_page / 搜索页 search_page 均支持）此前未在 quanben.json 配置
→ 卡片封面永远空白。修复：works_list_item + search.item 配
cover_backfill=true，后台按需抓详情页封面回填卡片。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_SRC = json.loads(
    (Path(__file__).resolve().parents[1] / "sources" / "quanben.json").read_text("utf-8")
)


def test_quanben_cover_backfill_enabled_on_discovery():
    """发现页列表启用详情封面回填（works_list_item.cover_backfill）。"""
    wi = _SRC["endpoints"]["discovery"]["works_list_item"]
    assert wi.get("cover_backfill") is True


def test_quanben_cover_backfill_enabled_on_search():
    """搜索结果启用详情封面回填（search.item.cover_backfill）。"""
    item = _SRC["endpoints"]["search"]["item"]
    assert item.get("cover_backfill") is True


def test_quanben_detail_cover_selector_extracts_img():
    """详情页封面选择器 div.pic img 能从真实 HTML 切片提取到图片 URL。"""
    from framework.parser import Parser

    html = (
        '<div class="box"><div class="pic">'
        '<img src="http://img.c0m.io/img/12/12957/129575.jpg"/>'
        "</div></div>"
    )
    fields = _SRC["endpoints"]["detail"]["fields"]
    cover = fields["cover"]
    doc = Parser().parse(html)
    got = Parser().extract_first(doc, cover, "https://quanben-xiaoshuo.com/")
    assert got == "http://img.c0m.io/img/12/12957/129575.jpg"


def test_quanben_source_config_loads():
    """源配置可被 SourceConfig 正常加载（不抛校验异常）。"""
    from framework.config import SourceConfig

    cfg = SourceConfig.from_dict(_SRC, "<quanben-test>")
    assert cfg.source_id == "quanben"