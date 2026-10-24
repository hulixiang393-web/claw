# -*- coding: utf-8 -*-
"""播放代理 m3u8 广告过滤：无配置也用内置规则；显式关闭仍生效。"""
from framework.media_proxy import MediaProxy


_AD_M3U8 = (
    "#EXTM3U\n"
    "#EXT-X-CUE-OUT\n"
    "#EXTINF:5.0,\n/seg/ad1.ts\n"
    "#EXT-X-CUE-IN\n"
    "#EXTINF:10.0,\n/seg/001.ts\n"
    "#EXT-X-ENDLIST\n"
)


def test_filter_ad_segments_no_config_builtin():
    mp = MediaProxy.instance()
    out = mp._filter_ad_segments(_AD_M3U8, "https://cdn.example.com/hls/i.m3u8", None)
    assert "ad1.ts" not in out
    assert "001.ts" in out


def test_filter_ad_segments_respects_disabled():
    mp = MediaProxy.instance()
    out = mp._filter_ad_segments(
        _AD_M3U8, "https://cdn.example.com/hls/i.m3u8", {"enabled": False}
    )
    assert out == _AD_M3U8


def test_serve_m3u8_text_filters_without_source_config(monkeypatch):
    mp = MediaProxy.instance()
    sent = []
    monkeypatch.setattr(mp, "_rewrite_m3u8", lambda text, *args, **kwargs: text)
    monkeypatch.setattr(mp, "_emit_m3u8", lambda handler, text: sent.append(text))

    mp._serve_m3u8_text(object(), _AD_M3U8, "https://cdn.example.com/hls/i.m3u8", {}, None)

    assert "ad1.ts" not in sent[0]
    assert "001.ts" in sent[0]


def test_serve_m3u8_text_18av_keeps_raw_stream(monkeypatch):
    mp = MediaProxy.instance()
    sent = []
    monkeypatch.setattr(mp, "_rewrite_m3u8", lambda text, *args, **kwargs: text)
    monkeypatch.setattr(mp, "_emit_m3u8", lambda handler, text: sent.append(text))

    mp._serve_m3u8_text(
        object(), _AD_M3U8, "https://cdn.example.com/hls/i.m3u8", {},
        {"enabled": False}, source_id="18av",
    )

    assert sent == [_AD_M3U8]


def test_cache_context_preserves_real_target_and_sensitive_headers_only_in_memory():
    proxy = MediaProxy(cache=type("Cache", (), {"enabled": False})())
    target = "https://cdn.example/hls/index.m3u8?token=signed&expires=9"
    proxy._register_cache_ctx(
        "k", target,
        {
            "Referer": "https://page.example/watch?token=ref",
            "User-Agent": "player",
            "Cookie": "sid=cookie-secret",
            "Authorization": "Bearer auth-secret",
        },
        None,
    )

    context = proxy._cache_ctx["k"]

    assert context[0] == target
    assert context[1]["Referer"] == "https://page.example/watch?token=ref"
    assert context[1]["Cookie"] == "sid=cookie-secret"
    assert context[1]["Authorization"] == "Bearer auth-secret"
    assert context[1]["User-Agent"] == "player"
    proxy.stop()


def test_signed_manifest_is_not_written_to_disk_cache(monkeypatch):
    writes = []

    class Cache:
        enabled = True

        @staticmethod
        def key_of(url):
            return "k"

        def playlist_write(self, key, data):
            writes.append((key, data))
            return True

        def mark_hls(self, key, size):
            pass

    proxy = MediaProxy(cache=Cache())
    sent = []
    monkeypatch.setattr(proxy, "_rewrite_m3u8", lambda text, *args, **kwargs: text)
    monkeypatch.setattr(proxy, "_emit_m3u8", lambda handler, text: sent.append(text))

    proxy._serve_m3u8_text(
        object(),
        '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.key?token=secret"\nseg.ts\n',
        "https://cdn.example/hls/index.m3u8?token=manifest",
        {},
        None,
    )

    assert sent
    assert "key.key?token=secret" in sent[0]
    assert writes == []
    assert "k" in proxy._cache_ctx
    proxy.stop()


def test_proxy_url_for_hls_without_headers_uses_media_proxy(monkeypatch):
    calls = []

    class Proxy:
        def build_url(self, *args):
            calls.append(args)
            return "http://127.0.0.1:1234/s/token"

    monkeypatch.setattr("framework.media_proxy.MediaProxy.instance", lambda: Proxy())

    from framework.media_proxy import proxy_url_for

    result = proxy_url_for("https://cdn.example.com/hls/index.m3u8", {})

    assert result.startswith("http://127.0.0.1:")
    assert calls == [("https://cdn.example.com/hls/index.m3u8", {}, None, False)]
