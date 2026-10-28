"""一次性排查脚本：把昨天真实播放过的清单丢给广告引擎，看会剔掉几片。

目的：回答「 ikanpp 的过滤规则有没有作用到别的源」——用真实清单验证。
看完即删。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.adblock import AdblockEngine  # noqa: E402

CACHE = Path(r"D:\code\claw\data\cache\video")


def summarize(text: str) -> str:
    segs = [ln for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")]
    disc = sum(1 for ln in text.splitlines()
               if ln.startswith("#EXT-X-DISCONTINUITY"))
    return f"{len(segs)}片/{disc}边界"


def main() -> int:
    rows = []
    for pl in sorted(CACHE.glob("*/playlist.m3u8"),
                     key=lambda p: p.stat().st_mtime, reverse=True)[:8]:
        text = pl.read_text(encoding="utf-8", errors="replace")
        if "EXT-X-STREAM-INF" in text or "EXTINF" not in text:
            continue
        hosts = {re.sub(r"^https?://([^/]+).*", r"\1", ln.strip())
                 for ln in text.splitlines()
                 if ln.strip().startswith("http")}
        host = next(iter(hosts)) if len(hosts) == 1 else "relative/multi"
        # 1) 通用引擎（源未声明 ad_block 时代理用的就是这套）
        generic = AdblockEngine(default_on=True)
        ad_idx, _ = generic.detect_m3u8_ads(text, "https://upstream/x/")
        # 2) 显式 ikanpp 规则引擎（模拟「规则被加到全局」的最坏情况）
        ikan = AdblockEngine()
        ikan.configure(
            type("S", (), {"raw": {"ad_block": {
                "enabled": True,
                "hls_rules": {"drop_short_blocks": {
                    "enabled": True, "max_duration": 30,
                    "max_ratio": 0.05, "require_boundary": True}},
            }}})(),
            default_on=True,
        )
        ik_idx, _ = ikan.detect_m3u8_ads(text, "https://upstream/x/")
        rows.append((pl.parent.name[:10], host[:38], summarize(text),
                     len(ad_idx), len(ik_idx)))
    print(f"{'cache':10} {'host':38} {'structure':16} {'通用':>4} {'ikanpp规则':>10}")
    for r in rows:
        print(f"{r[0]:10} {r[1]:38} {r[2]:16} {r[3]:>4} {r[4]:>10}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
