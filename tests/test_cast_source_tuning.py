# -*- coding: utf-8 -*-
import json
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


def test_prefetch_configuration_is_explicit_and_bounded():
    proxy = MediaProxy(cache=None, prefetch={"enabled": False})
    proxy.configure_prefetch(True, depth=10**9, workers=10**9)
    assert proxy._pf_cfg == {"enabled": True, "depth": 64, "workers": 8}
    proxy.configure_prefetch(False, depth=0, workers=0)
    assert proxy._pf_cfg == {"enabled": False, "depth": 1, "workers": 1}
    proxy.stop()
