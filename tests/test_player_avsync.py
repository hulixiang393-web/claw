# -*- coding: utf-8 -*-
"""音画漂移看门狗（framework/player_session.py）：avsync → audio-delay 纠偏。

现场（omofuna 2026-11-02 实测）：缓存分片真实帧周期 3.12s，而站点清单
`#EXTINF` 一律 3.00s（477 片累计少约 57s）；长片播放与多次续播之后，
mpv 以音频为主时钟会把"音频略快"的偏差累积到可感知，用户看到的就是
「视频和声音不同步」。换线路/重开播放器都只是重新计时，治不了根：必须在
有解码器时钟信息的地方（mpv 的 `avsync`）检测并纠正。

看门狗要么不动作，要么一次把音轨推到位，且必须防抖：
- 起播预热期内不判定（起播抖动会报 0.2~0.5s 假偏差）
- 0.25~0.6s 需连续两次采样（缓冲尖峰是单次）
- 两次纠偏至少隔 AVSYNC_COOLDOWN_S
- 一次会话最多 AVSYNC_MAX_FIXES 次（源本身时序坏了就别乱推）
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from framework import desktop_player as dp  # noqa: E402
from framework import player_session as ps  # noqa: E402


class _FakeIPC:
    instances: list = []

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
    _FakeIPC.instances = []
    monkeypatch.setattr(ps, "_MpvIPC", _FakeIPC)
    monkeypatch.setattr(ps.desktop_player, "launch",
                        lambda url, **kw: dp.Launched(
                            key=kw.get("preferred") or "mpv", name="mpv",
                            exe="mpv.exe", argv=[], process=_FakeProc(),
                            ipc_server=kw.get("ipc_server", "")))
    monkeypatch.setattr(ps.desktop_player, "load_prefs",
                        lambda *a, **k: dp.Prefs(player="mpv"))
    monkeypatch.setattr(ps.PlayerSession, "_hold_proxy_lease",
                        lambda self, proc: None)
    return ps.PlayerSession()


def _playing(session, at_seconds: float = 6.0):
    """推到「已开播且过了预热期」的稳定状态。"""
    session.play("http://127.0.0.1:9/s/t/index.m3u8")
    session._on_ipc_event({"event": "file-loaded"})
    session._on_ipc_event({
        "event": "property-change", "name": "time-pos", "data": at_seconds,
    })
    assert session.get_state() == ps.ST_PLAYING
    # 预热是按墙钟算的，测试里把"起播时刻"往前拨，等价于真的等了 10 秒
    session._sync_armed_at = time.monotonic() - 10.0
    return _FakeIPC.instances[-1]


def _avsync(session, value: float):
    session._on_ipc_event({
        "event": "property-change", "name": "avsync", "data": value,
    })


def _audio_delays(sent) -> list[float]:
    return [float(cmd[2]) for cmd in sent
            if cmd[:2] == ["set_property", "audio-delay"]]


def test_avsync_is_observed_from_mpv(session):
    ipc = _playing(session)
    names = [c[2] for c in ipc.sent if c[0] == "observe_property"]
    assert "avsync" in names and "audio-delay" in names


def test_small_drift_is_ignored(session):
    ipc = _playing(session)
    for _ in range(4):
        _avsync(session, 0.1)
    assert _audio_delays(ipc.sent) == []


def test_drift_before_warmup_is_ignored(session):
    """起播 5s 内 avsync 会被起播抖动污染，不能照它纠偏。"""
    ipc = _playing(session, at_seconds=1.0)
    _avsync(session, 1.2)
    assert _audio_delays(ipc.sent) == []


def test_drift_while_paused_is_ignored(session):
    ipc = _playing(session)
    session._on_ipc_event({
        "event": "property-change", "name": "pause", "data": True,
    })
    assert session.get_state() == ps.ST_PAUSED
    for _ in range(3):
        _avsync(session, 0.9)
    assert _audio_delays(ipc.sent) == []


def test_medium_drift_needs_two_consecutive_samples(session):
    ipc = _playing(session)
    _avsync(session, 0.35)
    assert _audio_delays(ipc.sent) == [], "单次尖峰不动手"
    _avsync(session, 0.35)
    assert _audio_delays(ipc.sent) == [pytest.approx(0.35)]


def test_hard_drift_corrects_on_first_sample(session):
    ipc = _playing(session)
    _avsync(session, 0.8)
    assert _audio_delays(ipc.sent) == [pytest.approx(0.8)]


def test_negative_drift_pulls_audio_back(session):
    ipc = _playing(session)
    _avsync(session, -0.9)
    assert _audio_delays(ipc.sent) == [pytest.approx(-0.9)]


def test_correction_accumulates_on_current_delay(session):
    """第二次纠偏要基于 mpv 报回的当前 audio-delay，不能覆盖成绝对值。"""
    ipc = _playing(session)
    _avsync(session, 0.8)
    session._on_ipc_event({
        "event": "property-change", "name": "audio-delay", "data": 0.8,
    })
    session._sync_last_fix_at = 0.0     # 冷却期过去
    _avsync(session, 0.5)
    _avsync(session, 0.5)               # 0.5s < 硬阈值，需连续两次
    assert _audio_delays(ipc.sent) == [
        pytest.approx(0.8), pytest.approx(1.3),
    ]


def test_cooldown_blocks_rapid_second_fix(session):
    ipc = _playing(session)
    _avsync(session, 0.8)
    _avsync(session, 0.8)
    _avsync(session, 0.8)
    assert len(_audio_delays(ipc.sent)) == 1
    session._sync_last_fix_at = time.monotonic() - ps.PlayerSession.AVSYNC_COOLDOWN_S - 0.1
    session._on_ipc_event({
        "event": "property-change", "name": "audio-delay", "data": 0.8,
    })
    _avsync(session, 0.8)
    assert len(_audio_delays(ipc.sent)) == 2


def test_fix_count_is_capped_per_file(session):
    ipc = _playing(session)
    for _ in range(ps.PlayerSession.AVSYNC_MAX_FIXES + 3):
        session._sync_last_fix_at = 0.0
        _avsync(session, 0.8)
    assert len(_audio_delays(ipc.sent)) == ps.PlayerSession.AVSYNC_MAX_FIXES


def test_counter_resets_on_next_episode(session):
    ipc = _playing(session)
    for _ in range(ps.PlayerSession.AVSYNC_MAX_FIXES):
        session._sync_last_fix_at = 0.0
        _avsync(session, 0.8)
    assert session._sync_fixes == ps.PlayerSession.AVSYNC_MAX_FIXES
    # 下一集：mpv 会再发 start-file/file-loaded
    session._on_ipc_event({"event": "start-file"})
    session._on_ipc_event({"event": "file-loaded"})
    session._on_ipc_event({
        "event": "property-change", "name": "time-pos", "data": 6.0,
    })
    session._sync_armed_at = time.monotonic() - 10.0   # 新一集也过了预热
    _avsync(session, 0.8)
    assert session._sync_fixes == 1


def test_delay_is_clamped_to_sane_range(session):
    """累计纠偏量封顶在 AVSYNC_MAX_DELAY_S，不能越推越远。"""
    ipc = _playing(session)
    for _ in range(3):
        session._sync_last_fix_at = 0.0     # 冷却期过去
        _avsync(session, 1.4)
    assert _audio_delays(ipc.sent)[-1] == pytest.approx(
        ps.PlayerSession.AVSYNC_MAX_DELAY_S
    )


def test_implausible_drift_is_ignored(session):
    """单次 >1.5s 的读数只可能是起播/缓冲噪声，不能照它纠偏。

    2026-11-04 现场：播放到 5.0s 的**第一个**采样报 avsync=-3.616s，
    看门狗直接把 audio-delay 推到 -3.000s，整场声音被推飞 3 秒。
    """
    ipc = _playing(session)
    _avsync(session, -3.616)
    assert _audio_delays(ipc.sent) == []
    assert session._sync_fixes == 0, "噪声样本不该占用纠偏次数"


def test_resume_position_does_not_skip_warmup(session):
    """续播把 time-pos 顶到 20 分钟，也不等于"预热已完成"。"""
    session.play("http://127.0.0.1:9/s/t/index.m3u8")
    session._on_ipc_event({"event": "file-loaded"})
    session._on_ipc_event({
        "event": "property-change", "name": "time-pos", "data": 1200.0,
    })
    ipc = _FakeIPC.instances[-1]
    _avsync(session, 0.8)
    assert _audio_delays(ipc.sent) == [], "刚起播的 avsync 还不能信"
    session._sync_armed_at = time.monotonic() - 10.0
    _avsync(session, 0.8)
    assert _audio_delays(ipc.sent) == [pytest.approx(0.8)]


def test_sync_signal_reports_applied_delta(session):
    seen = []
    session.sync_corrected.connect(seen.append)
    _playing(session)
    _avsync(session, 0.8)
    assert seen and seen[0] == pytest.approx(0.8)


def test_no_correction_without_ipc(session):
    """mpv 不可用（无 IPC）时看门狗必须静默：不能抛异常打断播放。"""
    session.play("http://127.0.0.1:9/s/t/index.m3u8")
    session._ipc = None
    session._state = ps.ST_PLAYING
    session._time_ms = 6000
    _avsync(session, 1.5)      # 不抛 = 通过


def test_user_audio_delay_is_kept_as_baseline(_qapp, monkeypatch):
    """附加参数里的 --audio-delay 是基线：纠偏在它上面叠加，不能抹成 0。"""
    _FakeIPC.instances = []
    monkeypatch.setattr(ps, "_MpvIPC", _FakeIPC)
    monkeypatch.setattr(ps.desktop_player, "launch",
                        lambda url, **kw: dp.Launched(
                            key="mpv", name="mpv", exe="mpv.exe", argv=[],
                            process=_FakeProc(),
                            ipc_server=kw.get("ipc_server", "")))
    monkeypatch.setattr(
        ps.desktop_player, "load_prefs",
        lambda *a, **k: dp.Prefs(player="mpv",
                                 extra_args=("--audio-delay=0.2",)))
    monkeypatch.setattr(ps.PlayerSession, "_hold_proxy_lease",
                        lambda self, proc: None)
    session = ps.PlayerSession()
    ipc = _playing(session)
    assert session._audio_delay == pytest.approx(0.2)

    _avsync(session, 0.8)
    # 0.2（用户基线）+ 0.8（本次漂移）
    assert _audio_delays(ipc.sent) == [pytest.approx(1.0)]
