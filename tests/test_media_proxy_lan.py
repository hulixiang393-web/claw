# -*- coding: utf-8 -*-
import re
from urllib.parse import urlsplit

import pytest

from framework.media_proxy import MediaProxy


class _OffCache:
    enabled = False


@pytest.fixture
def proxy_ctx():
    proxy = MediaProxy(cache=_OffCache())
    yield proxy
    proxy.stop_lan_endpoint()
    proxy.stop()
    proxy._leases.clear()


def _token(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def test_build_url_defaults_to_localhost(proxy_ctx):
    url = proxy_ctx.build_url("https://cdn.example/video.m3u8", {"Referer": "r"})
    parts = urlsplit(url)
    assert parts.scheme == "http"
    assert parts.hostname == "127.0.0.1"
    assert parts.port
    assert parts.path.startswith("/s/")


def test_start_lan_endpoint_binds_requested_host_and_reuses(proxy_ctx):
    first = proxy_ctx.start_lan_endpoint("127.0.0.1")
    second = proxy_ctx.start_lan_endpoint("127.0.0.1")
    parts = urlsplit(first)
    assert parts.hostname == "127.0.0.1"
    assert parts.port
    assert proxy_ctx._lan_server.server_address[0] == "127.0.0.1"
    assert second == first


def test_lan_build_url_uses_lan_base_and_owns_token(proxy_ctx):
    lan_base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    url = proxy_ctx.build_url(
        "https://cdn.example/video.m3u8",
        {"Referer": "r"},
        public_base=lan_base,
    )
    assert url.startswith(lan_base + "/s/")
    assert _token(url) in proxy_ctx._lan_tokens


def test_stop_lan_endpoint_preserves_local_tokens(proxy_ctx):
    local_url = proxy_ctx.build_url("https://cdn.example/local.m3u8", {"Referer": "r"})
    lan_base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    lan_url = proxy_ctx.build_url(
        "https://cdn.example/lan.m3u8",
        {"Referer": "r"},
        public_base=lan_base,
    )
    local_token = _token(local_url)
    lan_token = _token(lan_url)
    proxy_ctx.stop_lan_endpoint()
    proxy_ctx.stop_lan_endpoint()
    assert local_token in proxy_ctx._tokens
    assert lan_token not in proxy_ctx._tokens
    assert proxy_ctx._lan_tokens == set()
    assert proxy_ctx._server is not None
    assert proxy_ctx._lan_server is None


def test_release_lan_tokens_preserves_local_tokens(proxy_ctx):
    local_url = proxy_ctx.build_url("https://cdn.example/local.m3u8", {"Referer": "r"})
    lan_base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    lan_url = proxy_ctx.build_url(
        "https://cdn.example/lan.m3u8",
        {"Referer": "r"},
        public_base=lan_base,
    )
    proxy_ctx.release_lan_tokens()
    assert _token(local_url) in proxy_ctx._tokens
    assert _token(lan_url) not in proxy_ctx._tokens
    assert proxy_ctx._lan_tokens == set()


def test_lan_m3u8_rewrite_uses_lan_base_for_segments_keys_and_nested_manifests(proxy_ctx):
    lan_base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    local_base = proxy_ctx.build_url("https://cdn.example/local.m3u8", {"Referer": "r"})
    rewritten = proxy_ctx._rewrite_m3u8(
        '\n'.join([
            "#EXTM3U",
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"',
            "segment-1.ts",
            "variant.m3u8",
        ]),
        "https://cdn.example/hls/index.m3u8",
        {"Referer": "r"},
        public_base=lan_base,
    )
    urls = re.findall(r'http://[^"\n]+', rewritten)
    assert len(urls) == 3
    assert all(url.startswith(lan_base + "/s/") for url in urls)
    assert local_base not in rewritten
    assert all(_token(url) in proxy_ctx._lan_tokens for url in urls)


def test_stop_lan_endpoint_is_idempotent_and_local_endpoint_survives(proxy_ctx):
    lan_base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    proxy_ctx.stop_lan_endpoint()
    proxy_ctx.stop_lan_endpoint()
    local_url = proxy_ctx.build_url("https://cdn.example/local.m3u8", {"Referer": "r"})
    assert local_url.startswith("http://127.0.0.1:")
    assert lan_base != local_url.rsplit("/s/", 1)[0]
