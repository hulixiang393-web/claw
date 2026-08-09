# -*- coding: utf-8 -*-
"""bilibili / hanime1 取流链路验证脚本（tools_verify_bili_hanime.py）。

验证内容（每源走完整「搜索 → 详情 → 取流 → 播放地址可访问」链路，并分段计时）：
1. 取流链路每步网络请求数 + 每请求耗时（get_text/get_json 打点，按线程过滤，
   只统计主线程同步等待；后台自检线程请求不计入）。
2. 播放地址有效性：HTTP Range GET，接受 200/206（拒绝 403/404）。
3. 媒体首包/首 256KB 耗时（TTFB + 初段下载速度）——用于定位「加载慢」是否
   来自取流环节本身，而非播放器/远端 CDN 交付。
4. bilibili 无登录画质限制如实报告（不硬解 cookie）。

结论基线：2026-08 实测两源取流均为单请求（bilibili ~0.1s / hanime1 详情复用
缓存 0 请求，冷取流 ~1.1s），无多余串行请求、无大页面解析开销。若此脚本后续
跑出明显慢点（>2s 单请求 / 多次重试 / 403 播放地址），应回到取流链路排查。

用法：python -u tools_verify_bili_hanime.py
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time

sys.path.insert(0, r"D:\code\claw")

from framework.config import SourceConfig
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.content import Content
from framework.decrypter import Decrypter
from framework.search import Search

import requests

RECORDS: list = []  # (phase, method, url, dt)
PHASE = [""]
_THREAD_MAIN = "MainThread"


def _short(u, n=108):
    u = str(u)
    return u if len(u) <= n else u[:n] + "..."


def install_timer(http: HttpClient) -> None:
    """给 HttpClient 打点：记录每次 get_* 的 URL 与耗时（含重试/interval）。"""
    for name in ("get_text", "get_json", "get_bytes"):
        fn = getattr(http, name)

        def wrap(fn=fn, name=name):
            def inner(*a, **kw):
                url = a[0] if a else kw.get("url")
                t0 = time.perf_counter()
                try:
                    return fn(*a, **kw)
                finally:
                    if threading.current_thread().name == _THREAD_MAIN:
                        RECORDS.append((PHASE[0], name, url, time.perf_counter() - t0))
            return inner

        setattr(http, name, wrap(fn, name))


def begin(phase: str) -> None:
    PHASE[0] = phase
    del RECORDS[:]


def dump(title: str) -> None:
    print("    == %s ==" % title)
    if not RECORDS:
        print("    (no sync main-thread request - cache hit / parallel workers)")
        return
    for phase, name, url, dt in RECORDS:
        print("    [%s] %-9s %6.2fs  %s" % (phase, name, dt, _short(url)))


def load_source(path: str) -> SourceConfig:
    with open(path, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), path)


def verify_url(url: str, source: SourceConfig, label: str) -> bool:
    """播放地址有效性：Range GET 首 2KB，接受 200/206，拒绝 403/404。"""
    hdrs = source.request_headers()
    hdrs["Referer"] = hdrs.get("Referer") or (source.transports().get("base_url") or "") + "/"
    hdrs["Range"] = "bytes=0-2047"
    try:
        r = requests.get(url, headers=hdrs, timeout=15, stream=True)
        r.close()
        ok = r.status_code in (200, 206)
        print("    verify %s: status=%d %s -> %s" % (
            label, r.status_code,
            r.headers.get("content-type", "")[:30] if not ok else "",
            "PASS" if ok else "FAIL"))
        if not ok:
            print("    url=%s" % _short(url, 200))
        return ok
    except Exception as exc:
        print("    verify %s: error %r -> FAIL" % (label, exc))
        return False


def measure_delivery(url: str, source: SourceConfig, label: str, chunk_kb: int = 256) -> None:
    """首包 + 首 256KB 耗时（媒体交付速度，辅助判断慢点归属）。"""
    hdrs = source.request_headers()
    hdrs["Referer"] = hdrs.get("Referer") or (source.transports().get("base_url") or "") + "/"
    try:
        t0 = time.perf_counter()
        with requests.get(url, headers=hdrs, timeout=30, stream=True) as r:
            t_first = time.perf_counter() - t0
            total, t_last = 0, t_first
            for chunk in r.iter_content(chunk_size=64 * 1024):
                total += len(chunk)
                t_last = time.perf_counter() - t0
                if total >= chunk_kb * 1024:
                    break
        print("    delivery %s: ttl_first=%.2fs got=%dKB in %.2fs (avg=%.0fKB/s)"
              % (label, t_first, total // 1024, t_last,
                 (total / 1024) / t_last if t_last > 0 else 0))
    except Exception as exc:
        print("    delivery %s: error %r" % (label, exc))


def bilibili_chain(content: Content, search: Search, src: SourceConfig) -> bool:
    print("=== [1] bilibili: search -> detail -> stream ===")
    ok = True
    begin("search")
    try:
        res = search.search_one(src, "洛天依")
        first = res[0]
        print("    first: %s | %s" % (first.title[:44], _short(first.url)))
    except Exception as exc:
        print("    search FAILED: %r" % exc)
        return False
    dump("search (constraints.max_pages=3, 1 api/page)")

    begin("detail")
    try:
        detail = content.fetch_detail(src, first.url)
        print("    detail: %s chapters=%d" % (detail.title[:44], len(detail.chapters)))
    except Exception as exc:
        print("    detail FAILED: %r" % exc)
        return False
    dump("detail (api_endpoints.detail, 1 api)")

    ep = detail.chapters[0].url if detail.chapters else detail.url
    begin("stream")
    try:
        video, audio = content.fetch_video_streams(src, ep, quality="best", merged=True)
        print("    video=%s" % _short(video, 120))
        print("    audio=%s" % (_short(audio, 80) if audio else "(none, single mp4)"))
    except Exception as exc:
        print("    stream FAILED: %r" % exc)
        return False
    dump("stream (api_endpoints.episode, 1 api)")

    # 无登录画质如实报告（不硬解 cookie）
    m_bv = re.search(r"(BV[0-9A-Za-z]+)", ep)
    m_cid = re.search(r"(?:cid|p)=(\d+)", ep)
    if m_bv and m_cid:
        from urllib.parse import urlencode, urljoin
        cfg = (src.raw.get("api_endpoints") or {}).get("episode", {})
        qs = urlencode({"bvid": m_bv.group(1), "cid": m_cid.group(1), "qn": "80",
                        "fnval": "1", "platform": "pc"})
        purl = urljoin(src.base_url, cfg["url"]) + "?" + qs
        try:
            rj = http_probe(purl, src)
            data = rj.get("data") or {}
            print("    anonymous quality: requested qn=80, granted=%s (accept=%s) -> %s"
                  % (data.get("quality"), data.get("accept_quality"),
                     "login cookie needed for 1080p+" if data.get("quality", 0) < 80 else "max allowed"))
        except Exception as exc:
            print("    quality probe skipped: %r" % exc)

    if not verify_url(video, src, "bili mp4"):
        ok = False
    measure_delivery(video, src, "bili mp4")
    return ok


def hanime_chain(content: Content, search: Search, src: SourceConfig) -> bool:
    print("=== [2] hanime1: search -> detail -> stream (cached + fresh) ===")
    ok = True
    begin("search")
    try:
        res = search.search_one(src, "原神")
        first = res[0]
        print("    first: %s | %s" % (first.title[:44], _short(first.url)))
    except Exception as exc:
        print("    search FAILED: %r" % exc)
        return False
    dump("search (endpoints.search, parallel pages)")

    begin("detail")
    try:
        detail = content.fetch_detail(src, first.url)
        print("    detail: %s chapters=%d" % (detail.title[:44], len(detail.chapters)))
    except Exception as exc:
        print("    detail FAILED: %r" % exc)
        return False
    dump("detail (single_chapter watch page, cached for play)")

    begin("stream-cached")
    try:
        video, _ = content.fetch_video_streams(src, first.url, merged=True)
        print("    stream(app path, detail html reused): %s" % _short(video, 110))
    except Exception as exc:
        print("    stream FAILED: %r" % exc)
        return False
    dump("stream (should be 0 request - _video_html_cache hit)")

    begin("stream-fresh")
    try:
        content._video_html_cache.clear()
        video2, _ = content.fetch_video_streams(src, first.url, merged=True)
        print("    stream(fresh, cache miss): %s" % _short(video2, 110))
    except Exception as exc:
        print("    fresh stream FAILED: %r" % exc)
        return False
    dump("stream (cold: 1 watch-page html GET)")

    if not verify_url(video, src, "hanime mp4"):
        ok = False
    measure_delivery(video, src, "hanime mp4")
    return ok


def http_probe(url: str, source: SourceConfig) -> dict:
    """轻量探测接口 JSON（画质信息用，不走打点避免污染计时）。"""
    probe = HttpClient()
    try:
        return probe.get_json(url, headers=source.request_headers(),
                              timeout=float(source.transports().get("timeout") or 10))
    finally:
        probe.close()


def main() -> int:
    http = HttpClient()
    parser = Parser()
    checker = StructureChecker(http, parser, default_strategy="off")
    install_timer(http)
    search = Search(http, parser)
    content = Content(http, parser, checker, Decrypter(http))

    bili = load_source(r"D:\code\claw\sources\bilibili.json")
    hanime = load_source(r"D:\code\claw\sources\hanime1.json")

    print("#" * 72)
    print("# tools_verify_bili_hanime - stream fetch timing + play URL check")
    print("#" * 72)

    ok1 = bilibili_chain(content, search, bili)
    print()
    ok2 = hanime_chain(content, search, hanime)
    print()
    print("=== result: bilibili=%s hanime1=%s ===" % ("PASS" if ok1 else "FAIL",
                                                       "PASS" if ok2 else "FAIL"))
    http.close()
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
