# -*- coding: utf-8 -*-
"""omofuna 线路（精品/暴风）解析与「精品」线取流。

站点 2026-10-31 改版后的真实结构：
    <a class="play-channel-item" href="/anime/<hash>/play/1" data-count="24" data-sid="1">精品</a>
    <a class="play-channel-item" href="/anime/<hash>/play/2" data-count="24" data-sid="2">暴风</a>

旧配置 `.channel-tab li a` + `sid_regex: playlist(\\d+)` 在新页面上**一个都匹配不到**，
线路列表恒为空 → 换线失效；`default_sid` 又写成站点根本不存在的 "3"。

另外「精品」线（sid=1）的 `player_aaaa.from` 是 `mp4`、`url` 是不透明加密 id，
曾经被 `render_skip_from: ["mp4"]` 直接判死。实际它能播：渲染播放页后
Artplayer 把真实地址（tiktokcdn 的 272MB mp4，时长 23.9min）**赋给 video 的
property**，`get_attribute("src")` 读不到、`currentSrc` 才能读到。
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


def test_default_sid_is_premium_line(source):
    """站点默认线路是「精品」（sid=1），配置必须对齐，否则取不到片源。"""
    switch = source.to_dict()["endpoints"]["content"]["episode"]["source_switch"]
    assert switch["default_sid"] == "1"


def test_source_switch_selectors_match_site(source):
    switch = source.to_dict()["endpoints"]["content"]["episode"]["source_switch"]
    assert switch["list_selector"] == ".play-channel-item"
    assert switch["list_attr"] == "data-sid"
    assert switch["name_selector"] == ".play-channel-item"
    # 新结构 href 里没有 playlist<digits>，留着只会误伤
    assert not switch.get("sid_regex")


def test_premium_line_is_not_skipped_by_render(source):
    """`render_skip_from` 含 mp4 会把整条精品线判死（它 from=mp4）。

    实测精品线渲染后能拿到 272MB / 23.9min 的可播地址，所以这里必须为空。
    """
    play = source.to_dict()["endpoints"]["content"]["episode"]["play_url"]
    assert play.get("render_skip_from") == []
    assert play.get("render") == "playwright_fallback"


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
