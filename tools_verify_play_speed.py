# -*- coding: utf-8 -*-
"""播放链路速度验证脚本 —— 排查「视频播放变慢」是否由 adblock 中间件拖慢。

测量项：
A. adblock 微基准（纯 CPU 开销，与网络无关）：
   - AdblockEngine 实例化耗时（adblock_for(source) 每次取流都会新建引擎）
   - is_ad_url × 10000（模拟对大量 URL 逐条判断）
   - is_ad_url 耗时构成拆解（urlparse / 路径正则 / query 参数解析 / 域名循环）
   - filter_urls × 10000（模拟 URL 列表批量过滤）
   - filter_m3u8 / detect_m3u8_ads（2000 段合成 m3u8，模拟播放路径若做
     m3u8 重写的最坏开销；当前播放路径并不调用，仅量化兜底）
B. 真实播放链路（默认 bilibili JSON API 源；可用 --source 换源）：
   - search 取一条真实视频 URL（仅用于拿到测试地址，不计入播放链路耗时）
   - fetch_detail（点播放前已完成，参考计时）
   - fetch_video_streams(episode_url, merged=True)：网络请求 + adblock 过滤
   - open_with_player()：外部播放器（VLC）拉起（--launch 才真启动）

判定阈值：整条播放链路总耗时 ≤ 10s 则不动代码；is_ad_url 1 万条 < 50ms 算正常。

用法：
  python -u tools_verify_play_speed.py             # 不真正拉起 VLC
  python -u tools_verify_play_speed.py --launch     # 取流完成后真正拉起 VLC
  python -u tools_verify_play_speed.py --source D:\code\claw\sources\avgood.json --keyword 无码
"""
from __future__ import annotations

import argparse
import json
import sys
import time

sys.path.insert(0, r"D:\code\claw")

from framework.adblock import AdblockEngine, adblock_for, _has_ad_query
from framework.config import SourceConfig
from framework.content import Content
from framework.decrypter import Decrypter
from framework.http import HttpClient
from framework.parser import Parser
from framework.search import Search
from framework.selfcheck import StructureChecker

SOURCE_PATH = r"D:\code\claw\sources\bilibili.json"
KEYWORD = "罗翔"

_ms = lambda s: s * 1000.0  # noqa: E731


def micro_bench(src: SourceConfig) -> None:
    print("\n[A] adblock micro-bench (pure CPU)")
    eng = AdblockEngine()
    # 1. is_ad_url × 10000：混合正常 CDN/分片 URL + 广告 URL
    urls = []
    for i in range(10000):
        if i % 10 == 0:
            urls.append("https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?ad_id=%d" % i)
        elif i % 7 == 0:
            urls.append("https://c.example.com/banner/gg_%d.png" % i)
        else:
            urls.append("https://cdn.example.com/seg/%d.ts?ts=%d" % (i, i))
    n = len(urls)
    t0 = time.perf_counter()
    hits = sum(1 for u in urls if eng.is_ad_url(u))
    dt = time.perf_counter() - t0
    print("  %-46s n=%-7d total=%.2f ms  (%.2f us/op)  hits=%d"
          % ("is_ad_url(mixed 10k)", n, _ms(dt), dt / n * 1e6, hits))

    # 1b. is_ad_url 耗时构成拆解（定位 us/op 花在哪）
    from urllib.parse import urlparse

    lows = [u.lower() for u in urls]
    parsed = [urlparse(u) for u in lows]
    t0 = time.perf_counter()
    _ = [urlparse(u) for u in lows]
    print("  %-46s n=%-7d total=%.2f ms"
          % ("   breakdown: urlparse", n, _ms(time.perf_counter() - t0)))
    t0 = time.perf_counter()
    _ = [eng._block_re.search(p.path) for p in parsed]
    print("  %-46s n=%-7d total=%.2f ms"
          % ("   breakdown: path regex", n, _ms(time.perf_counter() - t0)))
    t0 = time.perf_counter()
    _ = [_has_ad_query(u) for u in lows]
    print("  %-46s n=%-7d total=%.2f ms"
          % ("   breakdown: query parse_qsl", n, _ms(time.perf_counter() - t0)))

    # 2. filter_urls × 10000
    t0 = time.perf_counter()
    out = eng.filter_urls(urls)
    dt = time.perf_counter() - t0
    print("  %-46s n=%-7d total=%.2f ms  (%.2f us/op)  kept=%d"
          % ("filter_urls(mixed 10k)", n, _ms(dt), dt / n * 1e6, len(out)))

    # 3. filter_m3u8 / detect_m3u8_ads：2000 段合成 m3u8
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:10",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    for i in range(2000):
        if i % 500 == 0:
            lines.append("#EXT-X-DISCONTINUITY")
        dur = 1.5 if i % 500 == 0 else 9.5
        lines.append("#EXTINF:%.2f," % dur)
        lines.append("http://cdn.example.com/seg/%d.ts" % i)
    m3u8 = "\n".join(lines)
    t0 = time.perf_counter()
    cleaned = eng.filter_m3u8(m3u8, "https://cdn.example.com/play/list.m3u8")
    dt = time.perf_counter() - t0
    print("  %-46s segs=%-6d total=%.2f ms  (%.2f us/seg)  changed=%s"
          % ("filter_m3u8(2000 segs)", 2000, _ms(dt), dt / 2000 * 1e6,
             cleaned != m3u8))

    # 4. AdblockEngine 实例化（含 configure 读源配置）× 1000
    t0 = time.perf_counter()
    for _ in range(1000):
        AdblockEngine(src)
    dt = time.perf_counter() - t0
    print("  %-46s n=%-7d total=%.2f ms  (%.2f us/op)"
          % ("AdblockEngine(source) ctor", 1000, _ms(dt), dt / 1000 * 1e6))

    # 5. adblock_for(source) 每次新建引擎（播放路径现状）× 1000
    t0 = time.perf_counter()
    for _ in range(1000):
        adblock_for(src)
    dt = time.perf_counter() - t0
    print("  %-46s n=%-7d total=%.2f ms  (%.2f us/op)"
          % ("adblock_for(source) as called", 1000, _ms(dt), dt / 1000 * 1e6))


def real_chain(src: SourceConfig, launch: bool, keyword: str = KEYWORD) -> None:
    print("\n[B] real playback chain (%s)" % src.source_id)
    http = HttpClient()
    parser = Parser()
    checker = StructureChecker(http, parser, default_strategy="off")
    content = Content(http, parser, checker, Decrypter(http))
    search = Search(http, parser)

    t0 = time.perf_counter()
    results = search.search_one(src, keyword)
    t_search = time.perf_counter() - t0
    print("  search(grab test url, not in playback chain): %.2f ms, %d items"
          % (_ms(t_search), len(results)))
    if not results:
        print("  !! search failed, no video url to test. skip real chain.")
        http.close()
        return

    first = results[0]
    print("  video: %s" % (first.title or "")[:40])
    print("  url: %s" % first.url)

    # 详情页（真实 GUI 中用户在点播放前已完成；单独计时供参考）
    t0 = time.perf_counter()
    try:
        detail = content.fetch_detail(src, first.url)
    except Exception as exc:
        print("  !! fetch_detail failed: %r" % exc)
        http.close()
        return
    t_detail = time.perf_counter() - t0
    print("  fetch_detail(before click, ref): %.2f ms, %d eps"
          % (_ms(t_detail), len(detail.chapters)))
    if not detail.chapters:
        # 无分集（season 页直播）：GUI 里 detail.url 直接触发取流
        print("  !! no chapters -> season page direct-play with detail url")
        ep_url = detail.url or first.url
    else:
        ep_url = detail.chapters[0].url
    print("  episode url: %s" % ep_url)

    # 点击播放 → 取到播放地址
    t0 = time.perf_counter()
    try:
        video, audio = content.fetch_video_streams(src, ep_url, merged=True)
    except Exception as exc:
        print("  !! fetch_video_streams failed: %r" % exc)
        http.close()
        return
    t_fetch = time.perf_counter() - t0
    print("  fetch_video_streams(network+adblock): %.2f ms" % _ms(t_fetch))
    print("    video = %s" % str(video)[:110])
    print("    audio = %s" % str(audio)[:80])

    if not video:
        print("  !! no play url fetched.")
        http.close()
        return

    # adblock 过滤本身（fetch_video_streams 内 2 次 is_ad_url）
    eng = adblock_for(src)
    t0 = time.perf_counter()
    _v = video and eng.is_ad_url(video)
    _a = audio and eng.is_ad_url(audio)
    t_ad = time.perf_counter() - t0
    print("  adblock is_ad_url(video+audio): %.3f ms" % _ms(t_ad))

    from framework.external_player import open_with_player

    t0 = time.perf_counter()
    if launch:
        msg = open_with_player(video, audio=audio,
                               referer="https://www.bilibili.com/",
                               user_agent=src.request_headers().get("User-Agent", ""))
    else:
        msg = "(--launch not set, skip VLC start)"
    t_launch = time.perf_counter() - t0
    print("  open_with_player(VLC launch): %.2f ms  -> %s" % (_ms(t_launch), msg))

    total = t_fetch + t_launch
    print("\n  == playback chain total(fetch+launch): %.2f ms == %s"
          % (_ms(total), "<= 10s" if total <= 10 else "> 10s"))
    print("  == adblock portion: %.3f ms (%.2f%%) =="
          % (_ms(t_ad), t_ad / total * 100 if total else 0.0))
    http.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--launch", action="store_true", help="really launch VLC after fetch")
    ap.add_argument("--source", default=SOURCE_PATH, help="source config json path")
    ap.add_argument("--keyword", default=KEYWORD, help="search keyword")
    args = ap.parse_args()

    with open(args.source, encoding="utf-8") as f:
        src = SourceConfig.from_dict(json.load(f), args.source)
    print("== tools_verify_play_speed.py ==")
    print("source: %s (%s)" % (src.source_id, src.base_url))

    micro_bench(src)
    real_chain(src, args.launch, args.keyword)
    print("\n== done ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
