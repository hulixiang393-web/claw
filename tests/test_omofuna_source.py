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
import json
import os
import sys
import time
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.decrypter import Decrypter  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.parser import Parser  # noqa: E402
from framework.search import Search, _maccms_ts_b64  # noqa: E402


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "sources", "omofuna.json")


def source():
    with open(PATH, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), PATH)


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
    assert switch["default_sid"] == "3"
    assert switch["max_switch_attempts"] == 4
    # 线路 sid 在 href="#playlistN"（站无 data-sid 属性）
    assert switch["list_attr"] == "href"
    assert switch["sid_regex"] == r"playlist(\d+)"
    # 分集 href 是 /anime/{id}/play/{sid}/{ep}.html（框架缺省 /tv/ 会全部过滤）
    assert switch["ep_href_contains"] == "/play/"


def test_omofuna_episode_and_player_rules():
    src = source()
    episode = src.raw["endpoints"]["content"]["episode"]
    assert "player_aaaa" in episode["source_switch"]["play_regex"]
    assert episode["list"]["fields"]["url"]["attr"] == "href"
    assert src.raw["decryption"]["targets"]["video_url"]["strategy"] == "maccms_url"


def test_omofuna_maccms_url_decodes_public_player_value():
    url = "https://vip.dytt-tvs.com/20261002/39442_f477a6f0/index.m3u8"
    cipher = base64.b64encode(quote(url, safe="").encode("ascii")).decode("ascii")
    assert Decrypter(None)._maccms_url(cipher) == url
    assert Decrypter(None)._maccms_url(quote(url, safe="")) == url


def test_omofuna_episode_selector_parses_links():
    src = source()
    cfg = src.raw["endpoints"]["content"]["episode"]["list"]
    html = "<ul class='play-list-content'><li><a href='/anime/abc/play/3/645.html'>第645集</a></li></ul>"
    items = Parser().parse_items(Parser().parse(html), cfg["root_selector"], cfg["fields"], src.base_url)
    assert items == [{"title": "第645集", "url": "https://www.omofuna.com/anime/abc/play/3/645.html"}]


def test_omofuna_detail_channels_and_chapters():
    """真实详情页：四线路 source_list（sid 来自 #playlistN href）+ 默认 sid=3 分集。"""
    src = source()
    detail = _content({"/anime/": _DETAIL_HTML}).fetch_detail(
        src, "/anime/e7f12f9fc5b567f379a2cc2b.html"
    )
    assert detail.title == "无上神帝"
    assert detail.cover == "https://as.cfhls.top/upload/vod/20230723-6/420b20f28b350c6654be8034f790be81.jpg"
    assert "残魂沉睡万年之后觉醒" in detail.summary
    assert detail.status == "645集"
    assert detail.author == "项方"
    # tags 只取「类型：」那一个 p（另有 更新 hidden-xs p，选择器过宽会混入空串）
    assert detail.tags == ["动画,热血,战斗,剧情,奇幻,玄幻,动作"]
    # 四条线路：天堂3 / 精品1 / 暴风2 / 量子4（页签顺序）
    sids = [s["sid"] for s in detail.source_list]
    assert sids == ["3", "1", "2", "4"]
    assert detail.source_list[0]["name"].startswith("天堂")
    # 默认 sid=3 分集（ep_href_contains=/play/ 过滤后仍非空），URL 带 sid 参数
    urls = [ch.url for ch in detail.chapters]
    assert len(urls) == 2
    assert urls[0] == "https://www.omofuna.com/anime/e7f12f9fc5b567f379a2cc2b/play/3/1.html?sid=3"
    assert [ch.title for ch in detail.chapters] == ["第01集", "第02集"]


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
