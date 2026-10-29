# -*- coding: utf-8 -*-
"""内嵌播放器播放链路回归（2026-10-29 从外部 VLC 进程改回内嵌 libvlc）。

约束：本文件**不启动任何播放器进程**，只用桩对象验证契约。
libvlc 实例创建代价高且会抢焦点，任何真机验证都不该出现在测试里。
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from framework.content import Chapter, Detail  # noqa: E402


def _detail(n=3):
    return Detail(source_id="demo", content_type="video", url="http://e/1",
                  title="w", cover="",
                  chapters=[Chapter(f"第{i + 1}集", f"http://e/{i}") for i in range(n)])


def _view(n=3):
    from gui.pages.reader.video_view import VideoView

    view = VideoView(None)
    view._detail = _detail(n)
    view._episodes = view._detail.chapters
    view._source = SimpleNamespace(source_id="demo", content_type="video", raw={})
    view._current_idx = 0
    return view


class _FakePlayer:
    """内嵌播放器桩：记录调用，模拟 libvlc 的读接口。"""

    def __init__(self, length_ms=600000):
        self.calls = []
        self._length = length_ms
        self._time = 0
        self._playing = False

    def play(self, url, audio="", title="", kind=""):
        self.calls.append(("play", url, audio))
        self._playing = True

    def pause(self):
        self.calls.append(("pause",))
        self._playing = False

    def resume(self):
        self.calls.append(("resume",))
        self._playing = True

    def release(self):
        self.calls.append(("release",))
        self._playing = False

    def close(self):
        self.calls.append(("close",))

    def is_playing(self):
        return self._playing

    def get_length(self):
        return self._length

    def get_time(self):
        return self._time

    def set_position(self, p):
        self.calls.append(("set_position", p))

    def seek_relative(self, s):
        self.calls.append(("seek_relative", s))

    def set_volume(self, v):
        self.calls.append(("set_volume", v))

    def toggle_mute(self):
        self.calls.append(("toggle_mute",))

    def set_rate(self, r):
        self.calls.append(("set_rate", r))

    def rehook(self):
        self.calls.append(("rehook",))

    # 供测试手动触发事件
    def _emit_time(self, view, seconds):
        self._time = int(seconds * 1000)
        view._on_player_time(self._time)


def _install(view, player):
    view._player = player
    view._has_played = True
    return player


# ---------------------------------------------------------------------- #
# 播放入口
# ---------------------------------------------------------------------- #
def test_play_uses_embedded_player(_qapp, monkeypatch):
    """_play 必须交给内嵌播放器，不能再拉起外部进程。

    「外部播放器优先」读的是本机 app_config.json 的偏好（用户可开），这里显式
    关掉，否则这条契约会随本机配置漂移。
    """
    view = _view()
    monkeypatch.setattr(view, "_prefer_external_player", lambda: False)
    fake = _FakePlayer()
    monkeypatch.setattr(view, "_ensure_player", lambda hdrs=None: fake)
    monkeypatch.setattr(view, "_embedded_play_url", lambda v, a, h, ad: (v, a))
    monkeypatch.setattr(view, "_start_prefetch_ticker", lambda: None)

    view._play("http://cdn/x.m3u8", "", "第1集")

    assert fake.calls and fake.calls[0][0] == "play"
    assert fake.calls[0][1] == "http://cdn/x.m3u8"
    assert view._has_played is True


def test_play_failure_does_not_mark_played(_qapp, monkeypatch):
    """play() 抛错 → 不算播过（不得写进度、不得定位续读）。"""
    view = _view()

    class Boom(_FakePlayer):
        def play(self, *a, **kw):
            raise RuntimeError("libvlc 缺失")

    monkeypatch.setattr(view, "_ensure_player", lambda hdrs=None: Boom())
    monkeypatch.setattr(view, "_embedded_play_url", lambda v, a, h, ad: (v, a))
    monkeypatch.setattr(view, "_stop_progress_ticker", lambda: None)
    monkeypatch.setattr(view, "_stop_prefetch_ticker", lambda: None)

    view._play("http://cdn/x.m3u8", "", "第1集")

    assert view._has_played is False


def test_play_empty_url_does_not_touch_player(_qapp, monkeypatch):
    view = _view()
    called = []
    monkeypatch.setattr(view, "_ensure_player", lambda hdrs=None: called.append(1))
    view._play("", "", "第1集")
    assert called == []
    assert view._has_played is False


def test_embedded_play_url_wraps_hls_through_proxy(_qapp, monkeypatch):
    """HLS 必须走本地代理（广告过滤 + 分片缓存 + 防盗链头）。"""
    view = _view()
    seen = {}

    class _Proxy:
        def build_url(self, target, headers, ad_block=None, force_proxy=False,
                      source_id=None):
            seen.setdefault("urls", []).append(target)
            return f"http://127.0.0.1:1/s/{len(seen['urls'])}"

    monkeypatch.setattr("framework.media_proxy.MediaProxy.instance",
                        staticmethod(lambda: _Proxy()))
    play, audio = view._embedded_play_url(
        "http://cdn/x.m3u8", "http://cdn/a.m4s", {"Referer": "r"}, {})
    assert play.startswith("http://127.0.0.1:1/s/")
    assert audio.startswith("http://127.0.0.1:1/s/")
    assert seen["urls"] == ["http://cdn/x.m3u8", "http://cdn/a.m4s"]


def test_embedded_play_url_direct_for_plain_mp4(_qapp):
    """非 HLS、无防盗链头、未强制代理 → 直连（少一跳，起播更快）。"""
    view = _view()
    play, audio = view._embedded_play_url("http://cdn/x.mp4", "", {}, {})
    assert play == "http://cdn/x.mp4"
    assert audio == ""


# ---------------------------------------------------------------------- #
# 进度 / 预取时基
# ---------------------------------------------------------------------- #
def test_progress_write_is_throttled_but_prefetch_is_not(_qapp):
    """进度回写 2s 节流；预取时基每次都收到（锁存负责去重）。"""
    view = _view()
    fake = _install(view, _FakePlayer())
    writes, ticks = [], []
    view._progress_cb = lambda idx, loc: writes.append(loc["seconds"])
    view._progress_position.connect(lambda i, s, d: ticks.append(s))
    view._last_pos_save_ts = 0.0

    for t in (100, 101, 102, 103):
        fake._emit_time(view, t)

    assert writes == [100], writes          # 只有第一次落盘
    assert ticks == [101, 102, 103] or len(ticks) >= 3
    _qapp.processEvents()


def test_progress_tick_ignores_bad_time(_qapp):
    view = _view()
    fake = _install(view, _FakePlayer(length_ms=10000))
    writes = []
    view._progress_cb = lambda idx, loc: writes.append(loc)
    view._on_player_time(999999)     # 超过时长 → 丢
    view._on_player_time(-1)         # 负数 → 丢
    assert writes == []


def test_resume_seek_applied_when_length_arrives(_qapp):
    """续播：时长到手后才 seek（此时才能算比例）。"""
    view = _view()
    fake = _install(view, _FakePlayer(length_ms=600000))
    view._pending_seek_s = 120.0
    view._on_player_length(600000)
    assert ("set_position", pytest.approx(0.2)) in [
        (c[0], c[1]) for c in fake.calls if c[0] == "set_position"], fake.calls


def test_resume_seek_skipped_near_end(_qapp):
    """续播点已在片尾 1s 内 → 从头播（跳到末尾等于立刻结束）。"""
    view = _view()
    fake = _install(view, _FakePlayer(length_ms=600000))
    view._pending_seek_s = 599.5
    view._on_player_length(600000)
    assert not [c for c in fake.calls if c[0] == "set_position"]


def test_resume_ratio_used_when_seconds_missing(_qapp):
    view = _view()
    fake = _install(view, _FakePlayer(length_ms=600000))
    view._pending_seek_s = None
    view._pending_seek_ratio = 0.5
    view._on_player_length(600000)
    assert [c for c in fake.calls if c[0] == "set_position"]


# ---------------------------------------------------------------------- #
# 控制 / 结束 / 停止
# ---------------------------------------------------------------------- #
def test_toggle_play_pause_uses_pause_resume(_qapp, monkeypatch):
    view = _view()
    fake = _install(view, _FakePlayer())
    fake._playing = True
    view._current_play = "http://cdn/x.m3u8"

    view._toggle_play_pause()
    assert ("pause",) in fake.calls
    view._toggle_play_pause()
    assert ("resume",) in fake.calls
    # 没有重新播放（不重开、不重新取流）
    assert not [c for c in fake.calls if c[0] == "play"]


def test_media_ended_advances_to_next_episode(_qapp, monkeypatch):
    view = _view(3)
    _install(view, _FakePlayer())
    selected = []
    monkeypatch.setattr(view, "_select_episode", selected.append)
    view._current_idx = 0
    view._on_media_ended()
    assert selected == [1]


def test_media_ended_on_last_episode_does_not_advance(_qapp, monkeypatch):
    view = _view(3)
    _install(view, _FakePlayer())
    selected = []
    monkeypatch.setattr(view, "_select_episode", selected.append)
    view._current_idx = 2
    view._on_media_ended()
    assert selected == []


def test_stop_player_releases_and_resets(_qapp):
    view = _view()
    fake = _install(view, _FakePlayer())
    view._current_play = "http://cdn/x.m3u8"
    view._pending_seek_s = 10.0
    view._stop_player()
    assert ("release",) in fake.calls
    assert view._has_played is False
    assert view._pending_seek_s is None
    assert view._player_length_ms == 0


def test_shutdown_closes_player(_qapp, monkeypatch):
    view = _view()
    fake = _install(view, _FakePlayer())
    monkeypatch.setattr(view, "_stop_cast", lambda *a, **kw: None)
    view.shutdown_video()
    assert ("close",) in fake.calls
    assert view._player is None


def test_open_external_now_replays_embedded(_qapp, monkeypatch):
    """原「外部播放器」入口改为内嵌重播：关闭旧实例并重新 _play。"""
    view = _view()
    fake = _install(view, _FakePlayer())
    view._current_play = "http://cdn/x.m3u8"
    replayed = []
    monkeypatch.setattr(view, "_play", lambda *a: replayed.append(a))
    view._open_external()
    assert ("close",) in fake.calls
    assert replayed and replayed[0][0] == "http://cdn/x.m3u8"


def test_open_external_without_stream_only_hints(_qapp, monkeypatch):
    view = _view()
    _install(view, _FakePlayer())
    view._current_play = ""
    hints = []
    monkeypatch.setattr(view, "_show_status", hints.append)
    view._open_external()
    assert hints, "无当前流时必须给反馈，不能静默"


# ---------------------------------------------------------------------- #
# 控制条位置：必须停在原生视频窗口**外面**
# ---------------------------------------------------------------------- #
def test_control_bar_docked_outside_native_video_surface(_qapp):
    """控制条不能是原生画面控件的后代。

    libvlc 用 set_hwnd 把视频渲染到画面控件的 HWND，并在其中创建自己的
    子窗口（Direct3D 输出）。原生子窗口永远盖在同一窗口里 Qt 绘制的兄弟/
    子控件之上 —— 控制条一旦挂在画面控件内部，开播后整条会被视频盖住，
    用户看到的就是「全屏/倍速/进度/上下集/暂停/投屏按钮全不见了」。
    """
    view = _view()
    assert view.control_bar.parent() is view._player_box
    assert view._video_frame.parent() is view._player_box
    assert not view._video_frame.isAncestorOf(view.control_bar), \
        "控制条被放回原生画面内部了：开播后会被 libvlc 的输出窗口盖住"


def test_all_player_controls_present_and_enabled(_qapp):
    """开播前后这些控件都要在、要可用（含投屏/全屏/倍速/进度）。"""
    view = _view()
    view.show()
    _qapp.processEvents()
    names = ("play_btn", "prev_btn", "next_btn", "progress", "time_label",
             "vol_btn", "vol_slider", "speed_combo", "fs_btn", "settings_btn",
             "cast_btn")
    for name in names:
        w = getattr(view, name, None)
        assert w is not None, f"缺少控件 {name}"
        assert w.isVisibleTo(view), f"{name} 不可见"
        if name != "cast_btn":  # 投屏按钮随「有无源/当前集」禁用，另测
            assert w.isEnabled(), f"{name} 被禁用"


def test_fullscreen_moves_whole_player_box(_qapp, monkeypatch):
    """全屏要把画面 + 控制条一起搬走，否则全屏里没法操作。"""
    view = _view()
    _install(view, _FakePlayer())
    monkeypatch.setattr(view, "_wake_controls", lambda: None)
    view._toggle_fullscreen()
    try:
        assert view._fs_win is not None
        assert view._player_box.parent() is view._fs_win
        assert view.control_bar.isVisibleTo(view._fs_win)
    finally:
        view._exit_fullscreen()
    assert view._fs_win is None
    assert view.control_bar.isVisibleTo(view)


# ---------------------------------------------------------------------- #
# 不残留外部播放器依赖
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("symbol", [
    "open_with_player", "player_command", "player_session", "PlaybackMonitor",
])
def test_video_view_no_longer_calls_external_player(symbol):
    import inspect

    from gui.pages.reader import video_view as module

    src = inspect.getsource(module.VideoView)
    assert f"{symbol}(" not in src, f"VideoView 仍在调用外部播放器：{symbol}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
