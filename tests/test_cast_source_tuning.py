# -*- coding: utf-8 -*-
import json
import threading
from pathlib import Path

from framework.media_proxy import MediaProxy


ROOT = Path(__file__).resolve().parent.parent


def test_bilibili_declares_cast_buffer_and_conservative_prefetch():
    raw = json.loads((ROOT / "sources" / "bilibili.json").read_text(encoding="utf-8"))
    cast = (raw.get("media") or {}).get("cast") or {}
    assert cast["network_caching_ms"] >= 20000
    assert cast["prefetch"]["enabled"] is False
    assert cast["prefetch"]["depth"] <= 16
    assert cast["prefetch"]["workers"] <= 4


def test_cast_tuning_is_forwarded_without_changing_local_player_defaults():
    from gui.pages.reader.video_view import VideoView

    class Source:
        raw = {"media": {"cast": {
            "network_caching_ms": 30000,
            "prefetch": {"enabled": True, "depth": 99, "workers": 99},
        }}}

    view = VideoView.__new__(VideoView)
    view._source = Source()
    assert view._source_cast_tuning() == {
        "network_caching_ms": 30000,
        "prefetch": {"enabled": True, "depth": 99, "workers": 99},
    }

    assert view._source_network_caching_ms() == 0


def test_cast_session_path_configures_current_source_prefetch(monkeypatch):
    from gui.pages.reader.video_view import VideoView

    class Source:
        raw = {"media": {"cast": {
            "prefetch": {"enabled": True, "depth": 12, "workers": 3},
        }}}

    calls = []
    class Proxy:
        def configure_prefetch(self, enabled, depth, workers):
            calls.append((enabled, depth, workers))

    view = VideoView.__new__(VideoView)
    view._source = Source()
    monkeypatch.setattr(
        "framework.media_proxy.MediaProxy.instance",
        staticmethod(lambda: Proxy()),
    )
    view._configure_cast_tuning()
    assert calls == [(True, 12, 3)]


def test_local_playback_prefetch_uses_hls_config(monkeypatch):
    from gui.pages.reader.video_view import VideoView

    class Source:
        raw = {"media": {"hls": {
            "prefetch": {"enabled": True, "depth": 8, "workers": 4},
        }}}

    calls = []

    class Proxy:
        def configure_prefetch(self, enabled, depth, workers):
            calls.append((enabled, depth, workers))

    view = VideoView.__new__(VideoView)
    view._source = Source()
    monkeypatch.setattr(
        "framework.media_proxy.MediaProxy.instance",
        staticmethod(lambda: Proxy()),
    )
    view._configure_playback_prefetch("https://cdn.example/video/index.m3u8")
    assert calls == [(True, 8, 4)]


def test_local_hls_without_source_tuning_uses_bounded_prefetch(monkeypatch):
    from gui.pages.reader.video_view import VideoView

    class Source:
        raw = {"media": {"format": "hls"}}

    calls = []

    class Proxy:
        def configure_prefetch(self, enabled, depth, workers):
            calls.append((enabled, depth, workers))

    view = VideoView.__new__(VideoView)
    view._source = Source()
    monkeypatch.setattr(
        "framework.media_proxy.MediaProxy.instance",
        staticmethod(lambda: Proxy()),
    )
    view._configure_playback_prefetch("https://cdn.example/video/index.m3u8")
    assert calls == [(True, 4, 2)]


def test_local_hls_format_without_extension_uses_bounded_prefetch(monkeypatch):
    from gui.pages.reader.video_view import VideoView

    class Source:
        raw = {"media": {"format": "hls"}}

    calls = []

    class Proxy:
        def configure_prefetch(self, enabled, depth, workers):
            calls.append((enabled, depth, workers))

    view = VideoView.__new__(VideoView)
    view._source = Source()
    monkeypatch.setattr(
        "framework.media_proxy.MediaProxy.instance",
        staticmethod(lambda: Proxy()),
    )
    view._configure_playback_prefetch("https://cdn.example/video/play")
    assert calls == [(True, 4, 2)]


def test_local_hls_explicit_prefetch_disabled_is_respected(monkeypatch):
    from gui.pages.reader.video_view import VideoView

    class Source:
        raw = {"media": {"hls": {"prefetch": {"enabled": False}}}}

    calls = []

    class Proxy:
        def configure_prefetch(self, enabled, depth, workers):
            calls.append((enabled, depth, workers))

    view = VideoView.__new__(VideoView)
    view._source = Source()
    monkeypatch.setattr(
        "framework.media_proxy.MediaProxy.instance",
        staticmethod(lambda: Proxy()),
    )
    view._configure_playback_prefetch("https://cdn.example/video/index.m3u8")
    assert calls == [(False, 4, 3)]


def test_non_hls_does_not_use_hls_prefetch_config(monkeypatch):
    from gui.pages.reader.video_view import VideoView

    class Source:
        raw = {"media": {"hls": {
            "prefetch": {"enabled": True, "depth": 8, "workers": 4},
        }}}

    calls = []

    class Proxy:
        def configure_prefetch(self, enabled, depth, workers):
            calls.append((enabled, depth, workers))

    view = VideoView.__new__(VideoView)
    view._source = Source()
    monkeypatch.setattr(
        "framework.media_proxy.MediaProxy.instance",
        staticmethod(lambda: Proxy()),
    )
    view._configure_playback_prefetch("https://cdn.example/video.mp4")
    assert calls == [(False, 4, 3)]


def test_prefetch_configuration_is_explicit_and_bounded():
    proxy = MediaProxy(cache=None, prefetch={"enabled": False})
    proxy.configure_prefetch(True, depth=10**9, workers=10**9)
    assert proxy._pf_cfg == {"enabled": True, "depth": 64, "workers": 8}
    proxy._pf_disabled = True
    proxy._pf_failures = 3
    proxy.configure_prefetch(True, depth=2, workers=2)
    assert proxy._pf_disabled is False
    assert proxy._pf_failures == 0
    proxy.configure_prefetch(False, depth=0, workers=0)
    assert proxy._pf_cfg == {"enabled": False, "depth": 1, "workers": 1}
    proxy.stop()


def test_prefetch_pool_rebuilds_for_new_worker_count():
    proxy = MediaProxy(cache=None, prefetch={"enabled": True, "workers": 1})
    try:
        first = proxy._ensure_pf_pool()
        assert first._max_workers == 1
        proxy.configure_prefetch(True, depth=4, workers=2)
        second = proxy._ensure_pf_pool()
        assert second is not first
        assert second._max_workers == 2
    finally:
        proxy.stop()


def test_configure_prefetch_cancels_previous_session_state():
    proxy = MediaProxy(cache=None, prefetch={"enabled": True, "workers": 1})
    try:
        old_event = threading.Event()
        with proxy._pf_lock:
            proxy._pf_cancelled_urls["old"] = old_event
            proxy._pf_scheduled.add("old")
            proxy._seg_order["old-key"] = ["old"]
            proxy._seg_pos["old-key"] = {"old": 0}
        proxy.configure_prefetch(True, depth=4, workers=2)
        assert old_event.is_set()
        assert not proxy._pf_cancelled_urls
        assert not proxy._pf_scheduled
        assert not proxy._seg_order
        assert not proxy._seg_pos
    finally:
        proxy.stop()
