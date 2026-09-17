# -*- coding: utf-8 -*-
"""外部播放器：HLS 一律走本地代理（无防盗链头也走），非 HLS 无头仍直连。"""
import framework.external_player as ep


def _play(monkeypatch, url, headers):
    calls = []

    def _proxy(u, *a, **k):
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


def test_hls_audio_slave_proxied(monkeypatch):
    calls = {}

    def _proxy(u, *a, **k):
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
