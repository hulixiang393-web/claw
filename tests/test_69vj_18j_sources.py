"""Offline regression checks for the 69VJ and 18J video source contracts."""

from pathlib import Path

import pytest

from framework.config import load_source
from framework.content import Content
from framework.discovery import Discovery
from framework.parser import Parser


ROOT = Path(__file__).resolve().parents[1]


class _Http:
    defaults = type("Defaults", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def __init__(self, pages):
        self.pages = pages

    def get_text(self, url, headers=None, **kwargs):
        return self.pages[url]


class _Checker:
    pass


def _source(source_id):
    return load_source(ROOT / "sources" / f"{source_id}.json")


def _disable_selfcheck(source):
    source.raw.setdefault("diagnostics", {})["selfcheck"] = {"strategy": "off"}
    return source


def test_new_video_sources_have_metadata_and_detail_tags():
    for source_id in ("18j", "69vj"):
        source = _source(source_id)
        assert source.content_type == "video"
        assert source.raw["$metadata"]["tags"]
        assert source.raw["endpoints"]["detail"]["fields"]["tags"]
        assert source.raw["endpoints"]["content"]["episode"]["single_chapter"] is True


@pytest.mark.skipif(not Parser()._lxml, reason="lxml is required for selector regression")
def test_18j_list_detail_and_hls_extraction():
    source = _disable_selfcheck(_source("18j"))
    list_url = source.get_discovery_config()["list_url"]
    list_page_url = "https://18j.tv/label/hot/by/time/page/1"
    detail_url = "https://18j.tv/v/123-1-1/"
    detail_html = """
    <html><head>
      <meta property="og:image" content="https://img.example/poster.webp">
      <meta property="video:section" content="JAV自拍">
      <meta property="video:tag" content="独家">
      <script type="application/ld+json">{"contentUrl":"https://cdn.example/video/index.m3u8"}</script>
    </head><body>
      <h1 class="play-title">Example 18J</h1>
      <video><source src="https://ads.example/preview.mp4"></video>
    </body></html>
    """
    pages = {
            list_page_url: '<ul class="list"><li><a href="/v/123-1-1/" title="Example 18J"><div class="vodlist_img"><img data-original="https://img.example/poster.webp"><span>JAV自拍</span></div></a></li></ul>',
        detail_url: detail_html,
    }
    http = _Http(pages)
    parser = Parser()
    works = Discovery(http, parser, _Checker()).list_works(source, list_url)
    assert works[0].url == detail_url
    content = Content(http, parser, _Checker())
    detail = content.fetch_detail(source, detail_url)
    assert detail.tags == ["JAV自拍", "独家"]
    assert content.fetch_video_episode(source, detail.chapters[0].url).endswith("index.m3u8")


@pytest.mark.skipif(not Parser()._lxml, reason="lxml is required for selector regression")
def test_69vj_iframe_chain_extracts_hls():
    source = _disable_selfcheck(_source("69vj"))
    detail_url = "https://69vj.com/video/123-example.html"
    iframe_url = "https://www.xvideos.com/embedframe/123"
    pages = {
        detail_url: f"""
          <meta property="og:title" content="Example 69VJ">
          <meta property="og:image" content="https://img.example/poster.jpg">
          <meta property="video:tag" content="日本">
          <h1 class="title"><span itemprop="name">Example 69VJ</span></h1>
          <iframe src="{iframe_url}"></iframe>
        """,
        iframe_url: "<script>html5player.setVideoHLS('https://cdn.example/live/index.m3u8');</script>",
    }
    content = Content(_Http(pages), Parser(), _Checker())
    detail = content.fetch_detail(source, detail_url)
    assert detail.tags == ["日本"]
    assert content.fetch_video_episode(source, detail.chapters[0].url) == "https://cdn.example/live/index.m3u8"


@pytest.mark.skipif(not Parser()._lxml, reason="lxml is required for selector regression")
def test_jptt_http_list_and_data_src_playback():
    source = _disable_selfcheck(load_source(ROOT / "sources" / "jptt.json"))
    list_url = "https://jptt.tv/"
    detail_url = "https://jptt.tv/video/GVH-885?output=1"
    pages = {
        list_url: (
            '<ul class="dx-video-list"><li><a href="/video/GVH-885">'
            '<img class="index_video_cover" alt="JPTT example" '
            'src="https://img.example/cover.jpg"></a></li></ul>'
        ),
        detail_url: (
            '<h1>JPTT example</h1><video id="f-player">'
            '<source data-src="//cdn.example/live/index.m3u8"></video>'
        ),
    }
    http = _Http(pages)
    pages["https://jptt.tv/?page=1"] = pages.pop(list_url)
    works = Discovery(http, Parser(), _Checker()).list_works(source, list_url)
    assert works[0].title == "JPTT example"
    assert works[0].url == "https://jptt.tv/video/GVH-885"
    content = Content(http, Parser(), _Checker())
    assert content.fetch_video_episode(source, detail_url) == "https://cdn.example/live/index.m3u8"


def test_18mh_cover_config_keeps_lazy_image_fallbacks():
    source = _source("18mh")
    cover = source.get_search_config()["item"]["fields"]["cover"]
    assert [item["attr"] for item in cover["fallback"]] == [
        "data-src", "data-original", "src"
    ]
