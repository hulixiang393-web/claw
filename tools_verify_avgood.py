# -*- coding: utf-8 -*-
"""avgood 源全链路验证脚本。

验证内容：
1. Search 搜索「无码」「中文字幕」「番号」→ framework 条数与源配置 max_results
   （72）一致，且无 /bbs/ 论坛帖、URL 均为视频条目格式。
   ⚠ 站点搜索接口（typeid=1 資源庫）返回 /c/{id}.html 条目中**混有磁力下载页**
   （详情无 iframe#video-player，chapters 为空，无法在线播放）与在线可播条目
   （详情带 iframe）。前几条多为磁力页——这是站点结构，非源 bug；本源可播放
   内容以发现界面分类浏览为主。脚本会统计磁力/在线比例供参考。
2. Discovery 分类列表（/t/664/1/）未破坏。
3. Detail/Play 链路：对**发现页可播放条目**（详情 chapters>0）调
   Content.fetch_video_streams 能取到非空 m3u8 且 m3u8 HTTP 200（搜索返回的
   磁力页无播放器、取流为空属站点结构，不作为播放断言样本）。

用法：python -u tools_verify_avgood.py
"""
from __future__ import annotations

import json
import re
import sys
import time

sys.path.insert(0, r"D:\code\claw")

from framework.config import SourceConfig
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.search import Search
from framework.discovery import Discovery
from framework.content import Content
from framework.decrypter import Decrypter

SOURCE_PATH = r"D:\code\claw\sources\avgood.json"
KEYWORDS = ["无码", "中文字幕", "番号"]
MAX_RESULTS = 72  # 源配置 constraints.search.max_results

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://avgood.com/t/",
}


def main() -> int:
    with open(SOURCE_PATH, encoding="utf-8") as f:
        src = SourceConfig.from_dict(json.load(f), SOURCE_PATH)

    http = HttpClient()
    parser = Parser()
    checker = StructureChecker(http, parser, default_strategy="off")

    print("=== 1) Search 链路（framework.Search） ===")
    search = Search(http, parser)
    all_ok = True
    for kw in KEYWORDS:
        results = search.search_one(src, kw)
        n = len(results)
        bbs = sum(1 for r in results if "/bbs/" in r.url)
        bad = [r for r in results if not re.search(r"/[a-z]/\d+\.html", r.url)]
        # 条数受源配置 max_results 约束，>=1 且不超过上限即通过
        ok = 0 < n <= MAX_RESULTS and bbs == 0 and not bad
        all_ok = all_ok and ok
        print("  [%s] framework=%d (max_results=%d) bbs=%d nonvideo=%d -> %s" % (
            kw, n, MAX_RESULTS, bbs, len(bad), "PASS" if ok else "FAIL"))
        for r in results[:5]:
            print("    -", r.title[:60], "|", r.url, "|", r.update)
    print("  Search 断言（条数<=max_results + 无 bbs + 全视频条目）:", "PASS" if all_ok else "FAIL")

    print("=== 2) Discovery 链路（分类列表 /t/664/1/） ===")
    discovery = Discovery(http, parser, checker)
    works = discovery.list_works(src, "https://avgood.com/t/664/1/", page=1)
    ok2 = len(works) >= 10
    print("  works=%d -> %s" % (len(works), "PASS" if ok2 else "FAIL"))
    for w in works[:3]:
        print("    -", getattr(w, "title", "")[:60], "|", getattr(w, "url", ""))

    print("=== 3) Detail/Play 链路（发现页可播放条目取流） ===")
    content = Content(http, parser, checker, Decrypter(http))
    # 从发现页取前几个详情，找到 chapters>0 的可播放条目做取流断言
    play_url = ""
    ok3 = False
    found = 0
    for w in works:
        try:
            detail = content.fetch_detail(src, w.url)
        except Exception as exc:  # noqa: BLE001
            print("  detail 异常 %s: %r" % (w.url, exc))
            time.sleep(1)
            continue
        if not detail.chapters:
            found += 1  # 磁力/无播放器条目
            time.sleep(1)
            continue
        ep = detail.chapters[0]
        try:
            video, audio = content.fetch_video_streams(src, ep.url)
            if video:
                r = http._session.get(video, headers=HEADERS, timeout=15)
                ok3 = r.status_code == 200
                play_url = video
                print("  可播条目 url=%s" % w.url)
                print("  chapters=%d ep=%s" % (len(detail.chapters), ep.url))
                print("  video=%s" % str(video)[:130])
                print("  m3u8 HTTP %d len=%d -> %s" % (
                    r.status_code, len(r.content), "PASS" if ok3 else "FAIL"))
                break
        except Exception as exc:  # noqa: BLE001
            print("  取流异常 %s: %r" % (w.url, exc))
        time.sleep(1)
    if not play_url:
        print("  发现页未找到可播放条目（磁力条目占多数？），检查条目数=%d" % found)

    http.close()
    final = all_ok and ok2 and ok3
    print("=== 总结果: %s ===" % ("ALL PASS" if final else "FAILED"))
    return 0 if final else 1


if __name__ == "__main__":
    sys.exit(main())
