import json
from pathlib import Path

from framework.config import SourceConfig
from framework.content import Content
from framework.parser import Parser


def test_51cg1_inline_dplayers_become_ordered_episodes():
    source = json.loads(
        (Path(__file__).resolve().parent.parent / "sources" / "51cg1.json")
        .read_text(encoding="utf-8")
    )
    episode = source["endpoints"]["content"]["episode"]
    html = (
        '<div class="dplayer" data-video_title="大瓜 001" '
        'data-config=\'{"video":{"url":"https:\\/\\/cdn.example/a.m3u8"}}\'></div>'
        '<div class="dplayer" data-video_title="大瓜 002" '
        'data-config=\'{"video":{"url":"https:\\/\\/cdn.example/b.m3u8"}}\'></div>'
    )

    items = Parser().parse_items(
        Parser().parse(html),
        episode["list"]["root_selector"],
        episode["list"]["fields"],
        "https://51cg1.com",
    )
    assert [item["title"] for item in items] == ["大瓜 001", "大瓜 002"]
    assert [item["url"] for item in items] == [
        "https:\\/\\/cdn.example/a.m3u8",
        "https:\\/\\/cdn.example/b.m3u8",
    ]


def test_51cg1_extracts_current_dplayer_config_with_hls_fallback():
    source = json.loads(
        (Path(__file__).resolve().parent.parent / "sources" / "51cg1.json")
        .read_text(encoding="utf-8")
    )
    episode = source["endpoints"]["content"]["episode"]
    html = (
        '<div class="dplayer" data-video_title="current" '
        'data-config=\'{"live":false,"video_player_ads":[{"src":"ad.gif"}],'
        '"video":{"url":"https:\\/\\/cdn.example/current.m3u8?auth_key=x",'
        '"type":"hls"},"video_h265":{"url":"https:\\/\\/cdn.example/h265.m3u8"}}\''
        '</div>'
    )

    items = Parser().parse_items(
        Parser().parse(html),
        episode["list"]["root_selector"],
        episode["list"]["fields"],
        "https://51cg1.com",
    )
    assert items[0]["url"] == "https:\\/\\/cdn.example/current.m3u8?auth_key=x"


def test_51cg1_escaped_direct_hls_url_is_normalized_before_playback():
    source = SourceConfig.from_dict(
        json.loads(
            (Path(__file__).resolve().parent.parent / "sources" / "51cg1.json")
            .read_text(encoding="utf-8")
        )
    )
    escaped = "https:\\/\\/cdn.example/current.m3u8?auth_key=x"
    resolved = Content(None, Parser(), None).fetch_video_episode(source, escaped)
    assert resolved == "https://cdn.example/current.m3u8?auth_key=x"


def test_51cg1_escaped_direct_hls_url_is_normalized_by_stream_resolver():
    source = SourceConfig.from_dict(
        json.loads(
            (Path(__file__).resolve().parent.parent / "sources" / "51cg1.json")
            .read_text(encoding="utf-8")
        )
    )
    escaped = "https:\\/\\/cdn.example/current.m3u8?auth_key=x"
    streams = Content(None, Parser(), None).fetch_video_streams(
        source, escaped, merged=True
    )
    assert streams == ("https://cdn.example/current.m3u8?auth_key=x", "")


def test_51cg1_discovery_cover_uses_z_bg_loader_url():
    """发现页封面走声明式背景属性（站点已移除 loadBannerDirect）。

    2026-10 站点改版：封面从 `<script>loadBannerDirect('...')</script>` 换成
    `<div class="blog-background" z-bg-loader-url="...">`。旧选择器匹配不到任何
    值，表现为整页封面空白。这里锁定新属性，并保留旧写法作为 fallback。
    """
    source = json.loads(
        (Path(__file__).resolve().parent.parent / "sources" / "51cg1.json")
        .read_text(encoding="utf-8")
    )
    fields = source["endpoints"]["discovery"]["works_list_item"]["fields"]
    html = (
        '<article itemscope itemtype="http://schema.org/BlogPosting">'
        '<a href="/archives/280360/">'
        '<div class="post-card">'
        '<div class="blog-background" '
        'z-bg-loader-url="https://pic.example/cover-a.jpeg" data-no-loading="1"></div>'
        '<h2 class="post-card-title" itemprop="headline">热搜 HOT 标题 A</h2>'
        "</div></a></article>"
        '<article itemscope itemtype="http://schema.org/BlogPosting">'
        '<a href="/archives/280176/">'
        '<div class="post-card">'
        '<div class="blog-background" '
        'z-bg-loader-url="https://pic.example/cover-b.jpeg"></div>'
        '<h2 class="post-card-title" itemprop="headline">标题 B</h2>'
        "</div></a></article>"
    )

    items = Parser().parse_items(
        Parser().parse(html),
        source["endpoints"]["discovery"]["works_list_item"]["root_selector"],
        fields,
        "https://51cg1.com",
    )
    assert [it["cover"] for it in items] == [
        "https://pic.example/cover-a.jpeg",
        "https://pic.example/cover-b.jpeg",
    ]
    assert [it["url"] for it in items] == [
        "https://51cg1.com/archives/280360/",
        "https://51cg1.com/archives/280176/",
    ]


def test_51cg1_search_cover_uses_z_bg_loader_url():
    """搜索页封面与发现页同源，必须同样命中 z-bg-loader-url。"""
    source = json.loads(
        (Path(__file__).resolve().parent.parent / "sources" / "51cg1.json")
        .read_text(encoding="utf-8")
    )
    item_cfg = source["endpoints"]["search"]["item"]
    html = (
        '<article itemscope>'
        '<a href="/archives/1/">'
        '<div class="blog-background" z-bg-loader-url="https://pic.example/s.jpeg"></div>'
        '<h2 class="post-card-title">搜索结果</h2>'
        "</a></article>"
    )
    items = Parser().parse_items(
        Parser().parse(html), item_cfg["root_selector"], item_cfg["fields"], "https://51cg1.com"
    )
    assert items[0]["cover"] == "https://pic.example/s.jpeg"


def test_51cg1_detail_cover_falls_back_to_body_image():
    """详情页 og:image 缺失时回退正文首图（加密站正文图需 img 属性）。"""
    source = json.loads(
        (Path(__file__).resolve().parent.parent / "sources" / "51cg1.json")
        .read_text(encoding="utf-8")
    )
    fields = source["endpoints"]["detail"]["fields"]
    html = (
        '<html><head><meta property="og:title" content="标题"></head><body>'
        '<div class="post-content">'
        '<img data-xkrkllgl="https://pic.example/body.jpeg" src="/placeholder.gif">'
        "</div></body></html>"
    )
    parser = Parser()
    doc = parser.parse(html)
    cover = parser.extract_first(doc, fields["cover"], "https://51cg1.com")
    assert cover == "https://pic.example/body.jpeg"


def test_other_sources_do_not_normalize_escaped_direct_hls_url():
    source = SourceConfig.from_dict({
        "$id": "demo",
        "$type": "video",
        "$name": "Demo",
        "transports": {"base_url": "https://demo.example"},
    })
    escaped = "https:\\/\\/cdn.example/current.m3u8?auth_key=x"
    assert Content(None, Parser(), None).fetch_video_episode(source, escaped) == escaped
    assert Content(None, Parser(), None).fetch_video_streams(
        source, escaped, merged=True
    ) == (escaped, "")


if __name__ == "__main__":
    test_51cg1_inline_dplayers_become_ordered_episodes()
    print("51cg1 source self-check: ok")
