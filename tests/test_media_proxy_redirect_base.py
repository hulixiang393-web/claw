# -*- coding: utf-8 -*-
"""媒体代理 m3u8 重写基准：用上游最终响应 URL（resp.url）而非请求 URL。

包装接口（如 /media/m3u8?url=…）302 到 CDN 时，清单内的相对分片/KEY
必须基于最终 host 解析，否则 urljoin 会拼到包装接口的假路径 → 404/403。
R3（可选）：Content-Encoding: zstd 时用 zstandard（可选依赖）解压，缺失原样返回。
"""
import io
import re
import sys
from urllib.parse import quote

import pytest
import framework.media_proxy as mp
from framework.media_cache import MediaCache
from framework.media_proxy import MediaProxy

_M3U8 = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:10\n"
    '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
    "#EXTINF:10.0,\n"
    "seg/001.ts\n"
    "#EXTINF:10.0,\n"
    "seg/002.ts\n"
    "#EXT-X-ENDLIST\n"
)

_WRAPPER = "https://wrapper.example/media/m3u8?url=bogus"
_FINAL = "https://cdn.example.net/hls/index.m3u8"
_AD_OFF = {"enabled": False}


class _Handler:
    headers = {}
    close_connection = False

    def send_error(self, code, message=None, explain=None):
        raise AssertionError(f"unexpected send_error {code}: {message}")


class _RedirectM3U8:
    """302 后落在另一 host 的 m3u8 响应（url = 最终 URL）。"""

    status_code = 200
    headers = {
        "Content-Type": "application/vnd.apple.mpegurl",
        "Content-Length": str(len(_M3U8.encode())),
    }
    url = _FINAL

    def __init__(self):
        self.raw = io.BytesIO(_M3U8.encode())

    def close(self):
        pass


class _PlainM3U8(_RedirectM3U8):
    """无重定向常规路径：resp.url == 请求 URL。"""

    url = _WRAPPER


def _capture(proxy, monkeypatch):
    sent = []
    monkeypatch.setattr(proxy, "_emit_m3u8", lambda h, text: sent.append(text))
    monkeypatch.setattr(proxy, "_base_url",
                        lambda public_base=None: "http://127.0.0.1:9")
    return sent


def _token_targets(proxy, text):
    tokens = set(re.findall(r"/s/([0-9a-f]{32})", text))
    assert tokens
    return [proxy._tokens[t][0] for t in tokens]


def test_forward_m3u8_rewrites_relative_uris_against_final_url(monkeypatch):
    """302 到另一 host 的清单：相对分片/KEY 基于最终 host，而非包装接口。"""
    proxy = MediaProxy(cache=type("Cache", (), {"enabled": False})())
    monkeypatch.setattr(mp, "_fetch_upstream", lambda *a, **k: _RedirectM3U8())
    sent = _capture(proxy, monkeypatch)
    try:
        proxy._forward_m3u8(_Handler(), _WRAPPER, {}, _AD_OFF)
        for upstream in _token_targets(proxy, sent[0]):
            assert upstream.startswith("https://cdn.example.net/hls/")
            assert "wrapper.example" not in upstream
    finally:
        proxy.stop()


def test_forward_m3u8_no_redirect_keeps_request_url_base(monkeypatch):
    """无重定向常规路径不回归：base 仍为请求 URL。"""
    proxy = MediaProxy(cache=type("Cache", (), {"enabled": False})())
    monkeypatch.setattr(mp, "_fetch_upstream", lambda *a, **k: _PlainM3U8())
    sent = _capture(proxy, monkeypatch)
    try:
        proxy._forward_m3u8(_Handler(), _WRAPPER, {}, _AD_OFF)
        for upstream in _token_targets(proxy, sent[0]):
            assert upstream.startswith("https://wrapper.example/media/")
    finally:
        proxy.stop()


def test_forward_m3u8_cache_ctx_and_c_uri_use_final_base(tmp_path, monkeypatch):
    """缓存开启：/c/ name 基于最终 host；cache_ctx 同时保存请求 URL 与 final base。"""
    cache = MediaCache(root=tmp_path / "cache")
    proxy = MediaProxy(cache=cache)
    monkeypatch.setattr(mp, "_fetch_upstream", lambda *a, **k: _RedirectM3U8())
    sent = _capture(proxy, monkeypatch)
    try:
        proxy._forward_m3u8(_Handler(), _WRAPPER, {}, _AD_OFF)
        text = sent[0]
        key = cache.key_of(_WRAPPER)
        assert f"/c/{key}/" in text
        assert f"/c/{key}/https://cdn.example.net/hls/seg/001.ts" in text
        assert "wrapper.example" not in text
        ctx = proxy._cache_ctx[key]
        assert ctx[0] == _WRAPPER          # 请求 URL：disk 缓存键隔离语义不变
        assert ctx[4] == _FINAL            # final base：重写基准
    finally:
        proxy.stop()


def test_serve_cache_nested_playlist_rebases_on_its_own_final_url(
        tmp_path, monkeypatch):
    """/c/ 嵌套播放列表自身 302 → 其相对分片基于自身最终 URL 解析。"""
    cache = MediaCache(root=tmp_path / "cache")
    proxy = MediaProxy(cache=cache)
    top_key = cache.key_of(_WRAPPER)
    proxy._register_cache_ctx(top_key, _WRAPPER, {"Referer": "r"}, _AD_OFF,
                              final_base=_FINAL)
    from urllib.parse import quote

    nested = "https://nested.example/hls/media.m3u8"
    path = f"/c/{top_key}/{quote(nested, safe='/:@')}"

    class _RespNested:
        status_code = 200
        headers = {"Content-Type": "application/vnd.apple.mpegurl"}
        url = "https://edge.example.net/hls/media.m3u8"

        def __init__(self):
            self.raw = io.BytesIO(_M3U8.encode())

        def close(self):
            pass

    monkeypatch.setattr(mp, "_fetch_upstream", lambda *a, **k: _RespNested())
    sent = _capture(proxy, monkeypatch)
    handler = _Handler()
    handler.headers = {"Range": "bytes=0-999999"}
    try:
        proxy._serve_cache(handler, path)
        for upstream in _token_targets(proxy, sent[0]):
            assert upstream.startswith("https://edge.example.net/hls/")
            assert "nested.example" not in upstream
    finally:
        proxy.stop()


def test_serve_cache_playlist_hit_uses_saved_final_base(tmp_path, monkeypatch):
    """缓存命中时，嵌套清单仍以 cache_ctx 保存的最终 host 重写。"""
    cache = MediaCache(root=tmp_path / "cache")
    proxy = MediaProxy(cache=cache)
    nested = "https://nested.example/hls/media.m3u8"
    final = "https://edge.example.net/hls/media.m3u8"
    key = cache.key_of(nested)
    proxy._register_cache_ctx(
        key, nested, {"Referer": "r"}, _AD_OFF, final_base=final
    )
    cache.playlist_write(key, _M3U8.encode())
    path = f"/c/{key}/{quote(nested, safe='/:@')}"
    sent = _capture(proxy, monkeypatch)
    try:
        proxy._serve_cache(_Handler(), path)
        assert sent
        for upstream in _token_targets(proxy, sent[0]):
            assert upstream.startswith("https://edge.example.net/hls/")
            assert "nested.example" not in upstream
    finally:
        proxy.stop()



def test_read_upstream_body_whole_reads_zstd(monkeypatch):
    """Content-Encoding: zstd → 整读并解压，可被当作 m3u8 文本重写。"""
    zstandard = pytest.importorskip("zstandard")
    body = _M3U8.encode()
    compressed = zstandard.ZstdCompressor().compress(body)

    class _Resp:
        status_code = 200
        headers = {
            "Content-Encoding": "zstd",
            "Content-Type": "application/vnd.apple.mpegurl",
        }

        def __init__(self):
            self.raw = io.BytesIO(compressed)

        def close(self):
            pass

    resp, first, whole = mp._read_media_prefix(
        _Resp(), "https://x/hls/i.m3u8", {}, False)
    assert whole is True
    assert first == body


def test_decode_zstd_falls_back_to_raw_without_library(monkeypatch):
    """zstandard 缺失时原样返回：可选依赖，不阻断播放路径。"""
    monkeypatch.setitem(sys.modules, "zstandard", None)
    assert mp._decode_zstd(b"not really zstd") == b"not really zstd"