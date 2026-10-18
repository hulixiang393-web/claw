"""离线契约测试：JPTT.TV 源配置与 2024 改版后的 DOM 选择器。

站点卡片从 `ul.dx-video-list > li` 改为 `div.oneVideo`；列表/搜索/标签页封面
img 的 class 为 `card-img-top`（仅首页带 `index_video_cover`），旧选择器命中
0 导致发现页与搜索空白。用例锁定新契约，避免再次回归。
"""

from pathlib import Path

import pytest

from framework.config import load_source
from framework.parser import Parser


ROOT = Path(__file__).resolve().parents[1]


def _source():
    return load_source(ROOT / "sources" / "jptt.json")


def _cat_titles(source):
    cats = source.get_discovery_config()["list_item"]["categories"]
    return {c["title"]: c["url"] for c in cats}


def test_jptt_card_selectors_cover_all_page_types():
    source = _source()
    disc = source.get_discovery_config()
    search = source.get_search_config()
    assert disc["works_list_item"]["root_selector"]["css"] == "div.oneVideo"
    assert search["item"]["root_selector"]["css"] == "div.oneVideo"
    # 封面回退链必须包含 card-img-top（tag_list/search/list 页只有该类）
    cover_attrs = [
        (s.get("css"), s.get("attr"))
        for s in disc["works_list_item"]["fields"]["cover"]["fallback"]
    ]
    assert ("img.card-img-top", "src") in cover_attrs


def test_jptt_pagination_uses_idx_parameter():
    source = _source()
    disc = source.get_discovery_config()
    assert disc["list_paginator"]["param"] == "idx"
    assert disc["list_paginator"]["first_page_without_param"] is True
    # 搜索分页同样使用真实参数 idx（?page=N 站点无效）
    tmpl = source.get_search_config()["paginator"]["url_template"]
    assert "idx={page}" in tmpl
    assert "page={page}" not in tmpl


def test_jptt_categories_include_latest_and_tag_urls():
    cats = _cat_titles(_source())
    assert cats.get("最新") == "/"
    tag_urls = [u for u in cats.values() if u.startswith("/tag_list?")]
    assert len(tag_urls) >= 12
    assert all("tid=" in u or "fid=" in u for u in tag_urls)
    # 片商分类需有清晰前缀区分
    assert any(t.startswith("片商:") for t in cats)


def test_jptt_selfcheck_and_play_selector():
    source = _source()
    sc = source.raw["diagnostics"]["selfcheck"]
    assert sc["selector"]["css"] == "div.oneVideo"
    play = source.raw["endpoints"]["content"]["episode"]["play_url"]["selector"]["fallback"]
    assert play[0] == {"css": "video source[data-src]", "attr": "data-src"}


@pytest.mark.skipif(not Parser()._lxml, reason="lxml is required for selector regression")
def test_jptt_taglist_card_without_index_cover_class_parses():
    """tag_list 卡片 img 无 index_video_cover：仍能取到 title/cover/url。"""
    source = _source()
    wi = source.get_discovery_config()["works_list_item"]
    html = (
        '<div class="col oneVideo"><div class="card h-100">'
        '<div class="oneVideo-top" data-src="/storage/preview/77492.mp4">'
        '<a href="https://jptt.tv/video/SSIS-816-chinese">'
        '<img loading="lazy" src="/storage/cover/1/77492.jpg?1792268279" '
        'class="card-img-top" alt="SSIS-816 超級明星女優大亂交"></a></div>'
        '</div></div>'
    )
    parser = Parser()
    items = parser.parse_items(
        parser.parse(html), wi["root_selector"], wi["fields"], source.base_url
    )
    assert len(items) == 1
    assert items[0]["title"] == "SSIS-816 超級明星女優大亂交"
    assert items[0]["url"] == "https://jptt.tv/video/SSIS-816-chinese"
    assert items[0]["cover"] == "https://jptt.tv/storage/cover/1/77492.jpg?1792268279"
