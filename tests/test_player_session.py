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
    observes = [c for c in ipc.sent if c[0] == "observe_property"]
    # mpv 的签名是 observe_property <id:int> <name>；传属性名当 id 会被
    # 回 invalid parameter，事件一条都不来（进度条/时长全死）。
    assert all(isinstance(c[1], int) for c in observes), observes
    ids = [c[1] for c in observes]
    assert len(ids) == len(set(ids)), f"observe id 必须唯一：{ids}"
    names = [c[2] for c in observes]
    assert {"time-pos", "duration", "pause", "demuxer-cache-state"} <= set(names)
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


def test_idle_before_file_loaded_is_ignored(session):
    """mpv 带 --idle=yes 启动时先发一条 idle：此时还没加载文件，
    不能据此判成"已停止"，否则一开播界面就显示已播完。"""
    session.play("http://x/y.m3u8")
    _events(session, {"event": "idle"})
    assert session.get_state() == ps.ST_OPENING

    _events(session, {"event": "start-file"})
    _events(session, {"event": "file-loaded"})
    assert session.get_state() == ps.ST_PLAYING
    # 文件加载之后的 idle 才代表播放结束/被卸载
    _events(session, {"event": "idle"})
    assert session.get_state() == ps.ST_STOPPED


def test_idle_never_overrides_ended_state(session):
    session.play("http://x/y.m3u8")
    _events(session, {"event": "file-loaded"})
    _events(session, {"event": "end-file", "reason": "eof"})
    assert session.get_state() == ps.ST_ENDED
    _events(session, {"event": "idle"})
    assert session.get_state() == ps.ST_ENDED


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


def test_closing_fresh_session_keeps_nothing_state(session):
    """没播过就 close() 不该冒出一个"已停止"，否则界面会闪一下。"""
    states = []
    session.state_changed.connect(states.append)
    session.close()
    assert states == []
    assert session.get_state() == ps.ST_NOTHING


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


class _Recorder:
    """记录写入内容的假句柄。"""

    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, data):
        self.written.append(data)
        return len(data)

    def read(self, n):  # pragma: no cover —— 读句柄不该被写
        return b""

    def close(self):
        self.closed = True


def test_ipc_send_writes_through_the_shared_connection():
    """观察和控制必须走**同一条**连接：mpv 的 property-change 事件与命令
    回执只发给发命令的那个客户端，拆成两条连接就再也收不到进度事件。"""
    handle = _Recorder()
    ipc = ps._MpvIPC("x", lambda m: None)
    ipc._conn = handle
    ipc._alive.set()

    first = ipc.send(["set_property", "pause", "yes"])
    second = ipc.send(["seek", "4.0", "absolute"])
    assert first > 0 and second > first
    assert len(handle.written) == 2
    payload = json.loads(handle.written[0].decode("utf-8"))
    assert payload["command"] == ["set_property", "pause", "yes"]
    assert payload["request_id"] == first


def test_ipc_read_loop_polls_available_bytes_when_handle_is_known():
    """Windows 上必须先 Peek 再按"可用字节数"读：
    句柄上挂起一次阻塞 read 会让同线程之外的 write 卡死（暂停后拖进度条
    就死在这上面），所以 read 的 size 只能是当前可读量，不能是 65536。"""
    received = []
    sizes = []

    class _Conn:
        def __init__(self):
            self.pending = [b'{"event":"a"}\n', b'{"event":"b"}\n']

        def read(self, n):
            sizes.append(n)
            return self.pending.pop(0) if self.pending else b""

        def close(self):
            return None

        def write(self, data):
            return len(data)

    ipc = ps._MpvIPC("x", received.append)
    ipc._conn = _Conn()
    ipc._handle = 1234          # 假装是 Windows 管道句柄
    ipc._alive.set()

    peeked = {"left": [8, 8, -1]}

    def fake_peek():
        return peeked["left"].pop(0) if peeked["left"] else 0

    ipc._peek_available = fake_peek
    ipc._read_loop()

    assert [m.get("event") for m in received] == ["a", "b"]
    assert sizes == [8, 8], f"必须按可用字节数读，实际 {sizes}"
    assert ipc.ok is False


def test_ipc_peek_returning_negative_stops_reader():
    ipc = ps._MpvIPC("x", lambda m: None)
    ipc._conn = _Recorder()
    ipc._handle = 4321
    ipc._alive.set()
    ipc._peek_available = lambda: -1
    ipc._read_loop()
    assert ipc.ok is False


def test_ipc_connect_opens_single_readwrite_connection(monkeypatch):
    opened = []

    class _FakePipe(_Recorder):
        def fileno(self):
            raise OSError

    def fake_open(path, mode="rb", buffering=-1):
        opened.append((path, mode))
        return _FakePipe()

    monkeypatch.setattr("builtins.open", fake_open)
    ipc = ps._MpvIPC(r"\\.\pipe\test", lambda m: None)
    assert ipc.connect(timeout=0.5) is True
    assert [mode for _path, mode in opened] == ["r+b"]
    assert ipc._handle is None, "拿不到 fileno 时退化为阻塞读，不能崩"
    ipc.close()
    assert ipc._conn is None
