# -*- coding: utf-8 -*-
"""omofuna 取流链路端到端（本地仿真站）：播放页 → player_aaaa → m3u8 → 可播。

真实站点当前从本机不可达（www.omofuna.com 的 CDN 80/443 都不回包），
所以这里用本地 HTTP 服务复刻它的播放页结构，把「配置 → 解析 → 换线 →
返回可播地址」整条链路跑通，保证现场可达时配置本身没问题。
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from framework.config import load_source
from framework.content import Content
from framework.http import HttpClient, NetworkDefaults
from framework.parser import Parser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OMOFUNA = os.path.join(ROOT, "sources", "omofuna.json")

PLAY_HTML = """<html><body>
<div class="play-channel-item" data-sid="2">暴风</div>
<div class="play-channel-item" data-sid="4">量子</div>
<script>
var player_aaaa={"flag":"play","encrypt":0,"trysee":0,"points":0,
"link":"/play/2/1/","url":"{url}","from":"bfzym3u8","sid":"2","nid":"1",
"ps":"0","parse":"","vip":"0"}
</script></body></html>"""

M3U8 = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg1.ts\n#EXT-X-ENDLIST\n"


class _Handler(BaseHTTPRequestHandler):
    m3u8_url = ""

    def do_GET(self):  # noqa: N802 —— http.server 接口
        if self.path.startswith("/live/index.m3u8"):
            body = M3U8.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        elif "/play/" in self.path:
            body = PLAY_HTML.replace("{url}", _Handler.m3u8_url).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            body = b"<html><body>placeholder</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音
        return


@pytest.fixture()
def fake_site(tmp_path):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    _Handler.m3u8_url = "http://127.0.0.1:%d/live/index.m3u8" % port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    raw = json.load(open(OMOFUNA, encoding="utf-8"))
    raw["transports"]["base_url"] = "http://127.0.0.1:%d" % port
    raw["transports"]["prefer_proxy"] = False
    raw["transports"]["retries"] = 0
    raw["endpoints"]["content"]["episode"]["play_url"].pop("render", None)
    path = tmp_path / "omofuna-local.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    yield load_source(str(path)), "http://127.0.0.1:%d" % port, port
    server.shutdown()


def test_omofuna_play_page_resolves_to_playable_m3u8(fake_site):
    source, base, port = fake_site
    http = HttpClient(defaults=NetworkDefaults(timeout=8, retries=0, interval_ms=0))
    content = Content(http, Parser(), None)
    episode = "%s/anime/abc123/play/1/1.html?sid=2" % base
    try:
        streams = content.fetch_video_streams(source, episode)
    finally:
        http.close()
    video = streams[0] if isinstance(streams, (tuple, list)) else streams
    assert video == "http://127.0.0.1:%d/live/index.m3u8" % port

    # 真正拉一次清单，确认地址可播（不是外壳页）
    http = HttpClient(defaults=NetworkDefaults(timeout=8, retries=0, interval_ms=0))
    try:
        playlist = http.get_text(video)
    finally:
        http.close()
    assert playlist.startswith("#EXTM3U")


def test_omofuna_external_player_config_stays_consistent():
    source = load_source(OMOFUNA)
    play = source.raw["endpoints"]["content"]["episode"]["play_url"]
    external = play["external_player"]
    assert external["from"] == ["mp4"]
    assert external["prefer_direct"] is True          # 解析站走系统代理会 ReadTimeout
    assert external["cache_ttl_s"] > 0                # 一次成功后复用
    assert external["http_timeout_s"] >= 8            # 实测页面 6~11s 才吐完
    # 解析站挂掉时的兜底：解析结果落盘，6h 内可继续用上次的直链
    assert external["stale_ttl_s"] >= 3600
    assert play["probe"]["mode"] == "skip"
    transports = source.raw["transports"]
    # 不再全局声明 prefer_proxy：站点直连 0.3~0.9s，代理偏好会让每个新主机
    # 先白等一次 8s 探测（详情实测 12.7s vs 直连 1.2s）。出口选择交给
    # framework.http 的按主机自愈（代理失败当场撤销偏好，直连失败才学代理）。
    assert "prefer_proxy" not in transports
    assert transports["timeout"] <= 8
    resolver = source.raw["stream_resolver"]
    assert resolver["total_budget"] <= 45, "取流失败要在 45s 内收场，不再干等 90s"
    # 默认线路已改成直链暴风线（sid=2）：正常路径只有一次播放页请求，预算
    # 只需覆盖「1 次正常请求 + 1 次换线」。旧值 40s 是"先跑解析器线再换线"
    # 时代留的，留着只会让超时变成长时间干等。
    assert resolver["total_budget"] <= 30
    assert resolver["candidate_timeout"] <= 20
    switch = source.raw["endpoints"]["content"]["episode"]["source_switch"]
    assert switch["default_sid"] == "2"
    assert switch["max_switch_attempts"] == 2
