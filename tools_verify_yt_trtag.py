# -*- coding: utf-8 -*-
"""YouTube + 提线影院(trtag) 取流链路验证脚本。

验证内容：
1. YouTube（yt-dlp 引擎）：搜索 / 发现 / 详情 / 取流（merged 单流 + 双流）。
   依赖本机 yt-dlp 已安装且版本较新；若 yt-dlp 未装/过期（YouTube 改签名会
   403），取流必然失败，需手动更新：`yt-dlp -U` 或 `pip install -U yt-dlp`。
2. trtag（MacCMS 多线路）：搜索 / 发现 / 详情分集 / 取流 m3u8（HTTP 可访问）/
   换源（switch_source 各线路）+ 换源后取流。

用法：python -u tools_verify_yt_trtag.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

sys.path.insert(0, r"D:\code\claw")

from framework.config import SourceConfig
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.search import Search
from framework.discovery import Discovery
from framework.content import Content
from framework.decrypter import Decrypter

ROOT = r"D:\code\claw"

YT_SRC = ROOT + r"\sources\youtube.json"
YT_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
TR_SRC = ROOT + r"\sources\trtag.json"
TR_KEYWORD = "狂飙"


def load(src_path):
    with open(src_path, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), src_path)


def http_ok(url: str, headers: dict) -> bool:
    """HEAD/GET m3u8 是否可访问（200 且返回 HLS 头）。"""
    try:
        req = urllib.request.Request(url, headers=headers or {})
        resp = urllib.request.urlopen(req, timeout=15)
        body = resp.read(120).decode("utf-8", "replace")
        return resp.status == 200 and body.startswith("#EXTM3U")
    except Exception:
        return False


def main() -> int:
    print("=" * 60)
    print("YouTube 源（yt-dlp 引擎）")
    print("=" * 60)
    yt_src = load(YT_SRC)
    http = HttpClient()
    parser = Parser()
    checker = StructureChecker(http, parser, default_strategy="off")
    content = Content(http, parser, checker, Decrypter(http))
    search = Search(http, parser)
    discovery = Discovery(http, parser, checker)

    yt_ok = True
    try:
        res = search.search_one(yt_src, "music")
        ok1 = len(res) >= 5
        yt_ok = yt_ok and ok1
        print("[YT] search 'music' -> %d results : %s" % (len(res), "PASS" if ok1 else "FAIL"))
        for r in res[:2]:
            print("     -", r.title[:60], "|", r.url)
    except Exception as exc:
        yt_ok = False
        print("[YT] search EXC: %r" % exc)

    try:
        works = discovery.list_works(
            yt_src, "https://www.youtube.com/results?keyword=music", page=1
        )
        ok2 = len(works) >= 5
        yt_ok = yt_ok and ok2
        print("[YT] discovery -> %d works : %s" % (len(works), "PASS" if ok2 else "FAIL"))
    except Exception as exc:
        yt_ok = False
        print("[YT] discovery EXC: %r" % exc)

    try:
        d = content.fetch_detail(yt_src, YT_URL)
        ok3 = bool(d.title)
        yt_ok = yt_ok and ok3
        print("[YT] detail -> %r : %s" % (d.title, "PASS" if ok3 else "FAIL"))
    except Exception as exc:
        yt_ok = False
        print("[YT] detail EXC: %r" % exc)

    try:
        v, a = content.fetch_video_streams(yt_src, YT_URL, merged=True)
        ok4 = bool(v)
        yt_ok = yt_ok and ok4
        print("[YT] fetch_video_streams merged=True -> %s : %s" % (
            "video=" + str(v)[:60] if v else "EMPTY", "PASS" if ok4 else "FAIL"))
        v2, a2 = content.fetch_video_streams(yt_src, YT_URL, merged=False)
        ok5 = bool(v2) and bool(a2)
        yt_ok = yt_ok and ok5
        print("[YT] fetch_video_streams merged=False (双流) -> %s : %s" % (
            "video+audio OK" if ok5 else "video=%s audio=%s" % (str(v2)[:40], str(a2)[:40]),
            "PASS" if ok5 else "FAIL"))
    except Exception as exc:
        yt_ok = False
        print("[YT] streams EXC: %r" % exc)

    print()
    print("=" * 60)
    print("提线影院(trtag) 源（MacCMS 多线路）")
    print("=" * 60)
    tr_src = load(TR_SRC)
    tr_ok = True
    try:
        res = search.search_one(tr_src, TR_KEYWORD)
        ok1 = len(res) >= 5
        tr_ok = tr_ok and ok1
        print("[TR] search %r -> %d results : %s" % (TR_KEYWORD, len(res), "PASS" if ok1 else "FAIL"))
    except Exception as exc:
        tr_ok = False
        print("[TR] search EXC: %r" % exc)

    try:
        works = discovery.list_works(tr_src, "https://jyoyuy.com/tv/index.html", page=1)
        ok2 = len(works) >= 5
        tr_ok = tr_ok and ok2
        print("[TR] discovery -> %d works : %s" % (len(works), "PASS" if ok2 else "FAIL"))
    except Exception as exc:
        tr_ok = False
        print("[TR] discovery EXC: %r" % exc)

    tr_stream_ok = False
    if res:
        first = res[0]
        try:
            d = content.fetch_detail(tr_src, first.url)
            ok3 = bool(d.title) and len(d.chapters) > 0
            tr_ok = tr_ok and ok3
            print("[TR] detail -> %r chapters=%d source_list=%d : %s" % (
                d.title, len(d.chapters), len(d.source_list), "PASS" if ok3 else "FAIL"))
            if d.chapters:
                ep = d.chapters[0]
                v, a = content.fetch_video_streams(tr_src, ep.url)
                reach = http_ok(v, tr_src.request_headers()) if v else False
                tr_stream_ok = bool(v) and reach
                tr_ok = tr_ok and tr_stream_ok
                print("[TR] streams -> %s : %s" % (str(v)[:70], "PASS" if tr_stream_ok else "FAIL"))
        except Exception as exc:
            tr_ok = False
            print("[TR] detail/stream EXC: %r" % exc)
    else:
        tr_ok = False
        print("[TR] 无搜索结果可测")

    # 换源 + 各线路取流
    if d and d.source_list and d.chapters:
        switch_ok = True
        for s in d.source_list:
            sid = s["sid"]
            try:
                d2, chs = content.switch_source(tr_src, d.url, sid)
                if chs:
                    v, a = content.fetch_video_streams(tr_src, chs[0].url)
                    reach = http_ok(v, tr_src.request_headers()) if v else False
                    ok = bool(v) and reach
                    switch_ok = switch_ok and ok
                    print("[TR] switch sid=%s (%s) -> %s : %s" % (
                        sid, s.get("name", ""), str(v)[:60], "PASS" if ok else "FAIL"))
                else:
                    switch_ok = False
                    print("[TR] switch sid=%s -> 无分集 : FAIL" % sid)
                time.sleep(1.0)
            except Exception as exc:
                switch_ok = False
                print("[TR] switch sid=%s EXC: %r" % (sid, exc))
        tr_ok = tr_ok and switch_ok

    http.close()
    print()
    print("=== 总结果: %s ===" % ("ALL PASS" if (yt_ok and tr_ok) else "FAILED"))
    if not yt_ok:
        print("注意：YouTube 取流依赖本机 yt-dlp。若失败请运行 `yt-dlp -U` 或 `pip install -U yt-dlp`")
    return 0 if (yt_ok and tr_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
