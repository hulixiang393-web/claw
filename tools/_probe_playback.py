"""播放链路自检脚本（可保留，非产品代码）。

两个子命令，都是「拿真实运行痕迹判断卡顿成因」用的，不联网也可跑一部分：

    python tools/_probe_playback.py delivery    # 分析最近播放的分片交付速率
    python tools/_probe_playback.py prefetch    # 自检预取预算/熔断规则

`delivery` 读 data/cache/video 下真实落盘的分片与 playlist，算出
「每墙钟秒拿到多少秒内容」（<1 必然卡顿）；`prefetch` 用假上游把
_prefetch_one 的三条分支跑一遍：慢但在动不熔断、零字节熔断、截断熔断。
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CACHE = ROOT / "data" / "cache" / "video"


# --------------------------------------------------------------------------- #
# 1. 交付速率（真实缓存）
# --------------------------------------------------------------------------- #
def _parse(text: str):
    segs, dur, disc = [], None, 0
    for line in text.splitlines():
        if line.startswith("#EXT-X-DISCONTINUITY"):
            disc += 1
        elif line.startswith("#EXTINF:"):
            m = re.search(r"#EXTINF:\s*([\d.]+)", line)
            dur = float(m.group(1)) if m else 0.0
        elif line.strip() and not line.startswith("#"):
            name = line.strip().split("?", 1)[0].rsplit("/", 1)[-1]
            segs.append((dur or 0.0, name))
            dur = None
    return segs, disc


def delivery(limit: int = 5) -> int:
    shown = 0
    dirs = sorted((d for d in CACHE.iterdir() if d.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for d in dirs:
        pl = d / "playlist.m3u8"
        if not pl.exists():
            continue
        segs, disc = _parse(pl.read_text(encoding="utf-8", errors="replace"))
        if not segs:
            continue
        files = {p.name: p for p in d.glob("*.ts")}
        matched = [(dur, files[name]) for dur, name in segs if name in files]
        if len(matched) < 3:
            continue
        matched.sort(key=lambda t: t[1].stat().st_mtime)
        span = matched[-1][1].stat().st_mtime - matched[0][1].stat().st_mtime
        content = sum(dur for dur, _ in matched)
        slow = 0
        prev = None
        for dur, f in matched:
            t = f.stat().st_mtime
            if prev is not None and (t - prev) > dur:
                slow += 1
            prev = t
        total_dur = sum(dur for dur, _ in segs)
        print(f"\n== {d.name[:10]}  {len(segs)} 片 / {total_dur:.0f}s / "
              f"边界 {disc} / 已落盘 {len(files)}")
        print(f"   内容 {content:.0f}s ÷ 墙钟 {span:.0f}s = "
              f"{content / span if span else 0:.2f}× 实时")
        print(f"   交付慢于该片时长的次数 {slow}/{max(len(matched) - 1, 1)}")
        shown += 1
        if shown >= limit:
            break
    if not shown:
        print("没有可分析的缓存（playlist 与分片都要在）")
    return 0


# --------------------------------------------------------------------------- #
# 2. 预取预算 / 熔断规则自检
# --------------------------------------------------------------------------- #
def _fake_response(total: int, raw):
    class _Resp:
        status_code = 200
        headers = {"Content-Length": str(total)}
        closed = False

        def __init__(self):
            self.raw = raw

        def close(self):
            self.closed = True

    return _Resp()


class _ProgressTimeout:
    def __init__(self):
        self.reads = 0

    def read(self, _size):
        self.reads += 1
        if self.reads == 1:
            return b"x" * 4096
        import requests

        raise requests.Timeout("slow but alive")


class _NoBytes:
    def read(self, _size):
        import requests

        raise requests.Timeout("no bytes")


class _Truncated:
    def __init__(self):
        self.reads = 0

    def read(self, _size):
        self.reads += 1
        return b"x" * 4096 if self.reads == 1 else b""


def prefetch() -> int:
    import framework.media_proxy as mp
    from framework.media_cache import MediaCache

    root = Path(tempfile.gettempdir()) / "claw_pf_verify"
    root.mkdir(parents=True, exist_ok=True)
    failures = []

    def _run(raw_factory, total, times):
        cache = MediaCache(root=root / f"c{len(failures)}", enabled=True)
        proxy = mp.MediaProxy(cache=cache, prefetch={"enabled": True,
                                                    "depth": 1, "workers": 1})
        orig = mp._fetch_upstream
        mp._fetch_upstream = lambda *a, **k: _fake_response(
            total, raw_factory())
        try:
            for _ in range(times):
                proxy._prefetch_one("k", "http://cdn.test/s.ts", {}, False)
            return proxy._pf_failures, proxy._pf_disabled
        finally:
            mp._fetch_upstream = orig
            proxy.stop()

    cases = [
        ("慢但在动（超时+已写字节）×5 不应熔断", _ProgressTimeout,
         100 * 1024, 5, False),
        ("零字节超时 ×3 应熔断", _NoBytes, 100 * 1024, 3, True),
        ("分片体被截断 ×3 应熔断", _Truncated, 100 * 1024, 3, True),
    ]
    for name, factory, total, times, expect_disabled in cases:
        fails, disabled = _run(factory, total, times)
        ok = disabled is expect_disabled
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: "
              f"failures={fails} disabled={disabled}")
        if not ok:
            failures.append(name)

    budget = [(None, mp._PREFETCH_TIMEOUT),
              (2500 * 1024, 88.0),
              (50 * 1024 * 1024, mp._PREFETCH_MAX_TIMEOUT)]
    for size, expect in budget:
        got = mp._prefetch_budget(size)
        ok = abs(got - expect) <= 6.0
        print(f"[{'PASS' if ok else 'FAIL'}] 预算 {size} → {got:.1f}s "
              f"(期望≈{expect})")
        if not ok:
            failures.append(f"budget {size}")
    return 1 if failures else 0


def _per_pid_min_pts(data: bytes) -> dict:
    """按 PID 取最小 PTS/PCR（秒），用来验证平移是否对每条流一致。"""
    import framework.media_proxy as mp

    step = 188
    out: dict = {}
    off = 0
    while off + step <= len(data):
        if data[off] != 0x47:
            break
        pid = ((data[off + 1] & 0x1F) << 8) | data[off + 2]
        if (data[off + 1] & 0x1F) == 0x1F:
            q = mp._read_ts_pcr_offset(data, off, step)
            if q is not None:
                base = ((data[q] << 25) | (data[q + 1] << 17) | (data[q + 2] << 9)
                        | (data[q + 3] << 1) | (data[q + 4] >> 7))
                ext = ((data[q + 4] & 1) << 8) | data[q + 5]
                val = base / 90000.0 + ext / 27000000.0
                key = "PCR"
                if key not in out or val < out[key]:
                    out[key] = val
        else:
            for p in mp._read_ts_timestamps(data, off, step):
                val = mp._decode_ts5(data, p) / 90000.0
                key = f"pid{pid:04x}"
                if key not in out or val < out[key]:
                    out[key] = val
        off += step
    return out


def remux() -> int:
    """对真实缓存分片跑一次纯字节平移，验证 A/V 各流位移完全一致。

    判断依据：平移后每个 PID 的最小 PTS（以及 PCR）增量必须**同一个数**；
    只要音频流和视频流的增量不同，就会出现用户报的"声音断断续续"。
    """
    import framework.media_proxy as mp

    dirs = sorted((d for d in CACHE.iterdir() if d.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    checked = 0
    for d in dirs:
        segs = [p for p in d.glob("*.ts")
                if not (p.with_name(p.name + ".remuxed")).exists()]
        segs.sort(key=lambda p: p.stat().st_mtime)
        for seg in segs[:2]:
            raw = seg.read_bytes()
            if not raw or raw[0] != 0x47:
                continue
            before = _per_pid_min_pts(raw)
            out = mp._ts_shift_bytes(raw, 100.0)
            print(f"\n== {d.name[:10]}/{seg.name}  {len(raw)/1024:.0f}KB  "
                  f"流={sorted(before)}")
            for k, v in sorted(before.items()):
                print(f"   平移前 {k:8} min={v:9.3f}")
            if out is None:
                print("   → 未平移（起点已在锚点之后，属正常直通）")
                checked += 1
                continue
            shifted, duration = out
            after = _per_pid_min_pts(shifted)
            deltas = {}
            for k in sorted(before):
                if k in after:
                    deltas[k] = round(after[k] - before[k], 6)
            uniq = set(deltas.values())
            ok = len(uniq) == 1
            print(f"   → 时长 {duration:.3f}s，各流位移 {deltas} "
                  f"{'一致 ✔' if ok else '不一致 ✘'}")
            checked += 1
    if not checked:
        print("没有可比对的分片（都在 .remuxed 标记之后）")
    return 0


def main(argv) -> int:
    cmd = argv[1] if len(argv) > 1 else "delivery"
    if cmd == "delivery":
        return delivery()
    if cmd == "prefetch":
        return prefetch()
    if cmd == "remux":
        return remux()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
