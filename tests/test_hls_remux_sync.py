# -*- coding: utf-8 -*-
"""分片时间戳整理不得破坏音画同步（omofuna 现场「视频和声音不同步」回归）。

现场证据（2026-11-02）：
- 站点清单 `#EXTINF` 一律 3.00s，而缓存分片实测帧周期 3.12s（477 片累计差
  约 57s）→ 清单时间轴与媒体时间轴本来就有 ~4% 漂移；
- 老 `_ts_shift_bytes` 在「流内连续性」判定触发时会把 `resid` 推到**之后**的
  包上，而音、视频包在同一片里交错 —— 一旦某条流触发了修正，同一片内音频
  被平移到 X、视频被平移到 X+resid，两条流拿到不同时基。播放器只能按某个
  主时钟对齐，表现就是画面和声音越播越偏。

本文件锁死改造后的不变量：
1. 一整片只用**一个** delta（PES 与 PCR 同一个），音视频相对偏移逐 tick 不变；
2. 单流 DTS 出现真倒退时不改写（返回 None），绝不交出时基可疑的分片；
3. 跨片大跳变（站点把两段片子硬拼）仍然整片平直，同步关系照样保持。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import framework.media_proxy as mp  # noqa: E402

HZ = 90000
VIDEO_PID = 0x100
AUDIO_PID = 0x101
STREAM_ID = {VIDEO_PID: 0xE0, AUDIO_PID: 0xC0}


def _encode_ts5(value: int) -> bytes:
    value &= 0x1FFFFFFFF
    return bytes([
        ((value >> 29) & 0x0E) | 0x21,
        (value >> 22) & 0xFF,
        ((value >> 14) & 0xFE) | 0x01,
        (value >> 7) & 0xFF,
        ((value << 1) & 0xFE) | 0x01,
    ])


def _encode_pcr(base90: int) -> bytes:
    base90 &= 0x1FFFFFFFF
    return bytes([
        (base90 >> 25) & 0xFF,
        (base90 >> 17) & 0xFF,
        (base90 >> 9) & 0xFF,
        (base90 >> 1) & 0xFF,
        ((base90 & 1) << 7) | 0x7E,
        0,
    ])


def _packet(pid: int, cc: int, pts: float, dts: float,
            pcr: float | None = None) -> bytes:
    header = bytes([
        0x47,
        0x40 | ((pid >> 8) & 0x1F),
        pid & 0xFF,
        (0x30 if pcr is not None else 0x10) | (cc & 0x0F),
    ])
    af = b""
    if pcr is not None:
        body = bytes([0x10]) + _encode_pcr(int(round(pcr * HZ)))
        af = bytes([len(body)]) + body
    hdr = (
        b"\x00\x00\x01" + bytes([STREAM_ID[pid]]) + b"\x00\x00"
        + bytes([0xC0, 0xC0, 10])
        + _encode_ts5(int(round(pts * HZ)))
        + _encode_ts5(int(round(dts * HZ)))
    )
    room = 188 - len(header) - len(af)
    assert len(hdr) <= room
    return header + af + hdr + b"\xff" * (room - len(hdr))


def _muxed(video_start: float = 1.0, audio_start: float = 1.5,
           count: int = 4, step: float = 0.1) -> bytes:
    """音视频交错的 TS 分片：每帧视频后跟一帧音频（偏移 0.5s 起步）。"""
    out = []
    for i in range(count):
        out.append(_packet(VIDEO_PID, i, video_start + i * step,
                           video_start + i * step,
                           pcr=video_start - 0.7 + i * step))
        out.append(_packet(AUDIO_PID, i, audio_start + i * step,
                           audio_start + i * step))
    return b"".join(out)


def _pcrs(data: bytes) -> list[float]:
    """分片内 PCR 的绝对时刻（秒）。"""
    out: list[float] = []
    for off in range(0, len(data) - 187, 188):
        assert data[off] == 0x47
        q = mp._read_ts_pcr_offset(data, off, 188)
        if q is None:
            continue
        base = ((data[q] << 25) | (data[q + 1] << 17) | (data[q + 2] << 9)
                | (data[q + 3] << 1) | (data[q + 4] >> 7))
        ext = ((data[q + 4] & 1) << 8) | data[q + 5]
        out.append(base / HZ + ext / 27000000.0)
    return out


def _offsets(data: bytes) -> dict:
    """每个包的 (PID, PTS, DTS)，按出现顺序 —— 用来逐包比较平移量。"""
    out = []
    for off in range(0, len(data) - 187, 188):
        assert data[off] == 0x47
        pid = ((data[off + 1] & 0x1F) << 8) | data[off + 2]
        values = [mp._decode_ts5(data, p) for p in
                  mp._read_ts_timestamps(data, off, 188)]
        out.append((pid, values))
    return out


def _av_offset(raw: bytes) -> float:
    """音视频起点差（秒）：video 最小时间戳 → audio 最小时间戳。"""
    seen: dict[int, int] = {}
    for pid, values in _offsets(raw):
        if not values:
            continue
        low = min(values)
        if pid not in seen or low < seen[pid]:
            seen[pid] = low
    return (seen[AUDIO_PID] - seen[VIDEO_PID]) / float(HZ)


# --------------------------------------------------------------------------- #
# 不变量 1：整片单一 delta，音视频相对偏移逐 tick 不变
# --------------------------------------------------------------------------- #
def test_shift_preserves_audio_video_offset_exactly():
    raw = _muxed()
    before = _av_offset(raw)
    assert before == pytest.approx(0.5)

    out = mp._ts_shift_bytes(raw, 10.0 * HZ)
    assert out is not None, "本片起点早于锚点，必须整片平移到锚点"
    shifted, duration = out
    # 片长按"两条流合起来的时间跨度"算（音轨比视频长 0.5s 时就是 0.8s），
    # 这才是下一片锚点的推进量，不会把后一片推回已播过的时刻。
    assert duration == pytest.approx(0.8)
    # 逐 tick 相等：不是"约等于"，是同一个 delta 加到两条流上
    assert _av_offset(shifted) == pytest.approx(before, abs=1e-9)
    assert _offsets_shift_is_uniform(raw, shifted)


def _offsets_shift_is_uniform(raw: bytes, shifted: bytes) -> bool:
    """每个时间戳字段的平移量必须完全一致（PES 与 PCR 都不例外）。"""
    deltas = set()
    raw_entries = _offsets(raw)
    new_entries = _offsets(shifted)
    assert len(raw_entries) == len(new_entries)
    for (_pid, old), (_pid2, new) in zip(raw_entries, new_entries):
        assert len(old) == len(new)
        for a, b in zip(old, new):
            deltas.add(b - a)
    assert len(deltas) == 1, f"同一片内出现多个平移量：{sorted(deltas)}"
    return True


def test_pcr_shifted_by_the_same_delta_as_pes():
    raw = _muxed()
    out = mp._ts_shift_bytes(raw, 10.0 * HZ)
    shifted, _duration = out
    raw_pcrs = _pcrs(raw)
    new_pcrs = _pcrs(shifted)
    assert raw_pcrs and new_pcrs
    deltas = {round(b - a, 6) for a, b in zip(raw_pcrs, new_pcrs)}
    assert deltas == {round(9.0, 6)}, deltas


def test_audio_and_video_stay_delta_identical_after_forward_splice():
    """跨片硬拼（30.9s → 211.5s）整片拉回锚点：同步关系同样不变。"""
    raw = _muxed(video_start=211.5, audio_start=212.0)
    before = _av_offset(raw)
    out = mp._ts_shift_bytes(raw, 30.9 * HZ)
    assert out is not None
    shifted, _duration = out
    assert _av_offset(shifted) == pytest.approx(before, abs=1e-9)
    video_min = min(v for pid, values in _offsets(shifted) if pid == VIDEO_PID
                    for v in values) / float(HZ)
    assert video_min == pytest.approx(30.9)


# --------------------------------------------------------------------------- #
# 不变量 2：单流倒退 → 不动字节（不制造错轴）
# --------------------------------------------------------------------------- #
def test_backward_jump_inside_one_stream_is_left_untouched():
    """视频流内部倒退（源片硬拼），整片平移救不了 —— 必须返回 None。

    老实现会在这里推进 resid 把视频"修"回去，音频却留在原来的偏移上，
    交出的分片音视频时基不一致，正是用户看到的「不同步」。
    """
    packets = [
        _packet(VIDEO_PID, 0, 1.0, 1.0),
        _packet(AUDIO_PID, 0, 1.5, 1.5),
        _packet(VIDEO_PID, 1, 1.1, 1.1),
        _packet(AUDIO_PID, 1, 1.6, 1.6),
        _packet(VIDEO_PID, 2, 0.5, 0.5),      # 视频倒退 0.6s
        _packet(AUDIO_PID, 2, 1.7, 1.7),
    ]
    assert mp._ts_shift_bytes(b"".join(packets), 10.0 * HZ) is None


def test_continuous_streams_are_still_shifted():
    raw = _muxed()
    assert mp._ts_shift_bytes(raw, 10.0 * HZ) is not None


def test_timestamp_wraparound_is_not_mistaken_for_backward_jump():
    """33 位时间戳回绕（2^33-0.05 → 0.05）不是倒退，不能因此放弃整理。"""
    span = float(1 << 33)
    packets = [
        _packet(VIDEO_PID, 0, span - 0.1, span - 0.1),
        _packet(VIDEO_PID, 1, 0.05, 0.05),
    ]
    raw = b"".join(packets)
    # 起点远大于锚点（回绕前的值），距锚点远超 _REMUX_MAX_FORWARD_JUMP → 平直
    assert mp._ts_shift_bytes(raw, 10.0 * HZ) is not None
