# -*- coding: utf-8 -*-
"""omofuna 线路（精品/暴风）解析与「默认线路」选择。

站点 2026-10-31 改版后的真实结构（2026-11-02 复核仍然如此）：
    <a class="play-channel-item" href="/anime/<hash>/play/1/<n>.html"
       data-count="24" data-sid="1">精品</a>
    <a class="play-channel-item" href="/anime/<hash>/play/2/<n>.html"
       data-count="24" data-sid="2">暴风</a>

旧配置 `.channel-tab li a` + `sid_regex: playlist(\\d+)` 在新页面上**一个都匹配不到**，
线路列表恒为空 → 换线失效。

2026-11 起站点只保留两条线，且「精品」（sid=1）退化成第三方解析器线：
`player_aaaa.from=mp4`、`url` 是不透明加密 id，要靠 art.v2player.top 的
`playData(密文, key)` 二次解密。实测该解析站 5xx/长期无响应，一次尝试就吃掉
整段取流预算（日志：`第三方播放器解析失败 … art.v2player.top:8989/player/`），
随后换到暴风线才能出画面 —— 用户看到的就是「取流很久然后超时」。
所以默认线路必须是直链的暴风线（sid=2），精品线留在菜单里供需要时手动切。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import framework.content.video as video_mod
from framework.config import SourceConfig
from framework.content import Content
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker

ROOT = Path(video_mod.__file__).resolve().parents[2]

_PAGE = """
<html><body>
<div class="play-channel">
  <div class="play-channel-title">播放线路</div>
  <div class="play-channel-items">
    <a class="play-channel-item active" href="/anime/abc/play/1"
       data-count="24" data-sid="1">精品</a>
    <a class="play-channel-item" href="/anime/abc/play/2"
       data-count="24" data-sid="2">暴风</a>
  </div>
</div>
</body></html>
"""


@pytest.fixture()
def source():
    path = ROOT / "sources" / "omofuna.json"
    return SourceConfig.from_dict(
        json.loads(path.read_text(encoding="utf-8-sig")), str(path))


def _content() -> Content:
    http = HttpClient()
    return Content(http, Parser(), StructureChecker(http, Parser()))


def test_source_list_parses_play_channel_items(source):
    """data-sid 线路导航要能解析出 精品/暴风 两条线。"""
    lines = _content()._parse_source_list(source, _PAGE)
    got = {line["sid"]: line["name"] for line in lines}
    assert got == {"1": "精品", "2": "暴风"}


def test_source_list_ignores_legacy_selector(source):
    """旧 `.channel-tab` 结构不该再被当成线路来源（页面里已不存在）。"""
    old_page = ('<ul class="channel-tab"><li><a href="/play/playlist3">旧线</a></li></ul>')
    assert _content()._parse_source_list(source, old_page) == []


def test_default_sid_is_the_direct_cdn_line(source):
    """默认线路必须是暴风（sid=2，CDN 直链）。

    精品（sid=1）是解析器线：一次解析往返就是整段预算，且现场解析站不可用。
    默认走它 = 每次开播都先等一次注定失败的解密，再换线（取流超时的直接原因）。
    """
    switch = source.to_dict()["endpoints"]["content"]["episode"]["source_switch"]
    assert switch["default_sid"] == "2"
    # 站点只有两条线，换线尝试次数不必留 4 次（多的两次会去打不存在的 sid）
    assert switch["max_switch_attempts"] == 2


def test_source_switch_selectors_match_site(source):
    switch = source.to_dict()["endpoints"]["content"]["episode"]["source_switch"]
    assert switch["list_selector"] == ".play-channel-item"
    assert switch["list_attr"] == "data-sid"
    assert switch["name_selector"] == ".play-channel-item"
    # 新结构 href 里没有 playlist<digits>，留着只会误伤
    assert not switch.get("sid_regex")


def test_premium_line_skips_render_and_uses_external_player(source):
    """精品线（from=mp4）不许再走 playwright 渲染，只能走外部解析器且预算受限。

    实测渲染 47s 仍拿不到地址（Artplayer 把地址赋给 property，等不到就是等不到），
    白等一轮渲染比「直接换线」慢一个数量级；解析器也有独立预算上限，
    这样即使它挂了，整次取流仍然在总预算内收得住。
    """
    play = source.to_dict()["endpoints"]["content"]["episode"]["play_url"]
    assert play.get("render_skip_from") == ["mp4"]
    assert play.get("render") == "playwright_fallback"
    external = play["external_player"]
    assert external["from"] == ["mp4"]
    assert external["budget_s"] <= 10
    assert external["http_timeout_s"] <= 8


def test_video_scan_reads_js_assigned_src():
    """渲染取源必须读 video 的 property（currentSrc/src），不能只读 HTML 属性。

    Artplayer（v2player 精品线）把地址 JS 赋给元素，属性为空；
    旧实现只 `get_attribute("src")` → 整条线判成"没有播放器"。
    """
    from framework import playwright_helper as helper

    src = Path(helper.__file__).read_text(encoding="utf-8")
    assert "currentSrc" in src, "渲染取源没有读 currentSrc"
    assert re.search(r"el => el\.currentSrc \|\| el\.src", src), \
        "渲染取源没有用 property 取值"


def test_media_request_filter_accepts_extensionless_cdn():
    """无扩展名的 CDN 直链要靠 mime_type=video 识别（tiktokcdn 形态）。"""
    from framework import playwright_helper as helper

    src = Path(helper.__file__).read_text(encoding="utf-8")
    assert "mime_type=video" in src


def test_premium_url_shape_is_playable():
    """精品线取到的地址要能被 `_is_playable_direct` 判定为可播。"""
    url = ("https://v16-tiktokcdn-com.akamaized.net/bd6414c2a66918f53efa8d9bdbc79de5"
           "/6ae6834a/video/tos/alisg/tos-alisg-v-0051c001-sg/oEBME9VEQ9mSiPUb0A9zTRAaiQaP"
           "/?a=1233&mime_type=video_mp4&vid=v29033g50000d9jcoh7og65tukgdaj60")
    assert video_mod.VideoMixin._is_playable_direct(url) is True
