"""Offline regression checks for the Rou.video and 18AV source contracts."""

import base64
import json
from pathlib import Path
from unittest.mock import patch

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


@pytest.mark.skipif(not Parser()._lxml, reason="lxml is required for selector regression")
def test_rouvideo_list_detail_and_dynamic_shifted_payload():
    source = _disable_selfcheck(_source("rouvideo"))
    detail_url = "https://rou.video/v/example"
    payload = json.dumps({"videoUrl": "/api/hls/example", "thumbVTTUrl": "/thumbs"})
    shift = 7
    encoded = base64.b64encode(bytes((byte + shift) & 0xFF for byte in payload.encode())).decode()
    pages = {
        "https://rou.video/home?page=1": (
            '<a href="/v/example"><img src="https://img.example/cover.jpg">'
            "<h3>Rou Example</h3></a>"
        ),
        detail_url: (
            '<meta name="description" content="summary">'
            '<video class="rv-player-video" poster="https://img.example/detail.jpg"></video>'
            '<h1>Rou Example</h1><a href="/t/%E6%8E%A2%E8%8A%B1">探花</a>'
            f'<script>ev:$R[92]={{d:"{encoded}",k:{shift}}}</script>'
        ),
    }
    http = _Http(pages)
    discovery = Discovery(http, Parser(), _Checker())
    works = discovery.list_works(source, "/home")
    assert works[0].title == "Rou Example"
    assert works[0].url == detail_url
    content = Content(http, Parser(), _Checker())
    detail = content.fetch_detail(source, detail_url)
    assert detail.title == "Rou Example"
    assert detail.cover.endswith("detail.jpg")
    assert detail.tags == ["探花"]
    assert content.fetch_video_episode(source, detail.chapters[0].url) == (
        "https://rou.video/api/hls/example"
    )


def _base13(value):
    digits = "0123456789abc"
    if value == 0:
        return "0"
    out = []
    while value:
        value, remainder = divmod(value, 13)
        out.append(digits[remainder])
    return "".join(reversed(out))


def _protected_player_payload(
    player_id,
    delimiter="n",
    radix=13,
    xor_key=14,
    key=b"0e4de3a7c8020b9d",
    iv=b"f86d5f74468c3ab5",
):
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(128).padder()
    padded = padder.update(player_id.encode()) + padder.finalize()
    encryptor = Cipher(
        algorithms.AES(key),
        modes.CBC(iv),
    ).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    encoded = base64.b64encode(encrypted).decode()
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    def encode(value):
        out = []
        while value:
            value, remainder = divmod(value, radix)
            out.append(digits[remainder])
        return "".join(reversed(out)) or "0"
    return delimiter.join(encode(ord(char) ^ xor_key) for char in encoded)


@pytest.mark.skipif(not Parser()._lxml, reason="lxml is required for selector regression")
def test_18av_list_detail_and_protected_player_hls():
    source = _disable_selfcheck(_source("18av"))
    detail_url = "https://18av.mm-cg.com/zh/chinese_content/22817/content.html"
    player_id = "-Bx3iKK94zPIHmjjDselfEw4mkgd3QJP7mfEpdTqVe1KvLDbE0u1CD9LnCkiH4it"
    player_url = (
        "https://18av.mm-cg.com/js/player/play.php?numresolution=1080&id="
        + player_id
    )
    stream_url = "https://cdn.example/18av-22817.m3u8"
    dynamic_key = b"827b6028b49ec12d"
    dynamic_iv = b"3818175f7ec89f6f"
    encrypted = _protected_player_payload(
        player_id,
        delimiter="v",
        radix=21,
        key=dynamic_key,
        iv=dynamic_iv,
    )
    rendered = (
        "<video data-src='https://gifb.example/recommended-preview.mp4'></video>"
        "<script>hadeedg252=14;hcdeedg252=21;"
        "argdeqweqweqwe='827b6028b49ec12d';"
        "hdddedg252='3818175f7ec89f6f';</script>"
        "<script>mvarr['10_1']=[["
        "'a_iframe_id_1090_602_7716',"
        f"'{encrypted}',"
        "'<iframe id=\\\"a_iframe_id_1090_602_7716\\\" ',"
        "'//18av.mm-cg.com/js/player/play.php?numresolution=1080&amp;id=',"
        "'',"
        "'></iframe><br>']]></script>"
    )
    pages = {
        "https://18av.mm-cg.com/zh/?page=1": (
            '<div class="post video_9s"><figure><img class="thumb" '
            'src="https://fbhost1.imgstream3.com/b/ei/HH05966.jpg"></figure>'
            '<div class="con"><h3><a href="/zh/chinese_content/22817/content.html">18AV Example</a></h3>'
            '<div class="meta">2016/05/10</div></div></div>'
        ),
        detail_url: (
            '<div class="archive-title"><h1><b>18AV Example</b></h1></div>'
            '<meta name="description" content="18AV summary">'
            '<script type="application/ld+json">'
            '{"thumbnailUrl":"https://fbhost1.imgstream3.com/b/ei/HH05966.jpg",'
            '"uploadDate":"2016-05-10"}</script>' + rendered
        ),
        player_url: (
            "<script>const videoSources = ["
            f"{{src: '{stream_url}', type: 'application/x-mpegURL'}}"
            "]; const recommended = 'https://cdn.example/recommended-preview.mp4';</script>"
        ),
    }
    http = _Http(pages)
    works = Discovery(http, Parser(), _Checker()).list_works(source, "/zh/")
    assert works[0].title == "18AV Example"
    content = Content(http, Parser(), _Checker())
    detail = content.fetch_detail(source, detail_url)
    assert detail.cover.endswith("HH05966.jpg")
    with patch(
        "framework.playwright_helper.fetch_rendered_html_sync",
        return_value=rendered,
    ) as render:
        assert content.fetch_video_episode(source, detail.chapters[0].url) == stream_url
    options = render.call_args.kwargs
    assert options["wait_until"] == "commit"
    assert "mvarr" in options["wait_for_function"]
    assert options["extra_delay_ms"] == 50


@pytest.mark.skipif(not Parser()._lxml, reason="lxml is required for selector regression")
def test_18av_direct_rendered_player_iframe_fallback():
    source = _disable_selfcheck(_source("18av"))
    detail_url = "https://18av.mm-cg.com/zh/chinese_content/22817/content.html"
    player_url = (
        "https://18av.mm-cg.com/js/player/play.php?numresolution=1080&id=direct-player"
    )
    stream_url = "https://cdn.example/18av-direct.m3u8"
    rendered = f'<iframe src="{player_url}"></iframe>'
    pages = {
        detail_url: (
            '<div class="archive-title"><h1><b>18AV Example</b></h1></div>'
            '<script type="application/ld+json">'
            '{"thumbnailUrl":"https://img.example/cover.jpg"}</script>'
        ),
        player_url: f"<script>const videoSources = [{{src: '{stream_url}'}}];</script>",
    }
    content = Content(_Http(pages), Parser(), _Checker())
    content.fetch_detail(source, detail_url)
    with patch("framework.playwright_helper.fetch_rendered_html_sync", return_value=rendered):
        assert content.fetch_video_episode(source, detail_url) == stream_url


def test_18av_protected_player_rule_is_configured():
    source = _source("18av")
    episode = source.raw["endpoints"]["content"]["episode"]
    assert episode["protected_player"] is True
    assert episode["play_url"]["protected_player"] is True
    assert source.raw["media"]["format"] == "hls"


def test_18av_disables_generic_ad_filtering_and_keeps_raw_hls():
    source = _source("18av")
    assert source.raw["ad_block"] == {"enabled": False}

    from framework.media_proxy import MediaProxy

    text = (
        "#EXTM3U\n"
        "#EXT-X-CUE-OUT\n"
        "#EXTINF:5.0,\n/seg/ad1.ts\n"
        "#EXT-X-CUE-IN\n"
        "#EXTINF:10.0,\n/seg/001.ts\n"
        "#EXT-X-ENDLIST\n"
    )
    out = MediaProxy.instance()._filter_ad_segments(
        text, "https://cdn.example/hls/i.m3u8",
        source.raw["ad_block"], source_id="18av",
    )
    assert out == text


def test_18av_search_and_category_details_are_configured():
    source = _source("18av")
    assert source.get_discovery_config()["works_list_item"]["page_item_limit"] == 30
    categories = source.get_discovery_config()["list_item"]["categories"]
    assert any(item["url"] == "/zh/censored_list/all/{page}.html" for item in categories)
    assert source.get_detail_config()["url_pattern"] == "/zh/[^/]+_content/"

    search = source.get_search_config()
    assert search["base_url"] == "/zh/fc_search/all/{keyword}/1.html"
    assert search["paginator"]["url_template"].endswith("/{page}.html")
    assert search["item"]["fields"]["url"]["attr"] == "href"


