# -*- coding: utf-8 -*-
"""TS 时间戳整理的单位与大跳修复（2026-10-29）。

现场：所有源在卡顿恢复后必然跳帧 + 声音丢失。排查到两处时间戳单位错误，
合起来把每片都推到不可能的位置：

1. `ctx["segs"]` 存的是 `_ts_last_ts()` 的**秒**，却被当作 **90kHz ticks**
   使用 → base 恒为 0~1e-6，每片都往 0 附近平移，各片时间轴互相重叠。
2. `_ts_shift_bytes` 里 `anchor = first_dts * 90000`，而调用方传的已经是
   ticks → 多乘 90000 倍，分片被推到约 25 小时开外。

另一个真实问题是源片会在清单中间向后突跳（某集第 9 片从 30.9s 跳到
211.5s，站点把两段片源硬拼），播放器只能卡住丢帧；现在超过
`_REMUX_MAX_FORWARD_JUMP` 的跳跃会被拉直。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import framework.media_proxy as mp  # noqa: E402

HZ = 90000


def _make_ts(start_seconds: float, frames: int = 6, step: float = 0.04,
             pid: int = 256) -> bytes:
    """构造最小合法 TS：每包都是 PES 起始包（含 PTS+DTS）。

    字节布局按 ISO 13818-1：0x47 | TEI/PUSI/priority | PID(5+8) | AFC/CC |
    (adaptation_length, adapt_flags) | PES start code + flags + PTS/DTS。
    `_read_ts_timestamps` 要求 PUSI 置位、PES flags 高两位为 0xC0（PTS+DTS）。
    """
    packets = []
    for i in range(frames):
        ticks = int(round((start_seconds + i * step) * HZ)) & 0x1FFFFFFFF

        def enc5(v: int, prefix: int) -> bytes:
            return bytes([
                prefix | ((v >> 29) & 0x0E),
                (v >> 22) & 0xFF,
                0x01 | ((v >> 14) & 0xFE),
                (v >> 7) & 0xFF,
                0x01 | ((v << 1) & 0xFE),
            ])

        pes = (b"\x00\x00\x01\xe0"          # start code
               + b"\x00\x00"                 # packet length
               + b"\x80\xc0\x0a"             # flags: PTS+DTS，header len 10
               + enc5(ticks, 0x31)           # PTS
               + enc5(ticks, 0x11))          # DTS
        pkt = bytearray()
        pkt.append(0x47)
        pkt.append(0x40)                     # PUSI，无 adaptation
        pkt.append((pid >> 8) & 0x1F)
        pkt.append(pid & 0xFF)
        pkt += pes
        pkt += b"\xff" * (188 - len(pkt))    # 填充
        packets.append(bytes(pkt))
    return b"".join(packets)


def _first_dts(buf: bytes) -> float:
    for off in range(0, len(buf) - 188 + 1, 188):
        if buf[off] != 0x47:
            continue
        if (buf[off + 1] & 0x1F) == 0x1F:
            continue
        offs = mp._read_ts_timestamps(bytearray(buf), off, 188)
        if offs:
            return mp._decode_ts5(bytearray(buf), offs[-1]) / HZ
    raise AssertionError("no timestamp")


def test_shift_anchor_is_ticks_not_seconds():
    """传 ticks 的 base，输出首包 DTS 必须落在该 ticks 上（不是乘 90000）。"""
    seg = _make_ts(1.0)
    base_seconds = 5.24
    res = mp._ts_shift_bytes(seg, base_seconds * HZ)
    assert res is not None
    out, duration = res
    assert abs(_first_dts(out) - base_seconds) < 0.001, _first_dts(out)
    assert duration > 0


def test_shift_returns_none_when_already_continuous():
    """本片起点已不早于锚点（且不构成大跳）→ 不动。"""
    seg = _make_ts(10.0)
    assert mp._ts_shift_bytes(seg, (10.0 - 0.04) * HZ) is None


def test_shift_pulls_back_large_forward_jump():
    """源片向后突跳（30.9s → 211.5s）要拉直，否则播放器卡住丢帧。"""
    seg = _make_ts(211.48)
    res = mp._ts_shift_bytes(seg, 31.28 * HZ)
    assert res is not None, "大跳必须被拉直"
    out, _ = res
    assert abs(_first_dts(out) - 31.28) < 0.001, _first_dts(out)


def test_remux_ctx_stores_ticks():
    """ctx["segs"] 必须是 90kHz ticks（存秒会让 base 恒为 0）。"""
    proxy = mp.MediaProxy.instance()
    key = "unit-test-ticks"
    mp._REMUX_CTX.pop(key, None)
    seg0 = _make_ts(3.0, frames=8)
    proxy._remux_segment_locked(key, "0.ts", seg0, None)
    ctx = mp._remux_ctx(key)
    value = ctx["segs"]["0.ts"]
    # 结束点约 3.0 + 8*0.04 ≈ 3.28s，用 ticks 表示应 > 100000
    assert value > 100000, f"ctx 存了秒而不是 ticks：{value}"


def test_remux_chain_produces_monotonic_dts():
    """连续三片经整理后，跨片 DTS 必须单调递增（不重叠、不倒退）。

    夹具里三片的原始起点是 10.0 / 12.0 / 14.0（片长 0.2s，源侧本身就有
    缺口）；整理后**不允许倒退**，也不允许出现比源缺口更大的空白。
    """
    proxy = mp.MediaProxy.instance()
    key = "unit-test-chain"
    mp._REMUX_CTX.pop(key, None)
    outs = []
    for i in range(3):
        seg = _make_ts(10.0 + i * 2.0)
        res = proxy._remux_segment_locked(key, f"{i}.ts", seg, None)
        outs.append(res if res is not None else seg)

    bounds = []
    for buf in outs:
        vals = []
        for off in range(0, len(buf) - 188 + 1, 188):
            if buf[off] != 0x47:
                continue
            if (buf[off + 1] & 0x1F) == 0x1F:
                continue
            offs = mp._read_ts_timestamps(bytearray(buf), off, 188)
            if offs:
                vals.append(mp._decode_ts5(bytearray(buf), offs[-1]) / HZ)
        bounds.append((min(vals), max(vals)))

    assert bounds[0][0] < 10.5
    for (prev_lo, prev_hi), (lo, hi) in zip(bounds, bounds[1:]):
        assert lo >= prev_hi - 0.05, f"跨片 DTS 倒退：{prev_hi:.3f} -> {lo:.3f}"
        # 源缺口 2.0s - 片长 0.2s ≈ 1.8s：整理后不能比它更大
        assert lo - prev_hi < 2.0, f"跨片缺口被放大：{prev_hi:.3f} -> {lo:.3f}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
