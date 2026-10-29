# -*- coding: utf-8 -*-
"""分片时间戳整理必须**按播放清单顺序**推进（「频繁跳帧」根因回归）。

现场（用户反馈）：播放时频繁跳帧，要求「所有播放片段都能按视频源原有的
播放顺序播放」。

根因：`_remux_segment_locked` 取 `base = max(ctx["segs"].values())` 当锚点。
而分片并不保证按序到达——并行预取（omofuna 深度 8 / 并发 3）、拖动 seek、
播放器并行请求都会让靠后的分片先被整理。max 于是取到「未来分片」的结束点，
先到的靠前分片被整体平移到未来分片之后：**清单顺序没变，时间轴却乱了**，
播放器只能靠丢帧追上。

修法：基准取「清单里的前一片」的结束点；前一片尚未整理（真乱序）就让本片
按源站原时间轴直通，绝不凭空造基准。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from framework import media_proxy  # noqa: E402
from framework.media_proxy import MediaProxy  # noqa: E402

HZ = 90000.0


def _proxy(order):
    """只带分片顺序表的代理实例（不启动本地服务器、不建缓存）。"""
    proxy = object.__new__(MediaProxy)
    proxy._seg_order = {"k": list(order)}
    proxy._seg_pos = {"k": {u: i for i, u in enumerate(order)}}
    return proxy


def _ctx(key):
    ctx = media_proxy._remux_ctx(key)
    with ctx["lock"]:
        ctx["segs"].clear()
        ctx["processed"].clear()
        ctx["inflight"].clear()
        ctx["last_end"] = None
    return ctx


ORDER = ["u0", "u1", "u2", "u3", "u4", "u5"]


def test_base_is_playlist_predecessor_not_max():
    """基准必须取清单前一片，而不是已处理集合里的最大结束点。"""
    proxy = _proxy(ORDER)
    ctx = _ctx("order-base-1")
    with ctx["lock"]:
        ctx["segs"].update({"u1": 10 * HZ, "u5": 500 * HZ})

    base = proxy._remux_base_for(ctx, "k", "u2")

    assert base == 10 * HZ          # 旧实现会返回 500*HZ（未来分片）


def test_no_base_for_first_segment_of_session():
    """会话第一片没有任何基准 → 原样直通（由它自己当整条链的起点）。"""
    proxy = _proxy(ORDER)
    ctx = _ctx("order-base-2")
    with ctx["lock"]:
        ctx["segs"]["u5"] = 500 * HZ
    assert proxy._remux_base_for(ctx, "k", "u0") is None


def test_missing_predecessor_falls_back_to_session_tail():
    """关键回归（ts 乱跳）：前一片查不到时，必须退回「本会话已整理到的末尾」，
    绝不能返回 None 让本片用源站原始时间轴——那会和已被平移的邻居混用两套
    时间轴，播放器就一会儿播这里一会儿播那里。
    """
    proxy = _proxy(ORDER)
    ctx = _ctx("order-base-3")
    with ctx["lock"]:
        ctx["segs"]["u5"] = 500 * HZ
        ctx["last_end"] = 42 * HZ

    assert proxy._remux_base_for(ctx, "k", "u2") == 42 * HZ


def test_missing_order_falls_back_to_session_tail():
    proxy = _proxy(ORDER)
    ctx = _ctx("order-base-5")
    with ctx["lock"]:
        ctx["last_end"] = 7 * HZ
    assert proxy._remux_base_for(ctx, "unknown-key", "u2") == 7 * HZ


RAW_TS = b"\x47" + bytes(187) * 2


def test_late_segment_not_shifted_onto_future_base():
    """未来分片先到时，靠前分片不得被平移（不得调用整理）。"""
    proxy = _proxy(ORDER)
    key = "k"
    ctx = _ctx(key)
    with ctx["lock"]:
        ctx["segs"]["u5"] = 500 * HZ
    bases = []
    proxy._run_remux = lambda ff, raw, base, hdrs=None: bases.append(base)

    out = proxy._remux_segment_locked(key, "u2", RAW_TS, None)

    assert out is None
    assert bases == []               # 旧实现会带上 500*HZ 去整理


def test_in_order_segment_uses_predecessor_end():
    proxy = _proxy(ORDER)
    key = "k"
    ctx = _ctx(key)
    with ctx["lock"]:
        ctx["segs"]["u1"] = 10 * HZ
    bases = []
    proxy._run_remux = lambda ff, raw, base, hdrs=None: bases.append(base)

    proxy._remux_segment_locked(key, "u2", RAW_TS, None)

    assert bases == [10 * HZ]


def test_session_tail_advances_with_shifted_segment():
    proxy = _proxy(ORDER)
    key = "k"
    ctx = _ctx(key)
    with ctx["lock"]:
        ctx["segs"]["u1"] = 10 * HZ
    proxy._run_remux = lambda ff, raw, base, hdrs=None: (b"out", 4.0)

    proxy._remux_segment_locked(key, "u2", RAW_TS, None)

    with ctx["lock"]:
        assert ctx["last_end"] == (10 + 4) * HZ


def test_unknown_segment_never_passes_through_raw():
    """乱跳回归：位置查不到的分片也必须接在本会话末尾，不得原样直通。"""
    proxy = _proxy(ORDER)
    key = "k"
    ctx = _ctx(key)
    with ctx["lock"]:
        ctx["last_end"] = 14 * HZ
    bases = []
    proxy._run_remux = lambda ff, raw, base, hdrs=None: bases.append(base)

    # 带 query 的分片名在顺序表里查不到（真实场景：/c/<key>/seg.ts%3Ftoken=…）
    proxy._remux_segment_locked(key, "seg.ts?token=abc", RAW_TS, None)

    assert bases == [14 * HZ]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
