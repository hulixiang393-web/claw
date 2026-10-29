# -*- coding: utf-8 -*-
"""本地视频内嵌播放窗口回归（替代原 open_with_player 外播进程）。

约束：**不创建 libvlc 实例**。`LocalVideoPlayer` 只在 `play()` 里才 import
并构造 `VlcPlayer`，所以只要替换 `framework.vlc_player.VlcPlayer` 就能把
整条链路测干净，不会弹窗、不会发出播放请求。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from gui import local_video_player as lvp


class _FakeVlc:
    """VlcPlayer 桩：记录调用并允许手工驱动事件。"""

    instances = []

    def __init__(self, widget=None, *a, **kw):
        self.widget = widget
        self.calls = []
        self._length = 0
        self._time = 0
        self._playing = False
        self.volume = None
        # 信号桩：真实 VlcPlayer 用 Qt Signal，这里只要可 connect 即可
        self.time_changed = _Signal()
        self.length_changed = _Signal()
        self.ended = _Signal()
        self.error = _Signal()
        type(self).instances.append(self)

    def play(self, url, audio="", title="", kind=""):
        self.calls.append(("play", url))
        self.kind = kind
        self._playing = True

    def pause(self):
        self.calls.append(("pause",))
        self._playing = False

    def resume(self):
        self.calls.append(("resume",))
        self._playing = True

    def is_playing(self):
        return self._playing

    def get_time(self):
        return self._time

    def get_length(self):
        return self._length

    def set_position(self, p):
        self.calls.append(("set_position", p))

    def seek_relative(self, s):
        self.calls.append(("seek_relative", s))

    def set_volume(self, v):
        self.volume = int(v)
        self.calls.append(("set_volume", int(v)))

    def rehook(self):
        self.calls.append(("rehook",))

    def close(self):
        self.calls.append(("close",))
        self._playing = False


class _Signal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *a):
        for s in list(self._slots):
            s(*a)


@pytest.fixture
def fake_vlc(monkeypatch):
    _FakeVlc.instances = []
    import framework.vlc_player as vp

    monkeypatch.setattr(vp, "VlcPlayer", _FakeVlc)
    return _FakeVlc


def _player(**kw):
    return lvp.LocalVideoPlayer(None, **kw)


# ---------------------------------------------------------------------- #
def test_play_creates_embedded_player_and_hands_off(_qapp, fake_vlc):
    handed = []
    p = _player(on_handoff=lambda: handed.append(1))
    p.play("D:/v/a.mp4")
    v = fake_vlc.instances[-1]
    assert ("play", "D:/v/a.mp4") in v.calls
    assert handed == [1], "开播必须 handoff 一次（落「最近阅读」）"


def test_handoff_only_once_per_play(_qapp, fake_vlc):
    handed = []
    p = _player(on_handoff=lambda: handed.append(1))
    p.play("D:/v/a.mp4")
    p._handoff()
    p._handoff()
    assert handed == [1]


def test_resume_seek_waits_for_length(_qapp, fake_vlc):
    p = _player()
    p.play("D:/v/a.mp4", start_seconds=120)
    v = fake_vlc.instances[-1]
    assert not [c for c in v.calls if c[0] == "set_position"]
    p._on_length(600000)
    assert ("set_position", pytest.approx(0.2)) in v.calls


def test_resume_skipped_near_end(_qapp, fake_vlc):
    p = _player()
    p.play("D:/v/a.mp4", start_seconds=599.5)
    v = fake_vlc.instances[-1]
    p._on_length(600000)
    assert not [c for c in v.calls if c[0] == "set_position"]


def test_no_resume_when_start_invalid(_qapp, fake_vlc):
    p = _player()
    p.play("D:/v/a.mp4", start_seconds=None)
    v = fake_vlc.instances[-1]
    p._on_length(600000)
    assert not [c for c in v.calls if c[0] == "set_position"]


def test_progress_callback_is_throttled(_qapp, fake_vlc):
    got = []
    p = _player(on_progress=lambda s, d, pos: got.append(s))
    p.play("D:/v/a.mp4")
    p._on_length(600000)
    p._last_save_ts = 0.0
    for t in (1000, 2000, 3000):
        p._on_time(t)
    assert got == [1.0], got        # 2s 节流：只落一次


def test_progress_ignores_out_of_range_time(_qapp, fake_vlc):
    got = []
    p = _player(on_progress=lambda s, d, pos: got.append(s))
    p.play("D:/v/a.mp4")
    p._on_length(10000)
    p._last_save_ts = 0.0
    p._on_time(999999)
    p._on_time(-5)
    assert got == []


def test_toggle_play_pause_uses_pause_resume(_qapp, fake_vlc):
    p = _player()
    p.play("D:/v/a.mp4")
    v = fake_vlc.instances[-1]
    v._playing = True
    p.toggle_play()
    assert ("pause",) in v.calls
    p.toggle_play()
    assert ("resume",) in v.calls


def test_seek_released_uses_ratio(_qapp, fake_vlc):
    p = _player()
    p.play("D:/v/a.mp4")
    v = fake_vlc.instances[-1]
    p._on_length(600000)
    p.progress.setValue(500)
    p._on_seek_released()
    assert ("set_position", pytest.approx(0.5)) in v.calls


def test_volume_applied_on_play_and_change(_qapp, fake_vlc):
    p = _player()
    p.vol.setValue(55)
    p.play("D:/v/a.mp4")
    v = fake_vlc.instances[-1]
    assert v.volume == 55
    p.vol.setValue(20)
    assert v.volume == 20


def test_ended_stops_polling_and_notifies(_qapp, fake_vlc):
    done = []
    p = _player(on_finished=lambda: done.append(1))
    p.play("D:/v/a.mp4")
    p._on_ended()
    assert done == [1]
    assert not p._timer.isActive()


def test_close_releases_player(_qapp, fake_vlc):
    p = _player()
    p.play("D:/v/a.mp4")
    v = fake_vlc.instances[-1]
    p.close()
    assert ("close",) in v.calls
    assert p._player is None


def test_fullscreen_toggles_without_reparent(_qapp, fake_vlc):
    p = _player()
    widget = p.video
    p.toggle_fullscreen()
    assert p._is_fs is True
    assert p.video is widget, "全屏不得重建画面控件（会换 HWND → 黑屏）"
    p.toggle_fullscreen()
    assert p._is_fs is False


def test_non_widget_parent_is_tolerated(_qapp, fake_vlc):
    """测试/脚本可能传轻量窗口替身：不能因此 TypeError 把播放带崩。"""
    p = lvp.LocalVideoPlayer(object())
    assert p.parent() is None


def test_replay_reuses_player_instance(_qapp, fake_vlc):
    """同一窗口换文件：复用播放器实例（重建会丢 HWND 与解码器）。"""
    p = _player()
    p.play("D:/v/a.mp4")
    p.play("D:/v/b.mp4")
    assert len(fake_vlc.instances) == 1
    assert ("play", "D:/v/b.mp4") in fake_vlc.instances[0].calls


def test_finite_seconds():
    assert lvp.finite_seconds(12) == 12.0
    assert lvp.finite_seconds("12") is None
    assert lvp.finite_seconds(-1) is None
    assert lvp.finite_seconds(True) is None
    assert lvp.finite_seconds(float("inf")) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
