# -*- coding: utf-8 -*-
"""饭搭子影视（fdzys）与 18J.TV（18j）源全链路验证：发现 → 详情 → 章节 → 取流 → m3u8 可达。

用法：python -u tools_verify_fdzys_18j.py
通过条件（全部断言）：
  - 两源配置加载 OK
  - discovery >= 1 条（title/url/cover 有效）
  - 详情 title 有效、chapters >= 1
  - fetch_video_streams 返回非空 m3u8
  - m3u8 HTTP 可达（200/206）

2026-08 修复要点：
  - fdzys：详情 URL 必须带 .html（无后缀返回反爬落地页）→ detail.url_suffix=".html"；
    章节列表 .player_name[data-sid] 源 tab + #playlist{sid} a[href*='/tv/']；
    播放页 player_aaaa 的 url 即 m3u8 直链（sid=2 无印云 ps=0 直接用）。
  - 18j：详情页即播放页（const source='...m3u8' 内嵌直链）→ 单集 single_chapter，
    play_url.regex 提取 m3u8。
"""
from __future__ import annotations

import json
import re
import sys

sys.path.insert(0, r"D:\code\claw")

from framework.config import SourceConfig
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.discovery import Discovery
from framework.content import Content
from framework.decrypter import Decrypter

BASE = r"D:\code\claw\sources"

PASS = 0
FAIL = []


def check(name: str, ok: bool, extra: str = ""):
    global PASS
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}" + (f"  {extra}" if extra else ""))


def m3u8_reachable(url: str, referer: str) -> bool:
    """m3u8 HTTP 可达性：200 或 206，content-type 含 mpegurl/hls 或文本以 #EXTM3U 开头。"""
    try:
        import requests
    except ImportError:
        return False
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": referer,
            },
            timeout=20,
        )
        if r.status_code not in (200, 206):
            return False
        ct = r.headers.get("content-type", "")
        if "mpegurl" in ct or "hls" in ct or "octet-stream" in ct:
            return True
        return r.text.lstrip().startswith("#EXTM3U")
    except Exception:
        return False


def run_source(path: str, source_id: str, discovery_url: str,
               referer: str, min_chapters: int) -> bool:
    """单源全链路：discovery → 详情 → 取流 → m3u8 可达。返回是否全过。"""
    ok = True
    print(f"\n===== 源: {source_id} =====")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    src = SourceConfig.from_dict(raw, path)
    check("配置加载", src.source_id == source_id, src.source_name)
    check("transport.base_url", src.base_url.startswith("https://"))

    with HttpClient() as http:
        parser = Parser()
        checker = StructureChecker(http=http, parser=parser, default_strategy="off")

        # --- discovery ---
        disc = Discovery(http=http, parser=parser, checker=checker)
        works = []
        try:
            works = disc.list_works(src, discovery_url, 1)
        except Exception as e:
            print(f"  [ERR] discovery: {e}")
        print(f"  discovery: {len(works)} 条")
        for w in works[:3]:
            print(f"    - {w.title[:30]} | {w.url}")
        check("discovery >= 1", len(works) >= 1, f"got {len(works)}")
        if works:
            check("works title/url 有效", all(w.title and w.url for w in works[:3]))
            ok = ok and len(works) >= 1 and all(w.title and w.url for w in works[:3])
        else:
            check("works title/url 有效", False, "无作品")
            ok = False

        # --- 详情 + 章节 ---
        ct = Content(http=http, parser=parser, checker=checker, decrypter=Decrypter(http))
        detail = None
        if works:
            try:
                detail = ct.fetch_detail(src, works[0].url)
            except Exception as e:
                print(f"  [ERR] 详情: {e}")
        if detail:
            print(f"  标题: {detail.title}")
            print(f"  章节数: {len(detail.chapters)}")
            if detail.chapters:
                print(f"    首章: {detail.chapters[0].title[:40]} | {detail.chapters[0].url}")
            check("详情 title 有效", bool(detail.title), detail.title[:40])
            check(f"chapters >= {min_chapters}",
                  len(detail.chapters) >= min_chapters,
                  f"got {len(detail.chapters)}")
            ok = ok and bool(detail.title) and len(detail.chapters) >= min_chapters
        else:
            check("详情 title 有效", False, "详情为空")
            check(f"chapters >= {min_chapters}", False, "详情为空")
            ok = False

        # --- 取流 + m3u8 可达 ---
        if detail and detail.chapters:
            ch = detail.chapters[0]
            try:
                v, a = ct.fetch_video_streams(src, ch.url)
                print(f"  取流: {v[:80]}")
                check("取流非空 (m3u8)", bool(v) and ".m3u8" in v, v[:70])
                if v:
                    reach = m3u8_reachable(v, referer)
                    check("m3u8 HTTP 可达", reach, v[:70])
                    ok = ok and bool(v) and ".m3u8" in v and reach
                else:
                    check("m3u8 HTTP 可达", False, "取流为空")
                    ok = False
            except Exception as e:
                print(f"  [ERR] 取流: {e}")
                check("取流非空 (m3u8)", False, str(e)[:70])
                check("m3u8 HTTP 可达", False, str(e)[:70])
                ok = False
        else:
            check("取流非空 (m3u8)", False, "无章节")
            check("m3u8 HTTP 可达", False, "无章节")
            ok = False
    return ok


def main() -> int:
    fdzys_ok = run_source(
        rf"{BASE}\fdzys.json", "fdzys", "/tv/all",
        "https://fdzys.com/", min_chapters=1,
    )
    okj_ok = run_source(
        rf"{BASE}\18j.json", "18j", "/vod/",
        "https://18j.tv/", min_chapters=1,
    )

    print(f"\n==== 结果: {PASS} PASS / {len(FAIL)} FAIL ====")
    if FAIL:
        print("FAILED:", FAIL)
        print(f"fdzys 全链路: {'OK' if fdzys_ok else 'FAIL'}")
        print(f"18j 全链路: {'OK' if okj_ok else 'FAIL'}")
        return 1
    print("ALL PASS")
    print(f"fdzys 全链路: OK  |  18j 全链路: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
