# -*- coding: utf-8 -*-
"""音画同步探针：直接读缓存里的 TS 分片，逐片算音/视频时间轴与偏移。

用法：
    python tools/_probe_av_sync.py <缓存目录>            # 逐片表 + 汇总
    python tools/_probe_av_sync.py <缓存目录> --table    # 只打异常片

判定口径（与 PROJECT_GUIDE §6.9.2 一致）：
- `a-v`：同片内 音频首 DTS − 视频首 DTS（秒）。恒定 = 音画同步；
  逐片漂移 = 播放侧每片都要重新对表，听感就是"越播越不同步"。
- 片间连续性：本片首 DTS 应 ≈ 前片末 DTS + 一帧；跳变会让播放器丢帧/丢音。
- PCR 与 PTS 的差：PCR 被单独平移会让播放器时钟回跳。
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
from pathlib import Path

HZ = 90000.0
STEP = 188


def _pes_offset(data, off, step=STEP):
    afc = (data[off + 3] >> 4) & 0x03
    p = off + 4
    if afc in (2, 3):
        p += 1 + data[p]
        if p >= off + step:
            return None
    return p


def _ts5(data, p):
    return (((data[p] >> 1) & 0x07) << 30) | (data[p + 1] << 22) | \
           (((data[p + 2] >> 1) & 0x7F) << 15) | (data[p + 3] << 7) | (data[p + 4] >> 1)


def _pcr_offset(data, off):
    if ((data[off + 3] >> 4) & 0x03) not in (2, 3):
        return None
    af_len = data[off + 4]
    if af_len <= 0:
        return None
    p = off + 5
    if p >= off + STEP or not (data[p] & 0x10):
        return None
    q = p + 1
    return q if q + 6 <= off + STEP and q + 6 <= p + af_len else None


def _pcr_value(data, p):
    base = ((data[p] << 25) | (data[p + 1] << 17) | (data[p + 2] << 9)
            | (data[p + 3] << 1) | (data[p + 4] >> 7))
    return base


def scan(path: Path):
    """返回 {'v': [(dts,pts)], 'a': [...], 'pcr': [(idx,value)], 'cc': {pid: [jumps]}}"""
    data = path.read_bytes()
    if not data or data[0] != 0x47:
        return {"kind": "not-ts", "bytes": len(data), "head": data[:8].hex()}
    out = {"kind": "ts", "v": [], "a": [], "pcr": [], "streams": {}, "cc_bad": {}}
    last_cc = {}
    off = 0
    n = 0
    while off + STEP <= len(data):
        if data[off] != 0x47:
            out["kind"] = "desync-bytes@%d" % off
            break
        pid = ((data[off + 1] & 0x1F) << 8) | data[off + 2]
        afc = (data[off + 3] >> 4) & 0x03
        has_payload = afc in (1, 3)
        cc = data[off + 3] & 0x0F
        if has_payload:
            prev = last_cc.get(pid)
            if prev is not None and cc != ((prev + 1) & 0x0F):
                out["cc_bad"][pid] = out["cc_bad"].get(pid, 0) + 1
            last_cc[pid] = cc
        q = _pcr_offset(data, off)
        if q is not None:
            out["pcr"].append((n, _pcr_value(data, q)))
        if pid != 0x1FFF and (data[off + 1] & 0x40):
            p = _pes_offset(data, off)
            if p is not None and p + 14 < off + STEP:
                sid = data[p + 3] if p + 3 < off + STEP else -1
                h = data[p + 7] & 0xC0
                ts = []
                if h == 0x80:
                    ts = [("pts", _ts5(data, p + 9))]
                elif h == 0xC0:
                    ts = [("pts", _ts5(data, p + 9)), ("dts", _ts5(data, p + 14))]
                if ts:
                    kind = "v" if 0xE0 <= sid <= 0xEF else ("a" if 0xC0 <= sid <= 0xDF else "o")
                    entry = (pid, sid, dict(ts))
                    out["streams"].setdefault(kind, []).append(entry)
        off += STEP
        n += 1
    return out


def summarize(path: Path):
    info = scan(path)
    if info["kind"] != "ts":
        return info
    res = {"name": path.name, "bytes": len(path.read_bytes())}
    for kind in ("v", "a"):
        entries = info["streams"].get(kind) or []
        dts = [e[2].get("dts", e[2].get("pts")) for e in entries]
        pts = [e[2].get("pts") for e in entries]
        if dts:
            res[kind] = {
                "packets": len(entries),
                "first_dts": min(dts) / HZ,
                "last_dts": max(dts) / HZ,
                "first_pts": min(p for p in pts if p is not None) / HZ,
                "span": (max(dts) - min(dts)) / HZ,
                "pids": sorted({e[0] for e in entries}),
            }
    if res.get("a") and res.get("v"):
        res["a_v_offset"] = res["a"]["first_dts"] - res["v"]["first_dts"]
        res["span_diff"] = res["a"]["span"] - res["v"]["span"]
    if info["pcr"]:
        res["pcr"] = {
            "count": len(info["pcr"]),
            "first": info["pcr"][0][1] / HZ,
            "last": info["pcr"][-1][1] / HZ,
        }
    res["cc_bad"] = info["cc_bad"]
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--table", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    root = Path(args.dir)
    segs = sorted(root.glob("*.ts"), key=lambda p: p.name)
    if args.limit:
        segs = segs[: args.limit]
    rows = []
    for seg in segs:
        rows.append(summarize(seg))
    offsets = [r["a_v_offset"] for r in rows if "a_v_offset" in r]
    print("分片 %d 片，带音视频偏移 %d 片" % (len(rows), len(offsets)))
    if offsets:
        print("a-v 偏移：min %.3fs max %.3fs 中位 %.3fs 抖动 %.3fs"
              % (min(offsets), max(offsets), statistics.median(offsets),
                 max(offsets) - min(offsets)))
    if rows and not args.table:
        print("%-12s %8s %7s %7s %8s %8s %8s %6s" % (
            "seg", "bytes", "v_span", "a_span", "a-v", "v_first", "a_first", "cc"))
        for r in rows[: min(len(rows), 25)]:
            print("%-12s %8d %7.2f %7.2f %8.3f %8.2f %8.2f %6s" % (
                r["name"], r["bytes"],
                r.get("v", {}).get("span", -1), r.get("a", {}).get("span", -1),
                r.get("a_v_offset", float("nan")),
                r.get("v", {}).get("first_dts", -1), r.get("a", {}).get("first_dts", -1),
                sum(r.get("cc_bad", {}).values())))
    # 片间连续性
    prev = None
    jumps = []
    for r in rows:
        v = r.get("v")
        if not v:
            continue
        if prev is not None:
            gap = v["first_dts"] - prev
            if abs(gap) > 0.05:
                jumps.append((r["name"], prev, v["first_dts"], gap))
        prev = v["last_dts"]
    print("视频首 DTS 片间跳变（|Δ|>50ms）%d 处" % len(jumps))
    for name, prev_end, cur, gap in jumps[:20]:
        print("  %s: 前片末 %.3f → 本片首 %.3f (Δ %+.3f)" % (name, prev_end, cur, gap))
    bad_cc = [(r["name"], r["cc_bad"]) for r in rows if r.get("cc_bad")]
    print("TS 连续计数器异常分片 %d 片" % len(bad_cc))
    for name, cc in bad_cc[:10]:
        print("  %s: %s" % (name, cc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
