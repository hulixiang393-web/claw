# -*- coding: utf-8 -*-
"""外部播放器接入（video_view → framework.desktop_player）。

现场：内嵌 VLC 播长 TS「播一会儿就卡」，用户改用外部播放器。本文件只验
**接线契约**：配置优先级、代理 URL 透传、续播位置、拉起失败回落内嵌、
代理租约不成对泄漏。不真正拉起进程。
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from framework import desktop_player as dp  # noqa: E402
from framework.content import Chapter, Detail  # noqa: E402


def _make_view(raw=None, source_id="ikanpp"):
    from gui.pages.reader.video_view import VideoView

    view = VideoView(None)
    view._detail = Detail(source_id=source_id, content_type="video",
                          url="http://e/1", title="w", cover="",
                          chapters=[Chapter("第1集", "http://e/0")])
    view._episodes = view._detail.chapters
    view._source = SimpleNamespace(source_id=source_id, content_type="video",
                                   raw=raw or {})
    view._current_idx = 0
    return view


@pytest.fixture
def build(_qapp):
    def _make(raw=None, source_id="ikanpp"):
        return _make_view(raw, source_id)
    return _make


@pytest.fixture
def prefs_on(monkeypatch):
    monkeypatch.setattr(dp, "load_prefs", lambda *a, **k: dp.Prefs(
        player="mpv", player_path="", extra_args=(), prefer_external=True))


@pytest.fixture
def prefs_off(monkeypatch):
    monkeypatch.setattr(dp, "load_prefs", lambda *a, **k: dp.Prefs())


class _Popen:
    pid = 777

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class _Launched:
    def __init__(self, key="mpv"):
        self.key = key
        self.name = dp.player_display_name(key)
        self.exe = "C:/mpv/mpv.exe"
        self.argv = []
        self.process = _Popen()

    @property
    def pid(self):
        return 777


def _stub_lease(view, monkeypatch):
    """替身代理：记录租约申请/释放，避免真起 HTTP 服务。"""
    state = {"acquired": 0, "released": []}

    class _Proxy:
        def acquire_lease(self):
            state["acquired"] += 1
            return f"lease-{state['acquired']}"

        def release_lease(self, lease_id):
            state["released"].append(lease_id)

    import framework.media_proxy as mp

    monkeypatch.setattr(mp.MediaProxy, "instance",
                        classmethod(lambda cls: _Proxy()))
    # 租约释放线程会 wait 进程后立刻释放；这里让 wait 直接返回
    monkeypatch.setattr(view, "_notify_last_episode", lambda: None)
    return state


# --------------------------------------------------------------------------- #
# 配置驱动的起播路径
# --------------------------------------------------------------------------- #
def test_play_uses_external_player_when_configured(build, monkeypatch, prefs_on):
    view = build()
    called = []
    monkeypatch.setattr(view, "_open_in_desktop_player",
                        lambda: called.append(1) or True)
    played = []
    monkeypatch.setattr(view, "_ensure_player",
                        lambda hdrs=None: played.append(1))
    monkeypatch.setattr(view, "_embedded_play_url", lambda v, a, h, ad: (v, a))

    view._play("http://cdn/x.m3u8", "", "第1集")

    assert called == [1]
    assert played == []          # 内嵌播放器不得被启动（省掉一套解码器）


def test_play_falls_back_to_embedded_when_external_fails(build, monkeypatch,
                                                         prefs_on):
    view = build()
    monkeypatch.setattr(view, "_open_in_desktop_player", lambda: False)
    played = []

    class _Player:
        def play(self, url, audio="", title="", kind="", network_caching_ms=0):
            played.append(url)

    monkeypatch.setattr(view, "_ensure_player", lambda hdrs=None: _Player())
    monkeypatch.setattr(view, "_embedded_play_url", lambda v, a, h, ad: (v, a))
    monkeypatch.setattr(view, "_configure_playback_prefetch", lambda url="": None)
    monkeypatch.setattr(view, "_start_prefetch_ticker", lambda: None)
    monkeypatch.setattr(view, "_start_progress_ticker", lambda: None)

    view._play("http://cdn/x.m3u8", "", "第1集")

    assert played == ["http://cdn/x.m3u8"]   # 外部失败 → 回落内嵌，不留黑屏


def test_play_keeps_embedded_when_not_configured(build, monkeypatch, prefs_off):
    view = build()
    called = []
    monkeypatch.setattr(view, "_open_in_desktop_player",
                        lambda: called.append(1) or True)
    played = []

    class _Player:
        def play(self, url, audio="", title="", kind="", network_caching_ms=0):
            played.append(url)

    monkeypatch.setattr(view, "_ensure_player", lambda hdrs=None: _Player())
    monkeypatch.setattr(view, "_embedded_play_url", lambda v, a, h, ad: (v, a))
    monkeypatch.setattr(view, "_configure_playback_prefetch", lambda url="": None)
    monkeypatch.setattr(view, "_start_prefetch_ticker", lambda: None)
    monkeypatch.setattr(view, "_start_progress_ticker", lambda: None)

    view._play("http://cdn/x.m3u8", "", "第1集")

    assert called == []
    assert played == ["http://cdn/x.m3u8"]


# --------------------------------------------------------------------------- #
# 拉起细节
# --------------------------------------------------------------------------- #
def test_open_in_desktop_player_passes_proxy_url_and_audio(build, monkeypatch,
                                                           prefs_on):
    view = build()
    view._current_play = "http://cdn/x.m3u8"
    view._current_audio = "http://cdn/x.mp4"
    view._current_title = "第1集"
    state = _stub_lease(view, monkeypatch)
    monkeypatch.setattr(view, "_embedded_play_url",
                        lambda v, a, h, ad: ("http://127.0.0.1:9/s/t/v.m3u8",
                                             "http://127.0.0.1:9/s/t/a.m4a"))
    seen = {}
    monkeypatch.setattr(dp, "launch",
                        lambda url, **kw: seen.update(kw, url=url) or _Launched())

    assert view._open_in_desktop_player() is True
    assert seen["url"] == "http://127.0.0.1:9/s/t/v.m3u8"
    assert seen["audio_url"] == "http://127.0.0.1:9/s/t/a.m4a"
    assert seen["title"] == "第1集"
    assert state["acquired"] == 1


def test_open_in_desktop_player_resumes_from_embedded_position(build, monkeypatch,
                                                               prefs_on):
    view = build()
    view._current_play = "http://cdn/x.m3u8"

    class _Embedded:
        def get_time(self):
            return 125_000

    view._player = _Embedded()
    view._has_played = True
    _stub_lease(view, monkeypatch)
    monkeypatch.setattr(view, "_embedded_play_url", lambda v, a, h, ad: (v, a))
    seen = {}
    monkeypatch.setattr(dp, "launch",
                        lambda url, **kw: seen.update(kw) or _Launched())

    view._open_in_desktop_player()

    assert seen["start_s"] == 125


def test_open_in_desktop_player_uses_pending_seek_when_idle(build, monkeypatch,
                                                             prefs_on):
    view = build()
    view._current_play = "http://cdn/x.m3u8"
    view._pending_seek_s = 42.0
    _stub_lease(view, monkeypatch)
    monkeypatch.setattr(view, "_embedded_play_url", lambda v, a, h, ad: (v, a))
    seen = {}
    monkeypatch.setattr(dp, "launch",
                        lambda url, **kw: seen.update(kw) or _Launched())

    view._open_in_desktop_player()

    assert seen["start_s"] == 42


def test_open_in_desktop_player_reports_status_when_launch_fails(build, monkeypatch,
                                                                 prefs_on):
    view = build()
    view._current_play = "http://cdn/x.m3u8"
    status = []
    monkeypatch.setattr(view, "_show_status", status.append)
    monkeypatch.setattr(view, "_embedded_play_url", lambda v, a, h, ad: (v, a))

    def boom(url, **kw):
        raise dp.PlayerNotFound("没有找到可用的外部播放器")

    monkeypatch.setattr(dp, "launch", boom)

    assert view._open_in_desktop_player() is False
    assert status and "外部播放器" in status[0]


def test_open_in_desktop_player_requires_a_stream(build, monkeypatch):
    view = build()
    view._current_play = ""
    status = []
    monkeypatch.setattr(view, "_show_status", status.append)
    assert view._open_in_desktop_player() is False
    assert status and "集" in status[0]


def test_launch_failure_releases_the_proxy_lease(build, monkeypatch, prefs_on):
    view = build()
    view._current_play = "http://cdn/x.m3u8"
    state = _stub_lease(view, monkeypatch)
    monkeypatch.setattr(view, "_embedded_play_url", lambda v, a, h, ad: (v, a))
    monkeypatch.setattr(view, "_show_status", lambda *_: None)

    def boom(url, **kw):
        raise OSError("exe 不见了")

    monkeypatch.setattr(dp, "launch", boom)

    view._open_in_desktop_player()
    assert state["released"] == ["lease-1"]   # 失败必须成对释放


def test_player_menu_lists_detected_players(build, monkeypatch):
    view = build()
    monkeypatch.setattr(dp, "detect", lambda: {"mpv": "C:/mpv/mpv.exe"})
    monkeypatch.setattr(dp, "load_prefs", lambda *a, **k: dp.Prefs())
    monkeypatch.setattr(dp, "resolve", lambda *a, **k: ("mpv", "C:/mpv/mpv.exe"))
    view._refresh_player_menu()
    labels = [a.text() for a in view.player_menu.actions()]
    assert any("mpv" in t for t in labels)
    assert any("起播时优先外部播放" in t for t in labels)


def test_selecting_player_persists_to_config(build, monkeypatch):
    view = build()
    saved = {}
    monkeypatch.setattr(view, "_save_video_config",
                        lambda **kw: saved.update(kw))
    monkeypatch.setattr(dp, "detect", lambda: {"vlc": "C:/vlc/vlc.exe"})
    monkeypatch.setattr(dp, "load_prefs", lambda *a, **k: dp.Prefs())
    monkeypatch.setattr(dp, "resolve", lambda *a, **k: ("vlc", "C:/vlc/vlc.exe"))
    monkeypatch.setattr(view, "_show_status", lambda *_: None)

    view._set_external_player("vlc")

    assert saved == {"player": "vlc", "player_path": ""}
    view._set_prefer_external(True)
    assert saved["prefer_external"] is True
