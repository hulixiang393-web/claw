"""一次性排查脚本：把真实播放过的 HLS 清单和落盘分片的到达时间对齐。

目的：算「每墙钟秒拿到多少秒内容」（>1 才够播）。<1 就是带宽/交付不足，
必然卡顿；远大于 1 却仍卡，则问题在解码/播放器侧。看完即删。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CACHE = Path(r"D:\code\claw\data\cache\video")


def parse(text: str):
    """返回 [(dur, name, key_method)] 与边界数。"""
    segs = []
    dur = None
    key = "NONE"
    disc = 0
    tags = []
    for line in text.splitlines():
        if line.startswith("#EXT-X-KEY:"):
            m = re.search(r"METHOD=([^,\s]+)", line)
            key = m.group(1) if m else "?"
        elif line.startswith("#EXT-X-DISCONTINUITY"):
            disc += 1
        elif line.startswith("#EXT-X-MAP"):
            tags.append("MAP")
        elif line.startswith("#EXT-X-ENDLIST"):
            tags.append("ENDLIST")
        elif line.startswith("#EXTINF:"):
            m = re.search(r"#EXTINF:\s*([\d.]+)", line)
            dur = float(m.group(1)) if m else 0.0
        elif line.strip() and not line.startswith("#"):
            name = line.strip().split("?", 1)[0].rsplit("/", 1)[-1]
            segs.append((dur or 0.0, name, key))
            dur = None
    return segs, disc, tags


def main() -> int:
    dirs = [d for d in CACHE.iterdir() if d.is_dir()]
    for d in sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True):
        pl = d / "playlist.m3u8"
        if not pl.exists():
            continue
        text = pl.read_text(encoding="utf-8", errors="replace")
        if "EXTINF" not in text:
            continue
        segs, disc, tags = parse(text)
        files = {p.name: p for p in d.glob("*.ts")}
        matched = [(dur, files[name]) for dur, name, _ in segs if name in files]
        if len(matched) < 3:
            continue
        matched.sort(key=lambda t: t[1].stat().st_mtime)
        t0 = matched[0][1].stat().st_mtime
        t1 = matched[-1][1].stat().st_mtime
        content = sum(d for d, _ in matched)
        wall = max(t1 - t0, 1e-6)
        keys = {k for _, _, k in segs}
        durs = [d for d, _, _ in segs]
        print(f"\n== {d.name[:10]}  {pl.stat().st_mtime_ns and ''}"
              f"{pl.stat().st_mtime:.0f}")
        print(f"   清单 {len(segs)} 片 / {sum(durs):.1f}s / 边界 {disc} / "
              f"KEY {keys} / {tags}")
        print(f"   段时长 min={min(durs):.2f} p50={sorted(durs)[len(durs)//2]:.2f} "
              f"max={max(durs):.2f}")
        print(f"   已落盘 {len(files)} 片，时序对齐 {len(matched)} 片："
              f"内容 {content:.0f}s / 墙钟 {wall:.0f}s = "
              f"{content/wall:.2f}×")
        gaps = []
        prev = None
        for dur, f in matched:
            t = f.stat().st_mtime
            if prev is not None:
                gaps.append((round(t - prev, 1), dur, round(f.stat().st_size / 1024)))
            prev = t
        worst = sorted(gaps, key=lambda g: g[0] - g[1], reverse=True)[:6]
        print("   最拖的 6 次交付 (间隔s, 该段时长s, KB):")
        for g in worst:
            print(f"      gap={g[0]:5.1f}  dur={g[1]:4.1f}  size={g[2]:6d}KB  "
                  f"速率≈{g[2]/max(g[0],.1):5.0f}KB/s")
        fast = sum(1 for g in gaps if g[0] > g[1])
        print(f"   交付慢于该段时长的次数 {fast}/{len(gaps)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
