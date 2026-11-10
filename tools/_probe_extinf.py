# -*- coding: utf-8 -*-
"""清单时间轴探针：对比 `#EXTINF` 声明时长与缓存分片的真实帧时间轴。

用法：
    python tools/_probe_extinf.py <缓存目录>
    python tools/_probe_extinf.py <缓存目录> --all      # 打印所有片的行，不只前 14 片

为什么需要它（现场，omofuna 2026-11）：
    缓存里 339 片、`#EXTINF` 一律 3.00s，而真实 V 帧跨度是 3.12s（75 帧 @24fps），
    477 片累计少算约 57s —— **清单时间轴比媒体时间轴短约 4%**。这个偏差会
    让时长、进度条和续播定位一起偏（`reading_progress.json` 里记的
    `duration 1372.4 / seconds 1109.4` 就是这么来的），也是 mpv 以音频为主
    时钟长时间播放后"看着看着音画不同步"的背景因素之一。

判定口径（与 PROJECT_GUIDE §6.9.2.1 / §12.7 一致）：
- `a-v`：同片内 音频首 PTS − 视频首 PTS（秒）。恒定 = 片内同步；
  逐片漂移 = 播放侧每片都要重新对表，听感是"越播越不同步"。
- 相邻片真实周期 vs `EXTINF`：两者差得越多，清单时间轴越不可信。
- `PCR-PTS`：PCR 被单独平移会让播放器时钟回跳。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HZ = 90000.0
STEP = 188
ROOT = Path(__file__).resolve().parents[1]


def _ts5(data: bytes, p: int) -> int:
    return (
        (((data[p] >> 1) & 0x07) << 30)
        | (data[p + 1] << 22)
        | (((data[p + 2] >> 1) & 0x7F) << 15)
        | (data[p + 3] << 7)
        | (data[p + 4] >> 1)
    )


def seg_stats(path: str):
    """返回 (视频 PTS 列表, 音频 PTS 列表)。只认 PES 头里带 PTS 的包。"""
    with open(path, "rb") as fh:
        data = fh.read()
    v_pts, a_pts = [], []
    off = 0
    while off + STEP <= len(data):
        if data[off] != 0x47:
            break
        pid = ((data[off + 1] & 0x1F) << 8) | data[off + 2]
        if (data[off + 1] & 0x40) and pid != 0x1FFF:
            afc = (data[off + 3] >> 4) & 3
            p = off + 4
            if afc in (2, 3):
                p += 1 + data[p]
            if p + 14 < off + STEP and data[p] == 0 and data[p + 1] == 0 and data[p + 2] == 1:
                sid = data[p + 3]
                header = data[p + 7] & 0xC0
                if header in (0x80, 0xC0) and p + 13 < off + STEP:
                    pts = _ts5(data, p + 9)
                    if 0xE0 <= sid <= 0xEF:
                        v_pts.append(pts)
                    elif 0xC0 <= sid <= 0xDF:
                        a_pts.append(pts)
        off += STEP
    return v_pts, a_pts


def parse_playlist(path: str):
    items = []
    extinf = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("#EXTINF:"):
                extinf = float(line.split(":", 1)[1].split(",")[0])
            elif line and not line.startswith("#"):
                items.append((line, extinf))
                extinf = None
    return items


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="对比 EXTINF 与真实分片时间轴")
    ap.add_argument("dir", nargs="?",
                    help="缓存目录（含 playlist.m3u8 与分片）；省略时用最新一集")
    ap.add_argument("--all", action="store_true", help="打印所有分片，不只前 14 片")
    args = ap.parse_args(argv)

    cache = Path(args.dir) if args.dir else None
    if cache is None:
        base = ROOT / "data" / "cache" / "video"
        candidates = [p for p in base.iterdir() if (p / "playlist.m3u8").exists()] if base.exists() else []
        if not candidates:
            print("没有可分析的缓存目录（先播一集，或显式传目录）")
            return 2
        cache = max(candidates, key=lambda p: p.stat().st_mtime)
    manifest = cache / "playlist.m3u8"
    if not manifest.exists():
        print(f"{manifest} 不存在")
        return 2

    items = parse_playlist(str(manifest))
    print(f"目录 {cache}")
    print("清单条目 %d，EXTINF 合计 %.1fs" % (len(items), sum(e or 0 for _, e in items)))

    rows = []
    for name, extinf in items:
        path = cache / name
        if not path.exists():
            continue
        v, a = seg_stats(str(path))
        rows.append((
            name, extinf, len(v), len(a),
            (max(v) - min(v)) / HZ if v else 0.0,
            min(v) / HZ if v else 0.0,
            min(a) / HZ if a else 0.0,
        ))

    print("%-14s %7s %5s %5s %7s %10s %9s" % (
        "seg", "EXTINF", "vPkt", "aPkt", "vSpan", "vFirst", "a-v(st)"))
    for r in (rows if args.all else rows[:14]):
        print("%-14s %7.2f %5d %5d %7.2f %10.3f %9.3f" % (
            r[0], r[1] if r[1] is not None else -1, r[2], r[3], r[4], r[5], r[6] - r[5]))

    periods = []
    prev = None
    print("\n相邻片真实周期（按分片编号连续才算）：")
    for r in rows:
        idx, prev_idx = _seg_index(r[0]), _seg_index(prev[0]) if prev else None
        if prev is not None and idx is not None and prev_idx is not None and idx == prev_idx + 1:
            delta = r[5] - prev[5]
            periods.append(delta)
            if len(periods) <= 12:
                print("  %s→%s Δ%.3fs (EXTINF %.2f/%.2f)" % (
                    prev[0], r[0], delta,
                    prev[1] if prev[1] is not None else -1,
                    r[1] if r[1] is not None else -1))
        prev = r

    if periods:
        periods_sorted = sorted(periods)
        declared = sorted(r[1] for r in rows if r[1])
        real_median = periods_sorted[len(periods_sorted) // 2]
        ext_median = declared[len(declared) // 2]
        print("\n周期样本 %d：中位 %.3f 最小 %.3f 最大 %.3f" % (
            len(periods), real_median, periods_sorted[0], periods_sorted[-1]))
        print("清单 EXTINF 中位 %.3f vs 真实周期中位 %.3f（偏差 %+.1f%%）" % (
            ext_median, real_median,
            (ext_median - real_median) / real_median * 100.0 if real_median else 0.0))
        print("按真实周期推算时长 %.1fs vs 清单合计 %.1fs" % (
            sum(periods), sum(r[1] or 0 for r in rows)))
    deviations = [abs(r[6] - r[5]) for r in rows if r[3] and r[2]]
    if deviations:
        print("片内 a-v 偏移：最大 %.3fs，均值 %.3fs（应接近 0）" % (
            max(deviations), sum(deviations) / len(deviations)))
    return 0


def _seg_index(name: str):
    try:
        return int(os.path.splitext(name)[0])
    except ValueError:
        return None


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    raise SystemExit(main())
