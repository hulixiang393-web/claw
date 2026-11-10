# -*- coding: utf-8 -*-
"""Omofun动漫（omofuna.com）源离线单元测试。

fixture 取自站点真实 DOM（2026-10-02/03 抓取）：
- 线路页签 ul.channel-tab > li > a[href='#playlist3'][data-toggle=tab]（sid 在 href，
  无 data-sid 属性）+ 各线路 ul.play-list-content > li > a[href=/anime/{id}/play/{sid}/{ep}.html]
- 列表/搜索卡片：a[data-original][href^=/anime/]（封面在锚点 data-original，非 img）
- 搜索被「系统安全验证」拦截：需先 POST /index.php/ajax/verify_check?type=search
  携带 i=btoa(XOR(毫秒时间戳, 固定密钥))，成功置 PHPSESSID 后正常返回 SSR 结果
- 分类分页 /type/2.html → /type/2-2.html；list_url 用 /type/1.html（/show/ 路径被验证页拦截）
"""
import base64
import inspect
import json
import logging
import os
import sys
import time

import pytest
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.errors import ContentMissingError  # noqa: E402
from framework.stream_resolver import (  # noqa: E402
    StreamBudget, StreamCancelledError, stream_resolver_options,
)
from framework.decrypter import Decrypter  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.parser import Parser  # noqa: E402
from framework.search import Search, _maccms_ts_b64  # noqa: E402


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "sources", "omofuna.json")


def source():
    with open(PATH, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), PATH)


def probe_enabled(src):
    """打开播放地址探测（现网 omofuna 配的是 probe.mode=skip）。

    站点直链对直接请求恒 403、只有本地媒体代理能取，所以线上把探测关了。
    但本文件里测的是**探测/死线黑名单/候选回退**这套机制本身，与站点是否
    跳过探测无关，故这些用例先摘掉 probe 段。
    """
    play = src.raw["endpoints"]["content"]["episode"]["play_url"]
    play.pop("probe", None)
    return src


# 真实列表/搜索卡片（type 页与 search 页同构）
_LIST_CARD = (
    '<li><div class="daFJ_dJJfFHa__gEI">'
    '<a class="daFJ_dJJfFHa__aAICe lazyload" href="/anime/9da25325d089689fc0c0a74f.html" '
    'target="_blank" title="斗罗大陆2：绝世唐门" '
    'data-original="https://as.cfhls.top/upload/vod/20230723-6/876d8c1cfe17f396433fd2f912084567.jpg">'
    '<span class="aeAI hidden-xs"></span><span class="aaE_EaDEa text-right"><b>国语/2023</b></span>'
    '<span class="aaE_EaDE text-right"><b>173集</b></span></a>'
    '<div class="daFJ_dJJfFHa__cFAFeA"><h4 class="title text-overflow">'
    '<a href="/anime/9da25325d089689fc0c0a74f.html" title="斗罗大陆2：绝世唐门">斗罗大陆2：绝世唐门</a>'
    "</h4></div></div></li>"
)

# 真实详情页（/anime/e7f12f9fc5b567f379a2cc2b.html 裁剪：海报 + 元数据 + 四线路）
_DETAIL_HTML = """<html><body><div class="container">
<div class="daFJ_iJEdJHa__aAICe"><a class="pic" href="" title="无上神帝"><img class="lazyload"
 data-original="https://as.cfhls.top/upload/vod/20230723-6/420b20f28b350c6654be8034f790be81.jpg"
 src="/template/the4/statics/img/load.gif" /></a></div>
<div class="daFJ_iJEdJHa__cFAFeA"><h1 class="title">无上神帝</h1>
<p class="data hidden-xs">类型：动画,热血,战斗,剧情,奇幻,玄幻,动作 / 地区：中国大陆 / 年份：2020</p>
<p class="data">状态：<span style="color: red;">645集</span></p>
<p class="data hidden-xs">更新：2026-10-02</p>
<p class="data">导演：<a href="/search/-----%E9%A1%B9%E6%96%B9--------.html" target="_blank">项方</a>&nbsp;</p></div>
<div class="detail-sketch">万年之前，仙王牧云因持有诛仙图而遭人暗算<span class="detail-content"
 style="display: none;">万年之前，仙王牧云因持有诛仙图而遭人暗算，残魂沉睡万年之后觉醒。</span></div>
<div class="card mb20"><ul class="channel-tab"><li><a href="#playlist3" data-toggle="tab">天堂<span>645</span></a></li><li><a href="#playlist1" data-toggle="tab">精品<span>645</span></a></li><li><a href="#playlist2" data-toggle="tab">暴风<span>645</span></a></li><li><a href="#playlist4" data-toggle="tab">量子<span>645</span></a></li></ul>
<div class="tab-content"><div id="playlist3" class="tab-pane"><ul class="play-list-content"><li class="play-list-content-item" data-name="1-50"><a href="/anime/e7f12f9fc5b567f379a2cc2b/play/3/1.html" title="第01集" data-num="1">第01集</a></li><li class="play-list-content-item" data-name="1-50"><a href="/anime/e7f12f9fc5b567f379a2cc2b/play/3/2.html" title="第02集" data-num="2">第02集</a></li></ul></div>
<div id="playlist1" class="tab-pane"><ul class="play-list-content"><li><a href="/anime/e7f12f9fc5b567f379a2cc2b/play/1/1.html">第01集</a></li></ul></div>
<div id="playlist2" class="tab-pane"><ul class="play-list-content"><li><a href="/anime/e7f12f9fc5b567f379a2cc2b/play/2/1.html">第01集</a></li></ul></div>
<div id="playlist4" class="tab-pane"><ul class="play-list-content"><li><a href="/anime/e7f12f9fc5b567f379a2cc2b/play/4/1.html">第01集</a></li></ul></div>
</div></div></div></body></html>"""


# 2026-11 播放页实测裁剪（/anime/fdd1ae735172e5b711ae99f0/play/2/1.html?sid=2）：
# 线路 tab 是 a.play-channel-item[href][data-count][data-sid]，只剩两条线路
# （1=精品 第三方解析器线、2=暴风 CDN 直链线）；分集在 ul.play-list-content。
# 站点改版前这里还有 3/4（天堂/量子），现在已不存在。
_PLAY_PAGE_HTML = """<html><body><div class="play-channel">
<a class="play-channel-item" href="/anime/e7f12f9fc5b567f379a2cc2b/play/1/1.html" data-count="24" data-sid="1">精品</a>
<a class="play-channel-item" href="/anime/e7f12f9fc5b567f379a2cc2b/play/2/1.html" data-count="24" data-sid="2">暴风</a>
</div>
<ul class="play-list-content"><li class="play-list-content-item" data-name="1-50"><a href="/anime/e7f12f9fc5b567f379a2cc2b/play/2/1.html" title="第01集" data-num="1">第01集</a></li><li class="play-list-content-item" data-name="1-50"><a href="/anime/e7f12f9fc5b567f379a2cc2b/play/2/2.html" title="第02集" data-num="2">第02集</a></li></ul>
<script>var player_aaaa={"flag":"play","encrypt":1,"from":"bfzym3u8","server":"no","sid":2,"nid":1,"id":"fdd1ae735172e5b711ae99f0","url":"https%3A%2F%2Fp.bvvvvvvvvv1f.com%2Fvideo%2Freplace0001%2F20250310yN2SDZnWindex.m3u8%2Findex.m3u8"}</script>
</body></html>"""

# 同一页面但线路 tab 只剩 href（站点改版 / 抽样式改动的风险形态）
_PLAY_PAGE_HREF_ONLY = _PLAY_PAGE_HTML.replace(
    ' data-count="24" data-sid="1"', ""
).replace(' data-count="24" data-sid="2"', "")


class _FakeHttp:
    defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()
    def __init__(self, pages=None):
        self.pages = pages or {}

    def get_text(self, url, headers=None, proxy=None, timeout=10, retries=3,
                 interval_ms=0, encoding=None, proxy_pool=None, direct=False):
        for key, html in self.pages.items():
            if key in url:
                return html
        return self.pages.get("*", "")

    def post_json(self, *a, **k):
        return {}

    def close(self):
        pass


class _Checker:
    pass


def _content(pages):
    http = _FakeHttp(pages)
    return Content(http, Parser(), _Checker(), decrypter=Decrypter(http))


def test_omofuna_categories_and_channels():
    src = source()
    cats = src.get_discovery_config()["list_item"]["categories"]
    assert [x["title"] for x in cats] == ["日漫", "国漫", "动画", "剧场", "特摄", "美漫"]
    switch = src.raw["endpoints"]["content"]["episode"]["source_switch"]
    # 2026-11 现场：线路只剩 1=精品（第三方解析器线）、2=暴风（CDN 直链线）。
    # 默认线路必须走直链线：精品线要 art.v2player.top 二次解密，实测必败且
    # 吃掉整段预算，这正是「取流 20s+ 然后超时」的直接原因。
    assert switch["default_sid"] == "2"
    assert switch["max_switch_attempts"] == 2
    # 线路 tab 现在是 a.play-channel-item[data-sid][href=/play/<sid>/<ep>.html]
    assert switch["list_selector"] == ".play-channel-item"
    assert switch["list_attr"] == "data-sid"
    assert switch["sid_regex"] == ""
    # 分集 href 是 /anime/{id}/play/{sid}/{ep}.html（框架缺省 /tv/ 会全部过滤）
    assert switch["ep_href_contains"] == "/play/"
    # 线路菜单从首集播放页补（详情页 0 个 .play-channel-item）
    assert switch["probe_from_chapter"] is True


def test_omofuna_external_player_budget_is_capped():
    """解析器线预算必须小：它会和换线抢同一份总预算。"""
    src = source()
    play = src.raw["endpoints"]["content"]["episode"]["play_url"]
    assert play["external_player"]["budget_s"] <= 10
    assert play["external_player"]["http_timeout_s"] <= 8
    budgets = src.raw["stream_resolver"]
    # 两条线路各一次机会即可，别留出"跑满 40s 才报错"的空档
    assert budgets["candidate_timeout"] < 20
    assert budgets["total_budget"] <= 30


def test_omofuna_episode_and_player_rules():
    src = source()
    episode = src.raw["endpoints"]["content"]["episode"]
    assert "player_aaaa" in episode["source_switch"]["play_regex"]
    assert episode["list"]["fields"]["url"]["attr"] == "href"
    decrypt = src.raw["decryption"]["targets"]["video_url"]
    assert decrypt["strategy"] == "js_custom"
    assert decrypt["script"] == "function decrypt(d) { return decodeURIComponent(d); }"


def test_external_player_skipped_when_budget_too_low(monkeypatch):
    """剩余预算不足时**不启动**第三方解析器，直接换线。

    现场（2026-11-02 日志）：`第三方播放器解析失败 … art.v2player.top:8989`
    每次都要吃满预算，而同一集直链线路只要 1s。预算已经不多时再赌一次解析，
    等于把最后的换线机会一起烧掉 —— 用户看到的就是「取流很久然后超时」。
    """
    src = source()
    cont = _content({})
    play = src.raw["endpoints"]["content"]["episode"]["play_url"]
    calls = []

    import framework.external_player as external_mod

    def fake(http, cfg, player, abs_ep):
        calls.append(dict(cfg))
        return "https://cdn.example/live.m3u8"

    monkeypatch.setattr(external_mod, "resolve_external_player", fake)
    player = {"from": "mp4", "url": "cb4eb195e96af271670c19b4a98ea3b4"}
    ep = "https://www.omofuna.com/anime/demo/play/1/1.html?sid=1"

    # 只剩 1s：解析器一步都别迈
    assert cont._resolve_external_stream(
        src, play, player, ep, deadline=time.monotonic() + 1.0
    ) == ""
    assert calls == []

    # 剩 10s：跑，但预算被压到配置上限以内（不能吃满剩余时间）
    assert cont._resolve_external_stream(
        src, play, player, ep, deadline=time.monotonic() + 10.0
    ) == "https://cdn.example/live.m3u8"
    assert calls and calls[0]["budget_s"] <= 9
    assert calls[0]["budget_s"] <= 10


def test_external_player_budget_shrinks_with_remaining_time(monkeypatch):
    """剩余预算比配置上限还小时，按剩余预算收窄（留 1s 收尾）。"""
    src = source()
    cont = _content({})
    play = src.raw["endpoints"]["content"]["episode"]["play_url"]
    calls = []

    import framework.external_player as external_mod

    monkeypatch.setattr(
        external_mod, "resolve_external_player",
        lambda http, cfg, player, abs_ep: calls.append(dict(cfg)) or "",
    )
    cont._resolve_external_stream(
        src, play, {"from": "mp4"}, "https://www.omofuna.com/play/1/1.html",
        deadline=time.monotonic() + 5.0,
    )
    assert calls and calls[0]["budget_s"] == pytest.approx(4.0, abs=0.3)


def test_omofuna_maccms_url_decodes_public_player_value():
    url = "https://vip.dytt-tvs.com/20261002/39442_f477a6f0/index.m3u8"
    cipher = base64.b64encode(quote(url, safe="").encode("ascii")).decode("ascii")
    assert Decrypter(None)._maccms_url(cipher) == url
    assert Decrypter(None)._maccms_url(quote(url, safe="")) == url


def test_omofuna_encrypt_one_uses_javascript_unescape():
    from framework.content import Content

    source_obj = source()
    encoded = quote("https://vip.example/video/index.m3u8", safe="")
    html = (
        '<script>var player_aaaa={"encrypt":1,"url":"'
        + encoded
        + '"}</script>'
    )
    content = Content.__new__(Content)
    content._decrypter = Decrypter(None)
    result = Content._fetch_play_url_from_player(
        content, html,
        source_obj.raw["endpoints"]["content"]["episode"]["source_switch"],
        "https://www.omofuna.com/anime/demo/play/3/1.html",
        source=source_obj,
    )
    assert result == "https://vip.example/video/index.m3u8"


def test_omofuna_episode_selector_parses_links():
    src = source()
    cfg = src.raw["endpoints"]["content"]["episode"]["list"]
    html = "<ul class='play-list-content'><li><a href='/anime/abc/play/3/645.html'>第645集</a></li></ul>"
    items = Parser().parse_items(Parser().parse(html), cfg["root_selector"], cfg["fields"], src.base_url)
    assert items == [{"title": "第645集", "url": "https://www.omofuna.com/anime/abc/play/3/645.html"}]


def test_omofuna_detail_channels_and_chapters():
    """2026-11 详情页：**没有**线路 tab，线路菜单改由首集播放页补。

    改版前详情页是 `ul.channel-tab > a[href=#playlistN]`；现在线路 tab 只渲染在
    播放页（`a.play-channel-item[data-sid]`）。详情解析拿不到线路时必须走
    `source_switch.probe_from_chapter` 补一次首集页请求，否则换源菜单为空、
    默认线路失败后永远无法换线（用户看到的就是取流一直超时）。
    """
    src = source()
    detail = _content({
        "play/2/1.html": _PLAY_PAGE_HTML,
        "e7f12f9fc5b567f379a2cc2b.html": _DETAIL_HTML,
    }).fetch_detail(src, "/anime/e7f12f9fc5b567f379a2cc2b.html")
    assert detail.title == "无上神帝"
    assert detail.cover == "https://as.cfhls.top/upload/vod/20230723-6/420b20f28b350c6654be8034f790be81.jpg"
    assert "残魂沉睡万年之后觉醒" in detail.summary
    assert detail.status == "645集"
    assert detail.author == "项方"
    # tags 只取「类型：」那一个 p（另有 更新 hidden-xs p，选择器过宽会混入空串）
    assert detail.tags == ["动画,热血,战斗,剧情,奇幻,玄幻,动作"]
    # 线路菜单来自首集播放页：精品1 / 暴风2（顺序与页面一致）
    assert [s["sid"] for s in detail.source_list] == ["1", "2"]
    assert [s["name"] for s in detail.source_list] == ["精品", "暴风"]
    # 默认线路 = default_sid=2（暴风直链线），分集 URL 带 sid=2
    urls = [ch.url for ch in detail.chapters]
    assert urls == [
        "https://www.omofuna.com/anime/e7f12f9fc5b567f379a2cc2b/play/2/1.html?sid=2"
    ]
    assert [ch.title for ch in detail.chapters] == ["第01集"]


def test_omofuna_line_menu_parses_new_channel_items():
    """线路 tab 新结构：a.play-channel-item[data-sid][data-count][href]。"""
    src = source()
    menu = _content({})._parse_source_list(src, _PLAY_PAGE_HTML)
    assert [(m["sid"], m["name"]) for m in menu] == [("1", "精品"), ("2", "暴风")]


def test_omofuna_line_sid_falls_back_to_href():
    """改版只剩 href（没有 data-sid）时仍要认出线路。

    拿不到 sid 不是"少一条线路"，而是整份线路表为空 → 换源菜单消失、
    取流失败后无线路可换。
    """
    src = source()
    menu = _content({})._parse_source_list(src, _PLAY_PAGE_HREF_ONLY)
    assert [m["sid"] for m in menu] == ["1", "2"]


def test_omofuna_line_menu_empty_page_is_not_an_error():
    """播放页确实没有线路块时返回空表，不抛异常（交给上层决定要不要重试）。"""
    src = source()
    html = (
        "<html><body><ul class='play-list-content'>"
        "<li><a href='/anime/demo/play/2/1.html'>第01集</a></li></ul></body></html>"
    )
    assert _content({})._parse_source_list(src, html) == []


def test_omofuna_list_items_from_real_card():
    """列表卡片：根为封面锚点自身（data-original），title/data-original/href 同锚点。"""
    src = source()
    wi = src.get_discovery_config()["works_list_item"]
    items = Parser().parse_items(Parser().parse(_LIST_CARD), wi["root_selector"], wi["fields"], src.base_url)
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "斗罗大陆2：绝世唐门"
    assert it["url"] == "https://www.omofuna.com/anime/9da25325d089689fc0c0a74f.html"
    assert it["cover"].startswith("https://as.cfhls.top/upload/vod/")


def test_omofuna_search_item_parses_real_card():
    src = source()
    scfg = src.get_search_config()
    assert scfg["paginator"]["url_template"] == "/search/{keyword}-------------.html"
    item = scfg["item"]
    items = Parser().parse_items(Parser().parse(_LIST_CARD), item["root_selector"], item["fields"], src.base_url)
    assert items[0]["title"] == "斗罗大陆2：绝世唐门"
    assert items[0]["cover"].startswith("https://as.cfhls.top/upload/vod/")


def test_omofuna_search_verify_config_and_token():
    """搜索前置验证配置 + maccms_ts_b64 token 可逆（XOR(毫秒串, key)→base64）。"""
    src = source()
    v = src.get_search_config()["verify"]
    assert v["url"] == "/index.php/ajax/verify_check?type=search"
    assert v["token"]["strategy"] == "maccms_ts_b64"
    key_hex = v["token"]["key_hex"]
    assert len(key_hex) == 32
    ms = 1790985763286
    tok = _maccms_ts_b64(key_hex, ms)
    raw = base64.b64decode(tok)
    k = bytes.fromhex(key_hex)
    assert bytes(b ^ k[i % 16] for i, b in enumerate(raw)).decode("ascii") == str(ms)


def test_omofuna_search_verify_posts_once_per_session():
    class _RecHttp(_FakeHttp):
        def __init__(self):
            super().__init__()
            self.calls = []

        def post_json(self, url, json_body=None, headers=None, timeout=None, retries=None, **k):
            self.calls.append((url, json_body, headers))
            return {"code": 1, "msg": "ok"}

    http = _RecHttp()
    search = Search(http, Parser())
    src = source()
    search._verify_search(src)
    search._verify_search(src)
    assert len(http.calls) == 1
    url, body, headers = http.calls[0]
    assert url == "https://www.omofuna.com/index.php/ajax/verify_check?type=search"
    assert set(body.keys()) == {"i"}
    raw = base64.b64decode(body["i"])
    k = bytes.fromhex(src.get_search_config()["verify"]["token"]["key_hex"])
    ms = int(bytes(b ^ k[i % 16] for i, b in enumerate(raw)).decode("ascii"))
    assert abs(time.time() * 1000 - ms) < 60_000
    assert headers and "omofuna" in headers.get("Referer", "")


def test_omofuna_pagination_and_list_url():
    """list_url 用 /type/1-{page}.html（/show/… 会命中安全验证页）；分页 /type/2-2.html。"""
    src = source()
    disc = src.get_discovery_config()
    assert disc["list_url"] == "/type/1-{page}.html"
    d = Discovery(_FakeHttp(), Parser(), _Checker())
    assert d._build_page_url(src, disc["list_url"], 1) == "/type/1-1.html"
    assert d._build_page_url(src, "/type/2-{page}.html", 2) == "/type/2-2.html"


def test_omofuna_switch_derives_detail_url_from_episode():
    """换源轮换依赖 detail_url_regex：分集 /anime/{id}/play/{sid}/{ep}.html
    → 详情 /anime/{id}.html（死链线路自动切换的前提）。"""
    src = source()
    cont = _content({})
    ep = "https://www.omofuna.com/anime/59e5d4cdb1bcefc84eb4b1f4/play/3/1.html?sid=3"
    assert cont._detail_url_from_episode(src, ep) == (
        "https://www.omofuna.com/anime/59e5d4cdb1bcefc84eb4b1f4.html"
    )


_CHANNEL_STRIP = (
    '<div class="play-channel">'
    '<a class="play-channel-item" href="/anime/demo/play/3/1.html" '
    'data-count="24" data-sid="3">天堂</a>'
    '<a class="play-channel-item" href="/anime/demo/play/1/1.html" '
    'data-count="24" data-sid="1">精品</a>'
    "</div>"
)
_PLAY3_DEAD = (
    "<html><body>" + _CHANNEL_STRIP +
    '<script>var player_aaaa={"flag":"play","encrypt":0,'
    '"url":"https://dead.example/xx.m3u8","from":"player","ps":0}'
    "</script></body></html>"
)
_PLAY1_LIVE = (
    "<html><body>" + _CHANNEL_STRIP +
    '<script>var player_aaaa={"flag":"play","encrypt":0,'
    '"url":"https://live.example/xx.m3u8","from":"player","ps":0}'
    "</script></body></html>"
)


def test_omofuna_non_http_token_is_not_a_playable_stream(monkeypatch):
    src = source()
    cont = _content({})
    monkeypatch.setattr(Content, "_fetch_play_url_once", lambda *args: "provider_token")
    monkeypatch.setattr(Content, "_switch_candidate_episode_urls", lambda *args: [])
    with pytest.raises(Exception, match="全部线路取流失败"):
        cont.fetch_video_episode(
            src,
            "https://www.omofuna.com/anime/demo/play/3/1.html?sid=3",
        )


def test_safe_url_redacts_url_without_secondary_name_error():
    assert Content._safe_url("https://cdn.example/live.m3u8?token=secret") == (
        "https://cdn.example/live.m3u8"
    )


@pytest.mark.parametrize("source_lock", [False, True])
def test_fetch_video_streams_passes_deadline_and_context_to_resolver(source_lock):
    src = source()
    cont = _content({})
    calls = []

    class _Resolver:
        def resolve(self, *args, **kwargs):
            calls.append((args, kwargs))
            return "video", "audio"

    cont._stream_resolver = _Resolver()
    deadline = 123.45
    context = StreamBudget()

    assert cont.fetch_video_streams(
        src, "episode", "720p", True, deadline=deadline, context=context, source_lock=source_lock
    ) == ("video", "audio")
    # 源级预算（stream_resolver.candidate_timeout / total_budget）必须一起传给
    # resolver：慢站不放预算就会在候选阶段被判超时、异常被吞成空地址（黑屏）。
    assert calls == [((src, "episode", "720p", True), {
        "deadline": deadline, "context": context, "source_lock": source_lock,
        **stream_resolver_options(src),
    })]


def test_fetch_video_streams_keeps_legacy_four_argument_call_compatible():
    src = source()
    cont = _content({})
    calls = []

    def resolve(self, *args, **kwargs):
        calls.append((args, kwargs))
        return "video", "audio"

    cont._stream_resolver = type("Resolver", (), {"resolve": resolve})()

    assert cont.fetch_video_streams(src, "episode", "720p", True) == ("video", "audio")

    args, kwargs = calls[0]
    assert args == (src, "episode", "720p", True)
    assert kwargs == {
        "deadline": None, "context": None, "source_lock": False,
        **stream_resolver_options(src),
    }


def test_player_aaaa_cancelled_decryption_is_not_fallback(monkeypatch):
    src = source()
    cont = _content({})
    cont._decrypter = type(
        "Decrypter", (), {"decrypt": lambda *args: (_ for _ in ()).throw(StreamCancelledError("cancelled"))}
    )()

    with pytest.raises(StreamCancelledError):
        cont._extract_play_url_candidates(
            '<script>var player_aaaa={"url":"encrypted"};</script>'
            '<video><source src="https://fallback.example/live.m3u8"></video>',
            src,
            {},
            {},
            "https://video.example/episode.html",
            "episode",
        )


def test_player_aaaa_deadline_error_is_not_fallback():
    src = source()
    cont = _content({})
    cont._decrypter = type(
        "Decrypter", (), {"decrypt": lambda *args: (_ for _ in ()).throw(TimeoutError("expired"))}
    )()

    with pytest.raises(TimeoutError):
        cont._extract_play_url_candidates(
            '<script>var player_aaaa={"url":"encrypted"};</script>'
            '<video><source src="https://fallback.example/live.m3u8"></video>',
            src,
            {},
            {},
            "https://video.example/episode.html",
            "episode",
        )


def test_player_aaaa_ordinary_decryption_error_uses_fallback():
    src = source()
    cont = _content({})
    cont._decrypter = type(
        "Decrypter", (), {"decrypt": lambda *args: (_ for _ in ()).throw(ValueError("bad input"))}
    )()

    assert cont._extract_play_url_candidates(
        '<script>var player_aaaa={"url":"encrypted"};</script>'
        '<video><source src="https://fallback.example/live.m3u8"></video>',
        src,
        {},
        {},
        "https://video.example/episode.html",
        "episode",
    ) == ["https://fallback.example/live.m3u8"]


def test_source_switch_logs_only_fixed_fields(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    src = source()
    cont = _content({})
    episode_url = "https://www.omofuna.com/anime/demo/play/3/1.html?sid=3&token=secret"
    key = cont._episode_key(episode_url)
    cont._dead_episode_lines[src.source_id] = {key: time.monotonic()}
    monkeypatch.setattr(Content, "_switch_candidate_episode_urls", lambda *args: [])

    with pytest.raises(Exception, match="全部线路取流失败"):
        cont._fetch_play_url_auto_switch(src, episode_url)

    assert episode_url not in caplog.text
    assert "www.omofuna.com" not in caplog.text
    assert "token=secret" not in caplog.text
    assert "线路索引=1" in caplog.text
    assert "异常类型=KnownDeadLine" in caplog.text
    assert "阶段=跳过" in caplog.text


def test_source_switch_exception_log_has_no_url_or_exception_text(caplog, monkeypatch):
    src = source()
    cont = _content({})
    secret = "https://cdn.example/live.m3u8?token=secret-signature"

    def fail(*args, **kwargs):
        raise RuntimeError(f"request failed for {secret}; Cookie: secret-cookie")

    monkeypatch.setattr(Content, "_fetch_play_url_candidates_once", fail)
    monkeypatch.setattr(Content, "_switch_candidate_episode_urls", lambda *args: [])
    with caplog.at_level(logging.INFO, logger="framework.content"):
        with pytest.raises(Exception, match="全部线路取流失败"):
            cont._fetch_play_url_auto_switch(
                src, "https://www.omofuna.com/anime/demo/play/3/1.html?sid=3"
            )
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in messages
    assert secret not in messages
    assert "secret-cookie" not in messages
    assert "阶段=取流" in messages
    assert "线路索引=1" in messages
    assert "www.omofuna.com" not in messages


def test_public_video_streams_pass_deadline_to_ytdlp_and_use_remaining_timeout(monkeypatch):
    src = source()
    src.raw["api_endpoints"] = {"episode": {"engine": "ytdlp"}}
    cont = _content({})
    calls = []

    class _Ytdlp:
        def fetch_streams(self, *args, **kwargs):
            calls.append((args, kwargs))
            return "video", "audio"

    cont._ytdlp = _Ytdlp()
    deadline = time.monotonic() + 4
    cont._stream_resolver = type(
        "Resolver", (), {"resolve": lambda self, *args, **kwargs: cont._fetch_video_streams_legacy(
            *args, deadline=kwargs["deadline"]
        )}
    )()

    assert cont.fetch_video_streams(src, "https://video.example/watch/1", deadline=deadline) == (
        "video", "audio"
    )
    assert calls and calls[0][1]["timeout"] <= 4


def test_public_video_streams_pass_deadline_to_api_and_html(monkeypatch):
    src = source()
    src.raw["api_endpoints"] = {"episode": {"url": "/api/{id}", "response_path": "data"}}
    cont = _content({})
    api_calls = []

    def get_json(*args, **kwargs):
        api_calls.append(kwargs)
        return {"data": "https://live.example/video.m3u8"}

    cont._http.get_json = get_json
    deadline = time.monotonic() + 4
    cont._stream_resolver = type(
        "Resolver", (), {"resolve": lambda self, *args, **kwargs: cont._fetch_video_streams_legacy(
            *args, deadline=kwargs["deadline"]
        )}
    )()
    assert cont.fetch_video_streams(src, "https://video.example/watch/1", deadline=deadline) == (
        "https://live.example/video.m3u8", ""
    )
    assert api_calls and api_calls[0]["timeout"] <= 4
    assert api_calls[0]["retries"] == 0

    src.raw["api_endpoints"] = {}
    src.raw["endpoints"]["content"]["episode"]["source_switch"] = {}
    html_calls = []
    monkeypatch.setattr(
        Content,
        "_fetch_play_url_once",
        lambda self, source, url, deadline=None: html_calls.append(deadline) or "https://live.example/html.m3u8",
    )
    assert cont.fetch_video_streams(src, "https://video.example/watch/1", deadline=deadline) == (
        "https://live.example/html.m3u8", ""
    )
    assert html_calls == [deadline]


def test_play_url_probe_respects_short_remaining_deadline(monkeypatch):
    import requests

    src = source()
    cont = _content({})
    calls = []

    class _Response:
        status_code = 200

        class _Raw:
            @staticmethod
            def read(size):
                return b"stream"

        raw = _Raw()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def get(*args, **kwargs):
        calls.append(kwargs)
        return _Response()

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(Content, "_timeout", lambda self, source: 0.1)
    now = [10.0]
    monkeypatch.setattr("framework.content.time.monotonic", lambda: now[0])

    assert cont._play_url_probe(src, "https://cdn.example/live.m3u8", deadline=10.2)
    assert calls[0]["timeout"] == 0.1

    calls.clear()
    with pytest.raises(TimeoutError):
        cont._play_url_probe(src, "https://cdn.example/live.m3u8", deadline=10.0)
    assert calls == []


def test_source_switch_stops_after_resolver_deadline(monkeypatch):
    from framework.stream_resolver import StreamResolver

    src = source()
    # 源级 stream_resolver.total_budget 会覆盖实例预算（设计如此），这里要测的是
    # 「换线阶段不得越过 resolver 的剩余预算」，所以先把源级预算摘掉。
    src.raw.pop("stream_resolver", None)
    cont = _content({})
    next_line_calls = []
    candidate_deadlines = []
    candidate_finished = __import__("threading").Event()

    def fetch_candidates(self, source, episode_url, deadline=None):
        candidate_deadlines.append(deadline)
        time.sleep(0.05)
        candidate_finished.set()
        # 本线路空手而归（现网 omofuna 的直链解析实际也常这样：播放页拿到了，
        # player_aaaa 里的地址却拼不出来）。此时才轮到换线 —— 而预算已耗尽，
        # 换线必须一步都不迈出去。
        return []

    monkeypatch.setattr(Content, "_fetch_play_url_candidates_once", fetch_candidates)
    monkeypatch.setattr(
        Content,
        "_switch_candidate_episode_urls",
        lambda *args, **kwargs: next_line_calls.append(True) or [
            "https://next.example/episode"
        ],
    )
    cont.resolve_video_stream_candidates = (
        lambda source, episode_url, quality, merged, deadline=None: [
            lambda: cont._fetch_play_url_auto_switch(source, episode_url, deadline=deadline)
        ]
    )
    cont._stream_resolver = StreamResolver(cont, total_budget=0.05)

    try:
        result = cont.fetch_video_streams(
            src, "https://www.omofuna.com/anime/demo/play/3/1.html?sid=3"
        )
    except (TimeoutError, ContentMissingError):
        # 预算耗尽的两种合法收尾：超时 / 明确失败（都不能是"看起来成功"）
        result = ("", "")
    assert result == ("", "")
    assert candidate_finished.wait(1)
    assert candidate_deadlines and candidate_deadlines[0] is not None
    assert next_line_calls == [], "预算耗尽后不得再发起换线请求"


def test_omofuna_dead_line_remembered_and_skipped(monkeypatch):
    """probe 失败的线路记入 TTL 黑名单：复播同集不再重抓死线路播放页、
    不再打已知死链 probe（死线路串行轮换是取流 8.9s 的最大耗时段）。"""
    calls = []

    class _RouterHttp(_FakeHttp):
        def get_text(self, url, *a, **k):
            calls.append(url)
            return super().get_text(url, *a, **k)

    pages = {
        "play/3/1.html": _PLAY3_DEAD,
        "play/1/1.html": _PLAY1_LIVE,
        "e7f12f9fc5b567f379a2cc2b.html": _DETAIL_HTML,
    }
    src = probe_enabled(source())
    http = _RouterHttp(pages)
    cont = Content(http, Parser(), _Checker(), decrypter=Decrypter(http))
    probes = []

    def _fake_probe(self, source, url):
        probes.append(url)
        return "dead" not in url

    monkeypatch.setattr(Content, "_play_url_probe", _fake_probe)
    ep = "https://www.omofuna.com/anime/e7f12f9fc5b567f379a2cc2b/play/3/1.html?sid=3"

    v1 = cont.fetch_video_episode(src, ep)
    assert v1 == "https://live.example/xx.m3u8"
    assert sum(1 for u in calls if "/play/3/" in u) == 1

    v2 = cont.fetch_video_episode(src, ep)
    assert v2 == "https://live.example/xx.m3u8"
    # 死线路播放页复播时不再抓（首轮已判死）
    assert sum(1 for u in calls if "/play/3/" in u) == 1
    # 已知死链不再重复 probe
    assert sum(1 for u in probes if "dead.example" in u) == 1


def test_omofuna_stream_candidates_fall_back_within_one_line(monkeypatch):
    src = probe_enabled(source())
    ep = "https://www.omofuna.com/anime/demo/play/3/1.html?sid=3"
    html = (
        '<script>var player_aaaa={"url":"https://dead.example/one.m3u8",'
        '"ps":0}</script>'
        '<video><source src="https://live.example/two.m3u8"></video>'
    )
    cont = _content({"play/3/1.html": html})
    probes = []

    def probe(self, source, url):
        probes.append(url)
        return "dead" not in url

    monkeypatch.setattr(Content, "_play_url_probe", probe)
    monkeypatch.setattr(Content, "_switch_candidate_episode_urls", lambda *args: [])

    assert cont.fetch_video_streams(src, ep)[0] == "https://live.example/two.m3u8"
    assert probes == [
        "https://dead.example/one.m3u8",
        "https://live.example/two.m3u8",
    ]


def test_stream_candidate_relative_url_is_absolutized():
    src = source()
    ep = "https://www.omofuna.com/anime/demo/play/3/1.html?sid=3"
    html = '<video><source src="/media/live.m3u8"></video>'
    cont = _content({"play/3/1.html": html})

    assert cont._fetch_play_url_candidates_once(src, ep) == [
        "https://www.omofuna.com/media/live.m3u8"
    ]
    assert cont._fetch_play_url_once(src, ep) == "https://www.omofuna.com/media/live.m3u8"


def test_stream_candidates_support_var_now_and_video_source_deduplication():
    src = source()
    ep = "https://www.omofuna.com/anime/demo/play/3/1.html?sid=3"
    html = (
        '<script>var now = "/media/live.m3u8";</script>'
        '<video><source src="/media/live.m3u8"></video>'
    )
    cont = _content({"play/3/1.html": html})

    assert cont._fetch_play_url_candidates_once(src, ep) == [
        "https://www.omofuna.com/media/live.m3u8"
    ]


def test_stream_candidates_support_escaped_json_m3u8():
    src = source()
    ep = "https://www.omofuna.com/anime/demo/play/3/1.html?sid=3"
    html = (
        '<script>var player_aaaa={\\"url\\":\\"https:\\/\\/live.example\\/x.m3u8\\"};'
        "</script>"
    )
    cont = _content({"play/3/1.html": html})

    assert cont._fetch_play_url_candidates_once(src, ep) == [
        "https://live.example/x.m3u8"
    ]


def test_stream_fallback_error_does_not_include_signed_candidate():
    src = probe_enabled(source())
    ep = "https://www.omofuna.com/anime/demo/play/3/1.html?sid=3"
    secret = "https://dead.example/live.m3u8?token=secret-signature"
    cont = _content({"play/3/1.html": f'<video><source src="{secret}"></video>'})
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Content, "_play_url_probe", lambda *args: False)
    monkeypatch.setattr(Content, "_switch_candidate_episode_urls", lambda *args: [])
    try:
        with pytest.raises(Exception) as exc_info:
            cont.fetch_video_episode(src, ep)
    finally:
        monkeypatch.undo()
    assert "secret-signature" not in str(exc_info.value)


def test_source_switch_passes_deadline_into_rendered_play_url(monkeypatch):
    src = source()
    episode = src.raw["endpoints"]["content"]["episode"]
    episode["play_url"] = {"render": "playwright"}
    cont = _content({})
    seen = []

    def fetch_once(self, source, episode_url, deadline=None):
        seen.append(deadline)
        return "https://live.example/video.m3u8"

    monkeypatch.setattr(Content, "_fetch_play_url_once", fetch_once)
    deadline = time.monotonic() + 1
    assert cont._fetch_play_url_candidates_once(src, "https://www.omofuna.com/play/1.html", deadline) == [
        "https://live.example/video.m3u8"
    ]
    assert seen == [deadline]


def test_source_switch_exception_log_is_redacted(monkeypatch, caplog):
    src = source()
    cont = _content({})
    secret = "https://cdn.example/live.m3u8?token=secret-signature"

    def fail(*args, **kwargs):
        raise RuntimeError(f"request failed for {secret}; Cookie: session=secret-cookie")

    monkeypatch.setattr(Content, "_switch_candidate_episode_urls", fail)
    with caplog.at_level("INFO", logger="framework.content"):
        with pytest.raises(Exception, match="全部线路取流失败"):
            cont._fetch_play_url_auto_switch(
                src, "https://www.omofuna.com/anime/demo/play/3/1.html?sid=3"
            )
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in messages
    assert secret not in messages
    assert "secret-cookie" not in messages


def test_stream_budget_cancellation_is_checked_before_api_stage():
    src = source()
    cont = _content({})
    calls = []
    cont._http.get_json = lambda *args, **kwargs: calls.append(kwargs) or {}
    context = type(
        "ExpiredContext",
        (),
        {
            "deadline": time.monotonic() - 1,
            "check": lambda self: (_ for _ in ()).throw(TimeoutError("expired")),
            "remaining": lambda self: 0.0,
        },
    )()

    with pytest.raises(TimeoutError):
        cont._fetch_episode_api(
            src, "https://video.example/watch/1", {"url": "/api/{id}"}, context=context
        )

    assert calls == []


def test_html_stream_request_uses_context_remaining_timeout(monkeypatch):
    src = source()
    cont = _content({"play/3/1.html": '<video><source src="https://live.example/x.m3u8"></video>'})
    calls = []

    def get_text(*args, **kwargs):
        calls.append(kwargs)
        return '<video><source src="https://live.example/x.m3u8"></video>'

    cont._http.get_text = get_text
    context = StreamBudget(time.monotonic() + 0.2)
    remaining_before = context.remaining()
    assert cont._fetch_play_url_candidates_once(
        src, "https://www.omofuna.com/anime/demo/play/3/1.html?sid=3", context=context
    ) == ["https://live.example/x.m3u8"]
    assert calls and calls[0]["timeout"] <= remaining_before
    assert calls[0]["retries"] == 0


def test_http_chain_uses_context_remaining_timeout_without_deadline():
    src = source()
    cont = _content({})
    calls = []
    cont._http.get_text = lambda *args, **kwargs: calls.append(kwargs) or "https://cdn.example/live.m3u8"
    context = type(
        "BudgetContext",
        (),
        {
            "deadline": None,
            "check": lambda self: self,
            "remaining": lambda self: 0.2,
        },
    )()
    chain = [
        {"regex": r"(https://cdn.example/player)", "name": "next", "fetch": True},
        {"regex": r"(https://cdn.example/live.m3u8)"},
    ]


    result = cont._fetch_http_chain(
        src,
        "https://www.omofuna.com/play/1.html",
        '<div>https://cdn.example/player</div>',
        chain,
        context=context,
    )

    assert result == "https://cdn.example/live.m3u8"
    assert calls[0]["timeout"] <= 0.2
    assert calls[0]["retries"] == 0


def test_http_chain_does_not_request_with_exhausted_context():
    src = source()
    cont = _content({})
    calls = []
    cont._http.get_text = lambda *args, **kwargs: calls.append(kwargs)
    context = type(
        "ExpiredContext",
        (),
        {
            "deadline": None,
            "check": lambda self: self,
            "remaining": lambda self: 0.0,
        },
    )()

    with pytest.raises(TimeoutError):
        cont._fetch_http_chain(
            src,
            "https://www.omofuna.com/play/1.html",
            '<div>https://cdn.example/player</div>',
            [{"regex": r"(https://cdn.example/player)", "fetch": True}],
            context=context,
        )
    assert calls == []


def test_play_url_probe_uses_context_remaining_without_deadline(monkeypatch):
    import requests

    src = source()
    cont = _content({})
    calls = []

    class _Response:
        status_code = 200

        class _Raw:
            @staticmethod
            def read(size):
                return b"stream"

        raw = _Raw()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: calls.append(kwargs) or _Response())
    monkeypatch.setattr(Content, "_timeout", lambda self, source: 10.0)
    context = type(
        "BudgetContext",
        (),
        {
            "deadline": None,
            "check": lambda self: self,
            "remaining": lambda self: 0.25,
        },
    )()

    assert cont._play_url_probe(src, "https://cdn.example/live.m3u8", context=context)
    assert calls[0]["timeout"] <= 0.25


def test_play_url_probe_does_not_request_with_exhausted_context(monkeypatch):
    import requests
    from framework.stream_resolver import StreamCancelledError

    src = source()
    cont = _content({})
    calls = []
    context = type(
        "ExpiredContext",
        (),
        {
            "deadline": None,
            "check": lambda self: (_ for _ in ()).throw(StreamCancelledError("expired")),
            "remaining": lambda self: 0.0,
        },
    )()
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: calls.append(kwargs))

    with pytest.raises(StreamCancelledError):
        cont._play_url_probe(src, "https://cdn.example/live.m3u8", context=context)
    assert calls == []


def test_playerconfig_cancellation_is_not_swallowed():
    from framework.stream_resolver import StreamCancelledError

    src = source()
    src.raw["endpoints"]["content"]["episode"]["source_switch"]["playerconfig_url"] = "/player.js"
    cont = _content({})
    context = StreamBudget()
    context.cancel()

    with pytest.raises(StreamCancelledError):
        cont._parse_source_list(src, _DETAIL_HTML, context=context)


def test_source_switch_cancellation_stops_before_next_probe(monkeypatch):
    from framework.stream_resolver import StreamCancelledError

    src = probe_enabled(source())
    cont = _content({})
    context = StreamBudget()
    probes = []

    monkeypatch.setattr(
        Content,
        "_fetch_play_url_candidates_once",
        lambda self, source, url, context=None: [
            "https://cdn.example/one.m3u8", "https://cdn.example/two.m3u8"
        ],
    )

    def probe(self, source, url, context=None):
        probes.append(url)
        context.cancel()
        return False

    monkeypatch.setattr(Content, "_play_url_probe", probe)
    with pytest.raises(StreamCancelledError):
        cont._fetch_play_url_auto_switch(
            src, "https://www.omofuna.com/anime/demo/play/3/1.html?sid=3", context=context
        )
    assert probes == ["https://cdn.example/one.m3u8"]


def test_source_switch_cancellation_from_line_builder_is_not_swallowed(monkeypatch):
    from framework.stream_resolver import StreamCancelledError

    src = source()
    cont = _content({})
    context = StreamBudget()
    monkeypatch.setattr(
        Content,
        "_fetch_play_url_candidates_once",
        lambda self, source, url, context=None: [],
    )
    monkeypatch.setattr(
        Content,
        "_switch_candidate_episode_urls",
        lambda *args, **kwargs: (_ for _ in ()).throw(StreamCancelledError("cancelled")),
    )

    with pytest.raises(StreamCancelledError):
        cont._fetch_play_url_auto_switch(
            src, "https://www.omofuna.com/anime/demo/play/3/1.html?sid=3", context=context
        )


def test_play_url_probe_reraises_cancellation_after_http_response(monkeypatch):
    from framework.stream_resolver import StreamCancelledError

    src = source()
    cont = _content({})
    context = StreamBudget()

    class _Raw:
        def read(self, size):
            context.cancel()
            return b"stream"

    class _Response:
        status_code = 200
        raw = _Raw()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: _Response())
    with pytest.raises(StreamCancelledError):
        cont._play_url_probe(src, "https://cdn.example/live.m3u8", context=context)


def test_ytdlp_cancellation_does_not_start_single_stream_fallback(monkeypatch):
    from framework.stream_resolver import StreamCancelledError

    src = source()
    cont = _content({})
    calls = []

    class _Ytdlp:
        def fetch_streams(self, *args, **kwargs):
            calls.append(kwargs.get("fmt"))
            raise StreamCancelledError("cancelled")

    cont._ytdlp = _Ytdlp()
    with pytest.raises(StreamCancelledError):
        cont._fetch_streams_ytdlp(src, "https://video.example/watch/1", {}, context=StreamBudget())
    assert calls == ["best"]


def test_http_chain_cancellation_is_not_wrapped_as_candidate_failure(monkeypatch):
    from framework.stream_resolver import StreamCancelledError

    src = source()
    cont = _content({})
    context = StreamBudget()
    monkeypatch.setattr(
        cont._http,
        "get_text",
        lambda *args, **kwargs: context.cancel() or "next",
    )
    with pytest.raises(StreamCancelledError):
        cont._fetch_http_chain(
            src,
            "https://www.omofuna.com/play/1.html",
            '<div>https://cdn.example/player</div>',
            [{"regex": r"(https://cdn\.example/player)", "name": "next", "fetch": True}],
            context=context,
        )


@pytest.mark.parametrize("cancel_sid", [None, "1", "2"])
def test_switch_builder_preserves_context_and_stops_after_cancel(monkeypatch, cancel_sid):
    from types import SimpleNamespace

    cont = _content({})
    budget = StreamBudget()
    calls = []
    monkeypatch.setattr(cont, "_detail_url_from_episode", lambda *args: "detail")
    monkeypatch.setattr(cont, "_episode_index_in_chapters", lambda *args: 0)

    def switch(source, url, sid, **kwargs):
        calls.append((sid, kwargs.get("context")))
        if sid == cancel_sid:
            budget.cancel()
        return SimpleNamespace(source_list=[{"sid": "2"}, {"sid": "3"}]), [SimpleNamespace(url=sid)]

    monkeypatch.setattr(cont, "switch_source", switch)
    if cancel_sid:
        with pytest.raises(StreamCancelledError):
            cont._switch_candidate_episode_urls(source(), "episode?sid=1", context=budget)
    else:
        assert cont._switch_candidate_episode_urls(source(), "episode?sid=1", context=budget) == ["2", "3"]
    expected = {None: ["1", "2", "3"], "1": ["1"], "2": ["1", "2"]}[cancel_sid]
    assert calls == [(sid, budget) for sid in expected]


def test_stream_context_reaches_all_video_stage_boundaries():
    expected = {
        "switch_source": Content.switch_source,
        "_fetch_play_url_once": Content._fetch_play_url_once,
        "_fetch_http_chain": Content._fetch_http_chain,
        "_fetch_episode_api": Content._fetch_episode_api,
        "_fetch_streams_ytdlp": Content._fetch_streams_ytdlp,
        "_fetch_play_url_auto_switch": Content._fetch_play_url_auto_switch,
    }
    for name, method in expected.items():
        assert "context" in inspect.signature(method).parameters, name


def test_slow_playwright_result_is_discarded_after_budget_expires(monkeypatch):
    src = source()
    src.raw["endpoints"]["content"]["episode"]["play_url"] = {"render": "playwright"}
    cont = _content({})
    seen = []

    def slow_render(*args, context=None, **kwargs):
        seen.append(context)
        time.sleep(0.05)
        return "https://live.example/late.m3u8"

    import framework.playwright_helper as helper

    monkeypatch.setattr(helper, "fetch_rendered_video_sync", slow_render)
    context = StreamBudget(time.monotonic() + 0.01)
    with pytest.raises(TimeoutError):
        cont._fetch_play_url_once(src, "https://video.example/watch/1", context=context)

    # 渲染**故意不接** context：慢站光 iframe 解析站就要 8~12s，外层预算常被
    # 前面的 HTTP 解析吃掉，带进来必然超时（实测带 context 68.9s 失败、不带
    # 3.5s 成功）。渲染只用自己的 timeout_ms，回来后再由外层判定预算。
    assert seen == [None]


def test_expired_detail_context_does_not_write_detail_cache():
    src = source()
    src.raw["api_endpoints"] = {"detail": {"url": "/api/{id}"}}
    cont = _content({})
    context = StreamBudget(time.monotonic() - 1)
    with pytest.raises(TimeoutError):
        cont.fetch_detail(src, "https://video.example/watch/1", context=context)
    assert cont._cache_get_detail("detail:omofuna:https://video.example/watch/1") is None


# ---------------------------------------------------------------------- #
# D6：死线路黑名单 TTL / 播放 URL 探测超时 可配置化
# ---------------------------------------------------------------------- #
def _with_runtime(cont, settings):
    cont._health_reporter = type("_Runtime", (), {"_runtime": settings})()
    return cont


def _run_auto_switch_with_dead(monkeypatch, cont, src, episode_url, candidate_calls):
    """把 auto_switch 的候选取流替换为纪实 spy：记录待抓取线路 URL。"""
    def candidate_once(self, source, episode_url, deadline=None):
        candidate_calls.append(episode_url)
        raise RuntimeError("boom")

    monkeypatch.setattr(Content, "_switch_candidate_episode_urls", lambda *a, **k: [])
    monkeypatch.setattr(Content, "_play_url_probe", lambda *a, **k: True)
    monkeypatch.setattr(Content, "_fetch_play_url_candidates_once", candidate_once)
    with pytest.raises(Exception, match="全部线路取流失败"):
        cont._fetch_play_url_auto_switch(src, episode_url)


def test_dead_line_ttl_default_prunes_after_600s(monkeypatch):
    """死线路默认 TTL 600s 惰性过期：超过后不再跳过，自动恢复尝试。"""
    src = source()
    cont = _content({})
    episode_url = "https://www.omofuna.com/anime/demo/play/3/1.html"
    key = cont._episode_key(episode_url)
    cont._dead_episode_lines[src.source_id] = {key: time.monotonic() - 700}
    calls = []
    _run_auto_switch_with_dead(monkeypatch, cont, src, episode_url, calls)
    assert calls == [episode_url]  # 过期死线路已回源重抓


def test_dead_line_ttl_reads_runtime_setting_extended(monkeypatch):
    """sources_runtime.dead_line_ttl_seconds 可延长黑名单（站恢复困难场景）。"""
    src = source()
    cont = _with_runtime(_content({}), {"sources_runtime": {"dead_line_ttl_seconds": 1200}})
    episode_url = "https://www.omofuna.com/anime/demo/play/3/1.html"
    key = cont._episode_key(episode_url)
    cont._dead_episode_lines[src.source_id] = {key: time.monotonic() - 700}
    calls = []
    _run_auto_switch_with_dead(monkeypatch, cont, src, episode_url, calls)
    assert calls == []  # 700s < 1200s：仍在黑名单 → 跳过


def test_dead_line_ttl_clamped_to_min_30s(monkeypatch):
    """TTL 越界钳制到 [30, 3600]：过小的配置不会让死线路瞬间复活。"""
    src = source()
    cont = _with_runtime(_content({}), {"sources_runtime": {"dead_line_ttl_seconds": 5}})
    episode_url = "https://www.omofuna.com/anime/demo/play/3/1.html"
    key = cont._episode_key(episode_url)
    cont._dead_episode_lines[src.source_id] = {key: time.monotonic() - 20}
    calls = []
    _run_auto_switch_with_dead(monkeypatch, cont, src, episode_url, calls)
    assert calls == []  # 钳制到 30s：20s 内仍视为死线路


def test_play_url_probe_timeout_cap_reads_setting_and_clamps(monkeypatch):
    import requests

    src = source()
    cont = _content({})
    calls = []

    class _Response:
        status_code = 200

        class _Raw:
            @staticmethod
            def read(size):
                return b"stream"

        raw = _Raw()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def get(*a, **k):
        calls.append(k)
        return _Response()

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(Content, "_timeout", lambda self, source: 30.0)

    # 默认上限 5s：源配置 30s 被压到 5s
    assert cont._play_url_probe(src, "https://cdn.example/live.m3u8", deadline=time.monotonic() + 60)
    assert calls and calls[0]["timeout"] == 5.0

    # 显式 1s → 1s；越界负数 → 钳制到下限 1s
    for raw_cap, expect in ((1, 1.0), (-2, 1.0)):
        calls.clear()
        runtime = type("_Runtime", (), {
            "_runtime": {"sources_runtime": {"play_url_probe_timeout_cap": raw_cap}}
        })()
        cont._health_reporter = runtime
        assert cont._play_url_probe(src, "https://cdn.example/live.m3u8", deadline=time.monotonic() + 60)
        assert calls and calls[0]["timeout"] == expect
