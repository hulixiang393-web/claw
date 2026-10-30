# -*- coding: utf-8 -*-
"""播放后端（framework/player_session.py）：mpv IPC 状态映射 + 降级路径。

VLC 已整体下线，本文件锁住新后端的对外契约：
- 信号语义（state_changed / time_changed / length_changed / buffering / ended）
- 状态码沿用旧 VLC 的值（video_view 的图标与覆盖层逻辑依赖同一套值）
- IPC 事件到信号的映射（time-pos / pause / core-idle / demuxer-cache-state / end-file）
- IPC 不可用时**降级不失败**（播放照常，界面拿不到进度）
- 控制命令真的发到了 IPC（暂停/seek/音量/倍速）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from framework import desktop_player as dp  # noqa: E402
from framework import player_session as ps  # noqa: E402


# --------------------------------------------------------------------------- #
# 桩：替掉真实进程与命名管道
# --------------------------------------------------------------------------- #
class _FakeIPC:
    instances = []

    def __init__(self, server, on_event):
        self.server = server
        self.on_event = on_event
        self.sent = []
        self.ok = True
        _FakeIPC.instances.append(self)

    def connect(self, timeout=0):
        return self.ok

    def send(self, command):
        self.sent.append(list(command))
        return len(self.sent)

    def close(self):
        self.ok = False


class _FakeProc:
    pid = 4242

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        return None


@pytest.fixture
def session(_qapp, monkeypatch):
    ps._FakeIPC = _FakeIPC  # 供 monkeypatch 引用
    _FakeIPC.instances = []
    monkeypatch.setattr(ps, "_MpvIPC", _FakeIPC)
    monkeypatch.setattr(ps.desktop_player, "launch",
                        lambda url, **kw: dp.Launched(
                            key=kw.get("preferred") or "mpv", name="mpv",
                            exe="mpv.exe", argv=[], process=_FakeProc(),
                            ipc_server=kw.get("ipc_server", "")))
    monkeypatch.setattr(ps.desktop_player, "load_prefs",
                        lambda *a, **k: dp.Prefs(player="mpv"))
    # 代理租约：不真的起代理
    monkeypatch.setattr(ps.PlayerSession, "_hold_proxy_lease",
                        lambda self, proc: None)
    return ps.PlayerSession()


def _events(session, payload):
    session._on_ipc_event(payload)


# --------------------------------------------------------------------------- #
# 状态码与信号
# --------------------------------------------------------------------------- #
def test_state_codes_match_legacy_values():
    """video_view 的图标/覆盖层按这套值判断，改动即破坏界面契约。"""
    assert (ps.ST_NOTHING, ps.ST_OPENING, ps.ST_BUFFERING, ps.ST_PLAYING,
            ps.ST_PAUSED, ps.ST_STOPPED, ps.ST_ENDED, ps.ST_ERROR) == tuple(range(8))


def test_play_launches_backend_and_attaches_ipc(session, monkeypatch):
    session.play("http://127.0.0.1:9/s/t/index.m3u8", title="第1集")
    assert _FakeIPC.instances, "mpv 必须带 IPC 才谈得上进度同步"
    ipc = _FakeIPC.instances[-1]
    observed = [c[1] for c in ipc.sent if c[0] == "observe_property"]
    assert "time-pos" in observed and "duration" in observed
    assert session.get_state() == ps.ST_OPENING


def test_time_pos_drives_progress_signals(session):
    seen = []
    session.time_changed.connect(seen.append)
    session.play("http://x/y.m3u8")
    _events(session, {"event": "property-change", "name": "time-pos",
                      "data": 12.5})
    assert seen == [12500]
    assert session.get_time() == 12500
    assert session.get_state() == ps.ST_PLAYING


def test_duration_drives_length_and_position(session):
    lengths = []
    session.length_changed.connect(lengths.append)
    session.play("http://x/y.m3u8")
    _events(session, {"event": "property-change", "name": "duration",
                      "data": 100.0})
    _events(session, {"event": "property-change", "name": "time-pos",
                      "data": 25.0})
    assert lengths == [100000]
    assert session.get_position() == pytest.approx(0.25)


def test_pause_property_maps_to_state(session):
    states = []
    session.state_changed.connect(states.append)
    session.play("http://x/y.m3u8")
    _events(session, {"event": "property-change", "name": "pause", "data": True})
    _events(session, {"event": "property-change", "name": "pause", "data": False})
    assert states[-2:] == [ps.ST_PAUSED, ps.ST_PLAYING]
    assert session.is_paused() is False and session.is_playing() is True


def test_core_idle_maps_to_buffering(session):
    states = []
    session.state_changed.connect(states.append)
    session.play("http://x/y.m3u8")
    _events(session, {"event": "file-loaded"})
    _events(session, {"event": "property-change", "name": "core-idle",
                      "data": True})
    _events(session, {"event": "property-change", "name": "core-idle",
                      "data": False})
    assert ps.ST_BUFFERING in states and states[-1] == ps.ST_PLAYING
    assert session.is_active() is True


def test_cache_state_maps_to_buffering_percent(session):
    percents = []
    session.buffering.connect(percents.append)
    session.play("http://x/y.m3u8")
    _events(session, {"event": "property-change", "name": "demuxer-cache-state",
                      "data": {"underrun": False, "cache-buffering-state": 42}})
    _events(session, {"event": "property-change", "name": "demuxer-cache-state",
                      "data": {"underrun": True, "cache-buffering-state": 99}})
    assert percents == [42, 0]


def test_eof_emits_ended_once(session):
    ended = []
    session.ended.connect(lambda: ended.append(1))
    session.play("http://x/y.m3u8")
    _events(session, {"event": "end-file", "reason": "eof"})
    assert ended == [1]
    assert session.get_state() == ps.ST_ENDED


def test_end_file_other_reason_is_not_ended(session):
    ended = []
    session.ended.connect(lambda: ended.append(1))
    session.play("http://x/y.m3u8")
    _events(session, {"event": "end-file", "reason": "error"})
    assert ended == []
    assert session.get_state() == ps.ST_STOPPED


def test_duplicate_state_is_not_re_emitted(session):
    states = []
    session.state_changed.connect(states.append)
    session.play("http://x/y.m3u8")
    before = len(states)
    for _ in range(5):
        _events(session, {"event": "file-loaded"})
    assert len(states) == before + 1


# --------------------------------------------------------------------------- #
# 控制命令
# --------------------------------------------------------------------------- #
def test_control_commands_are_forwarded(session):
    session.play("http://x/y.m3u8")
    ipc = _FakeIPC.instances[-1]
    ipc.sent.clear()

    session.pause()
    session.resume()
    session.set_volume(35)
    session.set_rate(1.5)
    session.toggle_mute()
    session.seek_relative(30)

    sent = [c for c in ipc.sent]
    assert ["set_property", "pause", "yes"] in sent
    assert ["set_property", "pause", "no"] in sent
    assert ["set_property", "volume", "35"] in sent
    assert ["set_property", "speed", "1.5"] in sent
    assert ["set_property", "mute", "yes"] in sent
    assert ["seek", "30.0", "relative"] in sent


def test_set_position_uses_absolute_seek_when_length_known(session):
    session.play("http://x/y.m3u8")
    _events(session, {"event": "property-change", "name": "duration",
                      "data": 200.0})
    ipc = _FakeIPC.instances[-1]
    ipc.sent.clear()
    session.set_position(0.5)
    assert ["seek", "100.0", "absolute"] in ipc.sent


def test_close_quits_and_resets(session):
    session.play("http://x/y.m3u8")
    ipc = _FakeIPC.instances[-1]
    session.close()
    assert ["quit"] in ipc.sent
    assert ipc.ok is False
    assert session.get_length() == 0 and session.get_time() == 0


# --------------------------------------------------------------------------- #
# 降级路径：不是 mpv / 没有 IPC
# --------------------------------------------------------------------------- #
def test_non_mpv_backend_degrades_without_ipc(_qapp, monkeypatch):
    """用户选 PotPlayer 时播放照常，只是界面拿不到进度（不得抛错）。"""
    monkeypatch.setattr(ps.desktop_player, "launch",
                        lambda url, **kw: dp.Launched(
                            key="potplayer", name="PotPlayer", exe="p.exe",
                            argv=[], process=_FakeProc(), ipc_server=""))
    monkeypatch.setattr(ps.desktop_player, "load_prefs",
                        lambda *a, **k: dp.Prefs(player="potplayer"))
    monkeypatch.setattr(ps.PlayerSession, "_hold_proxy_lease",
                        lambda self, proc: None)
    session = ps.PlayerSession()
    session.play("http://x/y.m3u8", title="t")
    assert session.get_state() == ps.ST_PLAYING
    # 控制命令全部安全空转
    session.pause()
    session.set_position(0.3)
    session.set_volume(10)
    session.close()


def test_ipc_connect_failure_keeps_playing(_qapp, monkeypatch):
    class _DeadIPC(_FakeIPC):
        def connect(self, timeout=0):
            return False

    monkeypatch.setattr(ps, "_MpvIPC", _DeadIPC)
    monkeypatch.setattr(ps.desktop_player, "launch",
                        lambda url, **kw: dp.Launched(
                            key="mpv", name="mpv", exe="mpv.exe", argv=[],
                            process=_FakeProc(), ipc_server=r"\\.\pipe\x"))
    monkeypatch.setattr(ps.desktop_player, "load_prefs",
                        lambda *a, **k: dp.Prefs(player="mpv"))
    monkeypatch.setattr(ps.PlayerSession, "_hold_proxy_lease",
                        lambda self, proc: None)
    session = ps.PlayerSession()
    session.play("http://x/y.m3u8")
    assert session.get_state() == ps.ST_PLAYING   # 不因 IPC 失败而报错


def test_play_rejects_empty_url(session):
    with pytest.raises(ValueError):
        session.play("")


# --------------------------------------------------------------------------- #
# IPC 客户端：分帧、容错
# --------------------------------------------------------------------------- #
def test_ipc_read_loop_splits_frames_and_survives_garbage():
    received = []

    class _Conn:
        def __init__(self):
            self.chunks = [
                b'{"event":"a"}\n{"event":"b',
                b'"}\nnot json\n{"event":"c"}\n',
                b"",
            ]

        def read(self, n):
            return self.chunks.pop(0) if self.chunks else b""

        def close(self):
            return None

        def write(self, data):
            return len(data)

    ipc = ps._MpvIPC("x", received.append)
    ipc._conn = _Conn()
    ipc._alive.set()
    ipc._read_loop()
    assert [m.get("event") for m in received] == ["a", "b", "c"]
    assert ipc.ok is False


def test_ipc_send_is_noop_when_closed():
    ipc = ps._MpvIPC("x", lambda m: None)
    assert ipc.send(["quit"]) == -1
    ipc.close()
