# -*- coding: utf-8 -*-
"""分片重封装时间轴回归：首片锚点取真实结束时间，PCR 与 PES 同步平移。"""
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

import framework.media_proxy as mp


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


def _packet(pid: int, cc: int, pts: float, pcr: float | None = None,
            dts: float | None = None, stream_id: int = 0xE0) -> bytes:
    has_af = pcr is not None
    header = bytes([
        0x47,
        0x40 | ((pid >> 8) & 0x1F),
        pid & 0xFF,
        (0x30 if has_af else 0x10) | (cc & 0x0F),
    ])
    af = b""
    if has_af:
        body = bytes([0x10]) + _encode_pcr(int(round(pcr * 90000)))
        af = bytes([len(body)]) + body
    flags = 0xC0 if dts is not None else 0x80
    hdr = (
        b"\x00\x00\x01" + bytes([stream_id]) + b"\x00\x00"
        + bytes([flags, flags, 10 if dts is not None else 5])
        + _encode_ts5(int(round(pts * 90000)))
    )
    if dts is not None:
        hdr += _encode_ts5(int(round(dts * 90000)))
    room = 188 - len(header) - len(af)
    assert len(hdr) <= room
    return header + af + hdr + b"\xff" * (room - len(hdr))


def _segment(start: float = 1.4, count: int = 4) -> bytes:
    return b"".join(
        _packet(0x100, i, start + i * 0.1, pcr=start - 0.7 + i * 0.1)
        for i in range(count)
    )


def _timestamps(data: bytes) -> tuple[list[float], list[float]]:
    pts, pcrs = [], []
    for off in range(0, len(data) - 187, 188):
        assert data[off] == 0x47
        q = mp._read_ts_pcr_offset(data, off, 188)
        if q is not None:
            base = ((data[q] << 25) | (data[q + 1] << 17) | (data[q + 2] << 9)
                    | (data[q + 3] << 1) | (data[q + 4] >> 7))
            ext = ((data[q + 4] & 1) << 8) | data[q + 5]
            pcrs.append(base / 90000.0 + ext / 27000000.0)
        for p in mp._read_ts_timestamps(data, off, 188):
            pts.append(mp._decode_ts5(data, p) / 90000.0)
    return pts, pcrs


def test_ts_shift_moves_pcr_with_pes(tmp_path: Path):
    path = tmp_path / "seg.ts"
    path.write_bytes(_segment())
    duration = mp._ts_shift(path, 10.0)
    assert duration == pytest.approx(0.3)
    pts, pcrs = _timestamps(path.read_bytes())
    assert min(pts) == pytest.approx(10.0)
    assert max(pts) == pytest.approx(10.3)
    assert min(pcrs) == pytest.approx(9.3)
    assert max(pcrs) == pytest.approx(9.6)


def test_remux_anchor_uses_first_segment_end():
    key = f"test-{uuid.uuid4().hex}"
    raw = _segment()
    bases = []

    def fake_run_remux(self, ffmpeg, data, base, req_headers=None):
        bases.append(base)
        return data, 0.3

    proxy = object.__new__(mp.MediaProxy)
    with patch.object(mp, "_locate_ffmpeg", return_value="ffmpeg"), \
            patch.object(mp.MediaProxy, "_run_remux", fake_run_remux):
        assert proxy._remux_segment_locked(key, "s0.ts", raw, None) is None
        ctx = mp._remux_ctx(key)
        assert ctx["segs"]["s0.ts"] == pytest.approx(1.7)
        assert "s0.ts" in ctx["processed"]
        assert proxy._remux_segment_locked(key, "s1.ts", raw, None) == raw
        assert proxy._remux_segment_locked(key, "s2.ts", raw, None) == raw
    assert bases == [pytest.approx(1.7), pytest.approx(2.0)]


def test_marked_segment_skips_remux_and_registers_end(tmp_path: Path):
    key = f"test-{uuid.uuid4().hex}"
    path = tmp_path / "s0.ts"
    path.write_bytes(_segment())
    mp._remux_marker(path).touch()
    proxy = object.__new__(mp.MediaProxy)
    with patch.object(mp.MediaProxy, "_remux_segment",
                      side_effect=AssertionError("should not remux")):
        assert proxy._serve_remux_from_file(
            object(), key, "s0.ts", "http://example/seg.ts", path) is False
    ctx = mp._remux_ctx(key)
    assert ctx["segs"]["s0.ts"] == pytest.approx(1.7)
    assert "s0.ts" in ctx["processed"]
