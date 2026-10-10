# -*- coding: utf-8 -*-
"""外部播放器：HLS 一律走本地代理（无防盗链头也走），非 HLS 无头仍直连。"""
import framework.external_player as ep


def _play(monkeypatch, url, headers):
    calls = []

    def _proxy(u, headers=None, ad_block=None, force_proxy=False, source_id=None):
        calls.append(u)
        return "PROXY:" + u

    monkeypatch.setattr(ep, "_locate_vlc", lambda: r"C:\Program Files\VideoLAN\VLC\vlc.exe")
    monkeypatch.setattr(ep, "proxy_url_for", _proxy)
    monkeypatch.setattr(ep.subprocess, "Popen", lambda *a, **k: None)
    return ep.open_with_player(
        url, audio="", referer="", user_agent="", headers=headers, ad_block=None
    ), calls


def test_hls_goes_through_proxy_without_headers(monkeypatch):
    _, calls = _play(monkeypatch, "https://cdn.example.com/hls/index.m3u8", {})
    assert "https://cdn.example.com/hls/index.m3u8" in calls


def test_mp4_without_headers_direct(monkeypatch):
    _, calls = _play(monkeypatch, "https://cdn.example.com/movie.mp4", {})
    assert calls == []


def test_hls_parameter_wrapped_url_is_proxied(monkeypatch):
    _, calls = _play(monkeypatch, "https://site/player?src=https%3A%2F%2Fcdn%2Findex.m3u8", {})
    assert calls == ["https://site/player?src=https%3A%2F%2Fcdn%2Findex.m3u8"]


def test_hls_without_vlc_uses_proxy_url(monkeypatch):
    calls = []
    monkeypatch.setattr(ep, "_locate_vlc", lambda: None)
    def proxy(u, headers=None, ad_block=None, force_proxy=False, source_id=None):
        calls.append(u)
        return "http://127.0.0.1/s/token"

    monkeypatch.setattr(ep, "proxy_url_for", proxy)
    monkeypatch.setattr(ep.webbrowser, "open", lambda u: calls.append(u))
    ep.open_with_player("https://cdn.example.com/index.m3u8", headers={})
    assert calls == ["https://cdn.example.com/index.m3u8", "http://127.0.0.1/s/token"]


def test_raw_source_id_does_not_enable_trusted_media_rules(monkeypatch):
    calls = []

    def proxy(u, headers=None, ad_block=None, force_proxy=False, source_id=None):
        calls.append({"ad_block": ad_block, "force_proxy": force_proxy, "source_id": source_id})
        return "PROXY:" + u

    monkeypatch.setattr(ep, "_locate_vlc", lambda: r"C:\\Program Files\\VideoLAN\\VLC\\vlc.exe")
    monkeypatch.setattr(ep, "proxy_url_for", proxy)
    monkeypatch.setattr(ep.subprocess, "Popen", lambda args, **kwargs: None)
    ep.open_with_player("https://cdn.example.com/index.m3u8", source_id="source-a")
    assert calls == [{"ad_block": None, "force_proxy": False, "source_id": None}]


def test_hls_audio_slave_proxied(monkeypatch):
    calls = {}

    def _proxy(u, headers=None, ad_block=None, force_proxy=False, source_id=None):
        calls[u] = True
        return "PROXY:" + u

    monkeypatch.setattr(ep, "_locate_vlc", lambda: r"C:\Program Files\VideoLAN\VLC\vlc.exe")
    monkeypatch.setattr(ep, "proxy_url_for", _proxy)
    monkeypatch.setattr(ep.subprocess, "Popen", lambda *a, **k: None)
    ep.open_with_player(
        "https://cdn.example.com/hls/index.m3u8",
        audio="https://cdn.example.com/hls/a.m3u8",
        referer="", user_agent="", headers={}, ad_block=None,
    )
    assert "https://cdn.example.com/hls/index.m3u8" in calls
    assert "https://cdn.example.com/hls/a.m3u8" in calls
