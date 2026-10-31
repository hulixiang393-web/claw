# -*- coding: utf-8 -*-
"""确认 omofuna 能取到**可播放**的流（走生产链路，不模拟）。

用法：
    python tools/verify_omofuna_stream.py                # 自动挑一部番、逐条线路验证
    python tools/verify_omofuna_stream.py <剧集URL>       # 指定播放页（?sid=1/2/4）
    python tools/verify_omofuna_stream.py <剧集URL> sid   # 只验证某条线路

每一步都打印耗时，最后用 HTTP Range 真拉一次流，确认拿到的是媒体字节
（mp4 的 ftyp / m3u8 的 #EXTM3U），而不是网页外壳。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from framework.config import load_source  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.http import HttpClient, NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")


class _Checker:
    def _selfcheck_url(self, source):
        return ""

    def check(self, source, url):
        return True


def _probe_playable(url: str) -> str:
    """真拉一小段字节，判断是否媒体内容。"""
    headers = {"User-Agent": UA, "Referer": "https://www.omofuna.com/"}
    try:
        with requests.get(url, headers=headers, timeout=20, stream=True, verify=False) as resp:
            chunk = next(resp.iter_content(2048), b"")
            kind = "?"
            head = chunk[:200]
            if b"#EXTM3U" in head:
                kind = "HLS 清单"
            elif b"ftyp" in chunk[:64] or chunk[:4] in (b"\x00\x00\x00\x18", b"\x00\x00\x00\x20"):
                kind = "MP4 分片"
            elif chunk[:1] == b"\x47":
                kind = "TS 分片"
            return "HTTP %s | %d B | %s | %s" % (
                resp.status_code, len(chunk), kind,
                (resp.headers.get("Content-Type") or "-")[:40],
            )
    except Exception as exc:  # noqa: BLE001
        return "拉流失败：%s %s" % (type(exc).__name__, str(exc)[:80])


def _episode_url(content, discovery, source) -> str:
    works = discovery.list_works(source, source.base_url + "/type/1-1.html", 1)
    if not works:
        raise SystemExit("发现页没有作品：站点当前不可达或改版")
    detail = content.fetch_detail(source, works[0].url)
    episodes = getattr(detail, "episodes", None) or []
    if not episodes:
        raise SystemExit("详情页没有剧集：%s" % works[0].url)
    return episodes[0].url


def main(argv) -> int:
    source = load_source(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "sources", "omofuna.json",
    ))
    http = HttpClient(defaults=NetworkDefaults(timeout=10, retries=1, interval_ms=200))
    parser = Parser()
    checker = _Checker()
    content = Content(http, parser, checker)
    discovery = Discovery(http, parser, checker)
    try:
        if len(argv) > 1 and argv[1].startswith("http"):
            episode = argv[1]
        else:
            started = time.time()
            try:
                episode = _episode_url(content, discovery, source)
            except SystemExit as exc:
                print("发现/详情失败：%s（耗时 %.1fs）" % (exc, time.time() - started))
                return 2
            print("首集：%s（发现+详情 %.1fs）" % (episode, time.time() - started))

        wanted = argv[2] if len(argv) > 2 else ""
        for sid in (["1", "2", "4"] if not wanted else [wanted]):
            url = episode
            if "sid=" in episode:
                url = episode.split("sid=")[0] + "sid=" + sid
            elif "?" in episode:
                url = episode + "&sid=" + sid
            else:
                url = episode + "?sid=" + sid
            started = time.time()
            try:
                streams = content.fetch_video_streams(source, url)
            except Exception as exc:  # noqa: BLE001
                print("线路 sid=%s 取流失败 %.1fs：%s %s"
                      % (sid, time.time() - started, type(exc).__name__, str(exc)[:120]))
                continue
            cost = time.time() - started
            video = ""
            if isinstance(streams, tuple):
                video = streams[0] if streams else ""
            elif isinstance(streams, list):
                video = streams[0][0] if streams and isinstance(streams[0], tuple) else ""
            elif isinstance(streams, str):
                video = streams
            if not video:
                print("线路 sid=%s 未取到地址 %.1fs" % (sid, cost))
                continue
            print("线路 sid=%s 取流 %.1fs → %s" % (sid, cost, video[:110]))
            print("           验播：%s" % _probe_playable(video))
    finally:
        http.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
