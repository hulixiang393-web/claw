# -*- coding: utf-8 -*-
"""内嵌播放器「加载进度 / 播放暂停图标」回归。

现场（用户反馈）：
1. 载入视频时看不到加载进度——原实现把缓冲百分比整条丢掉，只做 show/hide，
   控件还是 `setRange(0, 0)` 的忙碌动画，界面上读不出「加载到哪一步」；
2. 播放/暂停图标显示不正确——`_update_play_button` 只信 `is_playing()`，
   而 libvlc 在 Buffering/Opening/seek 重缓冲期间它都是 False，图标于是
   在缓冲时翻回 ▶；而且图标只在点击时刷新，缓冲→播放、播放→结束不会更新。

本文件只做**契约**验证：不启动 libvlc、不建真实播放器。
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from framework.content import Chapter, Detail  # noqa: E402


def _detail(n=3):
    return Detail(source_id="demo", content_type="video", url="http://e/1",
                  title="w", cover="",
                  chapters=[Chapter(f"第{i + 1}集", f"http://e/{i}") for i in range(n)])


def _make_view(n=3, raw=None):
    from gui.pages.reader.video_view import VideoView

    view = VideoView(None)
    view._detail = _detail(n)
    view._episodes = view._detail.chapters
    view._source = SimpleNamespace(source_id="demo", content_type="video",
                                   raw=raw or {})
    view._current_idx = 0
    return view


@pytest.fixture
def build(_qapp):
    """建一个离屏 VideoView（QWidget 必须先有 QApplication，否则进程直接崩）。"""
    def _make(n=3, raw=None):
        return _make_view(n, raw)
    return _make


@pytest.fixture(autouse=True)
def _embedded_mode(monkeypatch):
    """本文件验证**内嵌**播放链路：关掉「外部播放器优先」。

    那个开关读本机 app_config.json（用户可能已改成外部播放），不钉住的话
    这些契约会随本机配置漂移。
    """
    from gui.pages.reader.video_view import VideoView

    monkeypatch.setattr(VideoView, "_prefer_external_player", lambda self: False)


class _FakePlayer:
    """播放器桩：状态码是唯一真相来源（与 VlcPlayer 的语义一致）。"""

    def __init__(self, state=3):
        self.state = state
        self.calls = []

    def play(self, url, audio="", title="", kind="", network_caching_ms=0):
        self.calls.append(("play", url, kind, int(network_caching_ms)))
        self.state = 3

    def pause(self):
        self.calls.append(("pause",))
        self.state = 4

    def resume(self):
        self.calls.append(("resume",))
        self.state = 3

    def get_state(self):
        return self.state

    def is_active(self):
        return self.state in (2, 3)

    def is_playing(self):
        return self.state == 3

    def get_length(self):
        return 600000

    def get_time(self):
        return 1000


class _LegacyPlayer:
    """老签名播放器（不认 network_caching_ms）。"""

    def __init__(self):
        self.calls = []

    def play(self, url, audio="", title="", kind=""):
        self.calls.append(("play", url, kind))


def _install(view, player, played=True):
    view._player = player
    view._has_played = played
    return player


def _stub_play_pipeline(monkeypatch, view, player):
    monkeypatch.setattr(view, "_ensure_player", lambda hdrs=None: player)
    monkeypatch.setattr(view, "_embedded_play_url", lambda v, a, h, ad: (v, a))
    monkeypatch.setattr(view, "_configure_playback_prefetch", lambda url="": None)
    monkeypatch.setattr(view, "_start_prefetch_ticker", lambda: None)
    monkeypatch.setattr(view, "_start_progress_ticker", lambda: None)


# ---------------------------------------------------------------------- #
# 缓冲百分比归一化
# ---------------------------------------------------------------------- #
def test_buffer_percent_accepts_both_scales(build):
    """libvlc 的 new_cache 有 0~100 与 0~1 两种口径，都要读得懂。"""
    view = build()
    assert view._normalize_buffer_percent(0) == 0
    assert view._normalize_buffer_percent(37) == 37
    assert view._normalize_buffer_percent(100) == 100
    assert view._normalize_buffer_percent(0.42) == 42
    assert view._normalize_buffer_percent(1.0) == 100


def test_buffer_percent_clamps_and_rejects_garbage(build):
    view = build()
    assert view._normalize_buffer_percent(180) == 100
    assert view._normalize_buffer_percent(-5) == 0
    assert view._normalize_buffer_percent(None) == -1
    assert view._normalize_buffer_percent("abc") == -1


# ---------------------------------------------------------------------- #
# 加载进度可见
# ---------------------------------------------------------------------- #
def test_buffering_shows_real_progress_value(build):
    """缓冲事件必须把百分比写到控件上（不是只 show 一个忙碌动画）。"""
    view = build()
    _install(view, _FakePlayer(state=2))

    view._on_player_buffering(42)

    bar = view.buffer_spinner
    assert bar.isHidden() is False
    assert bar.value() == 42
    # format 里是 %p 占位符；实际渲染文本由 text() 给出（进度必须是真值）
    assert "42" in bar.text()


def test_buffering_hides_center_play_button(build):
    """缓冲不是暂停：中央播放键不能盖在进度条上。"""
    view = build()
    _install(view, _FakePlayer(state=2))
    view.center_play_btn.setVisible(True)

    view._on_player_buffering(10)

    assert view.center_play_btn.isHidden() is True


def test_buffering_complete_hides_indicator(build):
    view = build()
    _install(view, _FakePlayer(state=3))

    view._on_player_buffering(50)
    assert view.buffer_spinner.isHidden() is False
    view._on_player_buffering(100)
    assert view.buffer_spinner.isHidden() is True


def test_buffering_feeds_adaptive_tuner(build):
    """卡顿统计必须真的收到缓冲事件（此前一次都没喂过，自愈链路全死）。"""
    view = build()
    _install(view, _FakePlayer(state=2))
    fed = []
    view._tuner = SimpleNamespace(on_buffering=fed.append, reset=lambda: None)

    view._on_player_buffering(20)
    view._on_player_buffering(100)

    assert fed == [20, 100]


# ---------------------------------------------------------------------- #
# 播放/暂停图标
# ---------------------------------------------------------------------- #
def test_icon_stays_pause_while_buffering(build):
    """缓冲中图标必须是 ⏸：is_playing() 这时是 False，不能拿它判定。"""
    view = build()
    _install(view, _FakePlayer(state=2))
    view.play_btn.setText("▶")

    view._on_player_state(2)

    assert view.play_btn.text() == "⏸"


def test_icon_flips_on_state_change_not_only_on_click(build):
    """状态迁移（播放→暂停→播放→结束）必须各自刷新图标。"""
    view = build()
    fake = _install(view, _FakePlayer(state=1))

    def emit(st):
        # 真实 libvlc 里「状态事件」与 get_state() 是一致的，桩也保持同步
        fake.state = st
        view._on_player_state(st)

    emit(3)                           # Playing
    assert view.play_btn.text() == "⏸"
    emit(4)                           # Paused
    assert view.play_btn.text() == "▶"
    assert view.center_play_btn.isHidden() is False
    emit(3)
    assert view.play_btn.text() == "⏸"
    emit(6)                           # Ended
    assert view.play_btn.text() == "▶"


def test_pause_click_on_buffering_player_pauses_not_resumes(build):
    """缓冲中点击必须走 pause：旧的 is_playing() 判定会错走 resume()。"""
    view = build()
    fake = _install(view, _FakePlayer(state=2))
    view._current_play = "http://cdn/x.m3u8"

    view._toggle_play_pause()

    assert ("pause",) in fake.calls
    assert ("resume",) not in fake.calls


def test_player_is_active_treats_buffering_as_playing(build):
    view = build()
    _install(view, _FakePlayer(state=2))
    assert view._player_is_active() is True
    _install(view, _FakePlayer(state=4))
    assert view._player_is_active() is False


# ---------------------------------------------------------------------- #
# 源级 network_caching 下发
# ---------------------------------------------------------------------- #
def test_play_passes_source_network_caching(build, monkeypatch):
    """源声明 media.hls.network_caching_ms 时必须传到播放器。

    内嵌播放器此前一直用 classify 的 HLS 缺省 5000ms，源配置被静默丢弃
    → 缓冲浅于源站分片间隔，网络一抖就见底重缓冲（用户看到的跳帧）。
    """
    raw = {"media": {"format": "hls", "hls": {"network_caching_ms": 30000}}}
    view = build(raw=raw)
    fake = _FakePlayer()
    _stub_play_pipeline(monkeypatch, view, fake)

    view._play("http://cdn/x.m3u8", "", "第1集")

    assert fake.calls and fake.calls[0][0] == "play"
    assert fake.calls[0][2] == "hls"
    assert fake.calls[0][3] == 30000


def test_play_falls_back_for_legacy_player_signature(build, monkeypatch):
    """播放器桩/旧签名不认 network_caching_ms 时仍要能播（不得中断链路）。"""
    raw = {"media": {"format": "hls", "hls": {"network_caching_ms": 30000}}}
    view = build(raw=raw)
    legacy = _LegacyPlayer()
    _stub_play_pipeline(monkeypatch, view, legacy)

    view._play("http://cdn/x.m3u8", "", "第1集")

    assert legacy.calls == [("play", "http://cdn/x.m3u8", "hls")]
    assert view._has_played is True


def test_play_shows_loading_indicator_before_first_segment(build, monkeypatch):
    """起播到首片到达之间必须已有 0% 的加载条（原来这段是纯黑屏无反馈）。"""
    view = build()
    fake = _FakePlayer()
    _stub_play_pipeline(monkeypatch, view, fake)

    view._play("http://cdn/x.m3u8", "", "第1集")

    assert view.buffer_spinner.isHidden() is False
    assert view.buffer_spinner.value() == 0


# ---------------------------------------------------------------------- #
# 缓冲/状态事件的刷新节流（「进播放界面、按按键异常卡顿」回归）
# ---------------------------------------------------------------------- #
def _count_repositions(view, monkeypatch):
    calls = []
    monkeypatch.setattr(view, "_reposition_overlays",
                        lambda *a, **k: calls.append(1))
    return calls


def test_repeated_buffering_percent_is_ignored(build, monkeypatch):
    """libvlc 起播会连发几十条同值 buffering 事件：同值必须直接丢弃。"""
    view = build()
    _install(view, _FakePlayer(state=2))
    fed = []
    view._tuner = SimpleNamespace(on_buffering=fed.append, reset=lambda: None)

    for _ in range(20):
        view._on_player_buffering(7)
    view._on_player_buffering(8)

    assert fed == [7, 8]


def test_buffer_indicator_does_not_relayout_overlays(build, monkeypatch):
    """刷新加载条不得触发整层重排（_reposition_overlays 里的 adjustSize
    在缓冲事件洪水下就是卡顿主因）。"""
    view = build()
    _install(view, _FakePlayer(state=2))
    calls = _count_repositions(view, monkeypatch)

    view._show_buffer_indicator(30)
    view._show_buffer_indicator(31)
    view._show_buffer_indicator(-1)

    assert calls == []
    assert view.buffer_spinner.value() == 31


def test_repeated_state_events_are_ignored(build, monkeypatch):
    view = build()
    _install(view, _FakePlayer(state=3))
    view._on_player_state(3)                 # 先建立基线
    calls = _count_repositions(view, monkeypatch)

    for _ in range(10):
        view._on_player_state(3)

    assert calls == []
    assert view.play_btn.text() == "⏸"


def test_overlay_sync_skips_relayout_when_visibility_unchanged(build, monkeypatch):
    view = build()
    _install(view, _FakePlayer(state=3))
    view._sync_overlay_state(playing=True)   # 基线：这一次会把中央键收起来
    calls = _count_repositions(view, monkeypatch)

    view._sync_overlay_state(playing=True)   # 显隐没变 → 不重排
    assert calls == []
    view._sync_overlay_state(playing=False)  # 要显示 → 必须重排一次
    assert calls == [1]
    view._sync_overlay_state(playing=False)  # 已经显示了 → 不再重排
    assert calls == [1]


def test_play_button_text_not_reset_when_unchanged(build, monkeypatch):
    """进度轮询每秒调一次 _update_play_button：文本没变不能反复 setText。"""
    view = build()
    _install(view, _FakePlayer(state=3))
    view._update_play_button()
    writes = []
    real_set_text = view.play_btn.setText

    def spy(text):
        writes.append(text)
        real_set_text(text)

    monkeypatch.setattr(view.play_btn, "setText", spy)

    for _ in range(5):
        view._update_play_button()

    assert writes == []
