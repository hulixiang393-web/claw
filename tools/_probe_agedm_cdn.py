"""一次性排查：解析 agedm 播放页 → iframe → m3u8，实测该 CDN 的交付能力。

目的：确定「3000k 流 vs 单/多连接吞吐」的真实倍数，给预取/缓冲参数定标。
看完即删。
"""
from __future__ import annotations

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PLAY = "https://www.agedm.io/play/20250158/2/1"
H = {"User-Agent": UA, "Referer": "https://www.agedm.io/",
     "Accept-Language": "zh-CN,zh;q=0.9"}


def get(url, headers=None, timeout=25):
    r = requests.get(url, headers=headers or H, timeout=timeout)
    r.raise_for_status()
    return r


def parse_media(text):
    out, dur = [], None
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            m = re.search(r"#EXTINF:\s*([\d.]+)", line)
            dur = float(m.group(1)) if m else 0.0
        elif line.strip() and not line.startswith("#"):
            out.append((dur or 0.0, line.strip()))
            dur = None
    return out


def main() -> int:
    page = get(PLAY).text
    print(f"play page {len(page)}B")
    for m in re.finditer(r'<iframe[^>]+>', page, re.I):
        print("  iframe:", m.group(0)[:200])
    srcs = re.findall(r'iframe[^>]*src=["\']([^"\']+)', page, re.I)
    print("  iframe srcs:", srcs[:5])
    script_urls = [u for u in srcs if u]
    if not script_urls:
        print(page[:1200])
        return 1
    frame = urljoin(PLAY, script_urls[0])
    print("  frame:", frame[:140])
    fh = dict(H)
    fh["Referer"] = PLAY
    ftxt = get(frame, fh).text
    print(f"  frame {len(ftxt)}B")
    cands = re.findall(r'https?://[^\s"\']+?\.m3u8[^\s"\']*', ftxt)
    if not cands:
        for pat in (r'src\s*[:=]\s*["\']([^"\']+m3u8[^"\']*)',
                    r'url\s*[:=]\s*["\']([^"\']+?\.m3u8[^"\']*)'):
            cands += re.findall(pat, ftxt, re.I)
    print("  m3u8 candidates:", [c[:130] for c in cands[:4]])
    if not cands:
        print(ftxt[:1500])
        return 1
    m3u8 = urljoin(frame, cands[0])
    vh = dict(H)
    vh["Referer"] = frame
    master = get(m3u8, vh).text
    print(f"  master {len(master)}B")
    variants = [ln.strip() for ln in master.splitlines()
                if ln.strip() and not ln.startswith("#")]
    if variants and "EXTINF" not in master:
        for v in variants:
            print("   variant:", v[:130])
        vurl = urljoin(m3u8, variants[-1])
        media = get(vurl, vh).text
    else:
        vurl, media = m3u8, master
    segs = parse_media(media)
    total = sum(d for d, _ in segs)
    print(f"  media {len(segs)} 片 {total:.1f}s")
    sample = segs[:6]
    urls = [urljoin(vurl, u) for _, u in sample]
    durs = [d for d, _ in sample]
    sess = requests.Session()
    t0 = time.monotonic()
    sizes = []
    for u in urls:
        r = sess.get(u, headers=vh, timeout=30)
        r.raise_for_status()
        sizes.append(len(r.content))
    dt = max(time.monotonic() - t0, 1e-6)
    need = sum(sizes) * 8 / sum(durs) / 1000 if sum(durs) else 0
    print(f"  串行 {len(urls)} 片 {sum(sizes)/1048576:.2f}MB {dt:.1f}s → "
          f"{sum(sizes)/dt/1024:.0f}KB/s；该段平均码率 {need:.0f}kbps")
    if sum(durs):
        print(f"  串行 ≈ {(sum(sizes)*8/dt/1000)/need:.2f}× 实时")
    par = [requests.Session() for _ in range(4)]
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        got = list(pool.map(lambda iu: len(par[iu[0] % 4].get(
            iu[1], headers=vh, timeout=30).content), list(enumerate(urls))))
    dt4 = max(time.monotonic() - t0, 1e-6)
    print(f"  4并发 {len(urls)} 片 {sum(got)/1048576:.2f}MB {dt4:.1f}s → "
          f"{sum(got)/dt4/1024:.0f}KB/s")
    if need:
        print(f"  4并发 ≈ {(sum(got)*8/dt4/1000)/need:.2f}× 实时")
    return 0


if __name__ == "__main__":
    sys.exit(main())
