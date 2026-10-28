"""一次性排查脚本：实测 ikanpp 指定作品/线路的 HLS 码率与单/多连接吞吐。

目的：判断「播放卡顿」是 CDN 单连接限速（→ 小并发预取有效）还是按 IP
限速（→ 并发无效，只能降码率）。看完即删，不属于产品代码。

用法：
    python tools/_probe_ikanpp_bw.py                  # 首页随机一个作品
    python tools/_probe_ikanpp_bw.py 我和僵尸有个约会
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests

BASE = "https://www.ikanpp.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": BASE + "/",
}


def browse_items() -> list:
    r = requests.get(BASE + "/api/library/browse",
                     params={"page": 1, "type": "tv", "limit": 12},
                     headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data.get("list") or data.get("data") or []


def search_items(keyword: str) -> list:
    """POST /api/search-parallel（SSE）：每源一条 videos 事件。"""
    body = {"text": keyword, "keyword": keyword}
    r = requests.post(BASE + "/api/search-parallel", json=body,
                      headers=HEADERS, timeout=40, stream=True)
    r.raise_for_status()
    items: list = []
    for raw in r.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for key in ("videos", "list", "data"):
            val = ev.get(key)
            if isinstance(val, list):
                for it in val:
                    if isinstance(it, dict):
                        it.setdefault("source", ev.get("source"))
                        items.append(it)
        if isinstance(ev, dict) and ev.get("title"):
            items.append(ev)
    return items


def fetch_detail(item: dict) -> dict:
    body = {"id": str(item.get("id") or item.get("vod_id") or ""),
            "source": item.get("source") or "bfzy",
            "title": item.get("title") or item.get("vod_name") or ""}
    r = requests.post(BASE + "/api/detail", json=body, headers=HEADERS,
                      timeout=30)
    r.raise_for_status()
    return r.json()


def parse_playlist(text: str) -> list:
    out = []
    dur = None
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            m = re.search(r"#EXTINF:\s*([\d.]+)", line)
            dur = float(m.group(1)) if m else 0.0
        elif line.strip() and not line.startswith("#"):
            out.append((dur or 0.0, line.strip()))
            dur = None
    return out


def timed_get(session: requests.Session, url: str) -> int:
    r = session.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return len(r.content)


def measure(base: str, segs: list, tag: str, n: int = 8) -> None:
    sample = segs[:n]
    urls = [urljoin(base, u) for _, u in sample]
    durs = [d for d, _ in sample]
    if not urls:
        return
    sess = requests.Session()
    t0 = time.monotonic()
    sizes = [timed_get(sess, u) for u in urls]
    dt = max(time.monotonic() - t0, 1e-6)
    total = sum(sizes)
    need = total * 8 / sum(durs) / 1000 if sum(durs) else 0.0
    serial_kbps = total * 8 / dt / 1000
    print(f"  [{tag}] 串行 {len(urls)} 片 {total/1048576:.2f}MB {dt:.1f}s → "
          f"{serial_kbps:.0f}kbps（单连接 ≈{total/dt/1024:.0f}KB/s）")
    print(f"  [{tag}] 片段自身平均码率 {need:.0f}kbps（段均 "
          f"{sum(durs)/len(durs):.2f}s）")
    par = [requests.Session() for _ in range(4)]
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        got = list(pool.map(lambda iu: timed_get(par[iu[0] % 4], iu[1]),
                            list(enumerate(urls))))
    dt4 = max(time.monotonic() - t0, 1e-6)
    par_kbps = sum(got) * 8 / dt4 / 1000
    print(f"  [{tag}] 4 并发 {len(urls)} 片 → {par_kbps:.0f}kbps，"
          f"串行/并发={serial_kbps/par_kbps:.2f}")
    if need:
        print(f"  [{tag}] 串行/所需={serial_kbps/need:.2f}×  "
              f"4并发/所需={par_kbps/need:.2f}×")


def probe_episode(url: str, label: str) -> None:
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    text = r.text
    print(f"\n== {label}: {url[:130]}")
    if "EXT-X-STREAM-INF" in text:
        variants = [ln.strip() for ln in text.splitlines()
                    if ln.strip() and not ln.startswith("#")]
        bands = re.findall(r"BANDWIDTH=(\d+)", text)
        print(f"  master：{len(variants)} 个变体 BANDWIDTH={bands}")
        for idx in range(len(variants)):
            vurl = urljoin(url, variants[idx])
            vr = requests.get(vurl, headers=HEADERS, timeout=25)
            vr.raise_for_status()
            vsegs = parse_playlist(vr.text)
            total = sum(d for d, _ in vsegs)
            print(f"  变体{idx}: {len(vsegs)} 片 {total:.1f}s")
            measure(vurl, vsegs, f"v{idx}")
    else:
        segs = parse_playlist(text)
        total = sum(d for d, _ in segs)
        print(f"  media：{len(segs)} 片 {total:.1f}s")
        measure(url, segs, "media")


def main(argv: list) -> int:
    keyword = argv[1] if len(argv) > 1 else ""
    episodes: list = []
    if keyword:
        items = search_items(keyword)
        print(f"search {keyword!r}: {len(items)} 条")
        for it in items[:12]:
            print("   ", {k: it.get(k) for k in ("source", "id", "vod_id",
                                                 "title", "vod_name")
                          if k in it})
        for it in items:
            try:
                detail = fetch_detail(it)
            except Exception as exc:  # noqa: BLE001
                print(f"   detail 失败 {it.get('source')}: {exc}")
                continue
            data = detail.get("data") or {}
            eps = data.get("episodes") or []
            if eps:
                print(f"   -> {it.get('source')} {data.get('vod_name')!r} "
                      f"{len(eps)} 集")
                episodes.append((it.get("source"), eps[0].get("url") or ""))
            if len(episodes) >= 3:
                break
    else:
        items = browse_items()
        for it in items[:6]:
            print("   ", {k: it.get(k) for k in ("id", "title", "source")
                          if k in it})
        detail = fetch_detail(items[0])
        data = detail.get("data") or {}
        eps = data.get("episodes") or []
        print(f"detail {data.get('vod_name')!r} {len(eps)} 集")
        if eps:
            episodes.append(("browse", eps[0].get("url") or ""))
    for label, url in episodes:
        if not url:
            continue
        try:
            probe_episode(url, str(label))
        except Exception as exc:  # noqa: BLE001
            print(f"  !! {label}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
