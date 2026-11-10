# -*- coding: utf-8 -*-
"""XVideos 源离线回归（2026-10-28）。

夹具来自真实抓取（tests/fixtures/xvideos/，均为免费 tab 响应）：
- ``search_brazzers.html``：免费搜索 tab，27 个视频卡 + 1 个频道卡；
- ``search_premium.html``：付费 tab（``?premium=1``），视频卡带 ``.is-premium`` 角标；
- ``tag_page1.html``：``/tags/xvedio``，3 个视频卡（该标签本身条目很少）；
- ``video_free.html``：单集详情页，``html5player.setVideoHLS(...)`` 是 HLS 主清单。

本文件锁住「只要免费」这条产品约束与取流规则：
1. 列表/搜索都不会吐出频道卡（``.thumb-block-profile``，点进去是用户主页）；
2. 列表/搜索都不会吐出付费卡（卡内 ``.is-premium``）；把同一套配置指向付费 tab
   时结果必须为 0——这是「付费的不用显示」最直接的证明；
3. 取流只取播放器脚本里的 m3u8，不会被相关视频的 ``preview.mp4`` 抢走；
4. 发现分页按站点 0 索引规则生成（第 1 页无后缀，第 2 页 ``/1``）。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from framework.config import load_source  # noqa: E402
from framework.parser import Parser  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "xvideos"
SOURCE_PATH = Path(__file__).resolve().parent.parent / "sources" / "xvideos.json"


@pytest.fixture(scope="module")
def source():
    return load_source(SOURCE_PATH)


@pytest.fixture(scope="module")
def parser():
    return Parser()


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _parse(source, parser, html, key="discovery"):
    cfg = source.get_discovery_config() if key == "discovery" else source.get_search_config()
    item = (cfg.get("works_list_item") if key == "discovery" else cfg.get("item")) or {}
    return parser.parse_items(
        parser.parse(html), item.get("root_selector"), item.get("fields") or {},
        source.base_url, source_id=source.source_id,
    )


def test_source_file_is_valid_v2(source):
    assert source.source_id == "xvideos"
    assert source.content_type == "video"
    assert source.base_url == "https://www.xvideos.com"
    assert source.raw["$schema_version"] == 2
    assert source.raw["$metadata"]["adult"] is True
    # 默认入口就是用户给的标签页
    assert source.get_discovery_config().get("list_url") == "/tags/xvedio"


def test_discovery_tag_page_keeps_free_videos_only(source, parser):
    items = _parse(source, parser, _fixture("tag_page1.html"))
    assert len(items) == 3
    for item in items:
        assert item["url"].startswith(("https://www.xvideos.com/video.", "/video."))
        assert "thumb-block-profile" not in item["url"]
        assert item["title"]
        assert item["cover"].startswith("https://")


def test_search_drops_profile_cards(source, parser):
    items = _parse(source, parser, _fixture("search_brazzers.html"), key="search")
    assert len(items) == 27  # 28 个 thumb-block 里的 1 个频道卡被剔除
    assert all("/video." in item["url"] for item in items)


def test_premium_tab_yields_nothing(source, parser):
    """同一套「只要免费」配置指向付费 tab → 一条都不显示。"""
    assert _parse(source, parser, _fixture("search_premium.html"), key="search") == []


def test_premium_tab_detector_matches_paid_cards(source):
    """确认付费 tab 的卡确实带 .is-premium：防止测试在「夹具没付费卡」时假通过。"""
    doc = Parser().parse(_fixture("search_premium.html"))
    paid = doc.cssselect("div.thumb-block .is-premium")
    assert len(paid) >= 20


def test_play_url_prefers_hls_player_script(source):
    import re

    cfg = source.raw["endpoints"]["content"]["episode"]["play_url"]
    html = _fixture("video_free.html")
    match = re.search(cfg["regex"], html)
    assert match, "详情页夹具里没有 setVideoHLS"
    url = match.group(1)
    assert url.endswith(".m3u8")
    assert "hls-cdn" in url
    # 相关视频 / 缩略图预览是 preview.mp4，不能被当成播放地址
    assert "preview.mp4" not in url


def test_discovery_pagination_is_zero_indexed(source):
    from framework.discovery import Discovery

    build = Discovery._build_page_url.__get__(Discovery.__new__(Discovery))
    pag = source.get_discovery_config().get("list_paginator") or {}
    source.raw["endpoints"]["discovery"]["list_paginator"] = dict(
        pag, type="increment", start=1, step=1, param="page")
    assert build(source, "https://www.xvideos.com/tags/xvedio", 2) == (
        "https://www.xvideos.com/tags/xvedio?page=2")


def test_search_paginator_is_zero_indexed_p(source):
    """搜索分页参数是 `p` 且 0 索引（实测 `?k=…&page=N` 被站点忽略，每页都回第 1 页）。

    配错的表现：搜索只出第一页、结果「显示不完全」。
    """
    pag = source.get_search_config().get("paginator") or {}
    assert pag.get("param") == "p"
    assert pag.get("start") == 0


def test_search_pagination_depth_is_project_standard(source):
    """搜索翻页深度：60 页、不按条数截断。

    旧值 max_pages=10 / max_results=300 会把常见词（milf 等）的搜索结果
    截在约 250 条，用户看到的就是「数量不对」；站点实测 p=50 仍返回新结果，
    故按用户要求放到 60 页（每页约 25 条）。
    """
    cons = source.raw["constraints"]["search"]
    assert cons["max_pages"] == 60
    assert cons["max_results"] == 0


def test_search_page_urls_follow_site_zero_indexing(source):
    from framework.search import Search

    def build(page):
        return Search._build_page_url(
            source=source, base_url="/", keyword="xvedio", kw_param="k",
            page=page, page_param="p", page_start=0,
        )

    assert build(1) == "https://www.xvideos.com/?k=xvedio&p=0"
    assert build(2) == "https://www.xvideos.com/?k=xvedio&p=1"
    # 页码互不相同，才不会被「重复页保护」提前收敛成 1 页
    assert build(1) != build(2) != build(3)


def test_detail_fields_parse_free_video(source, parser):
    detail_cfg = source.get_detail_config()
    doc = parser.parse(_fixture("video_free.html"))
    fields = detail_cfg["fields"]
    title = parser.extract_first(doc, fields["title"], source.base_url)
    cover = parser.extract_first(doc, fields["cover"], source.base_url)
    tags = parser.extract(doc, fields["tags"])
    assert title and "coronavirus" in title.lower()
    assert cover.startswith("https://")
    assert "porn" in tags
    assert "Team Skeet3M" not in tags  # 频道链接（不带 /tags/）不算标签
    assert not any(t.startswith("http") for t in tags)  # 文字标签不能被拼成伪链接


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
