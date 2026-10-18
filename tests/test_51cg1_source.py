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
