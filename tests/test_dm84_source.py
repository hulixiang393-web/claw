# -*- coding: utf-8 -*-
"""动漫巴士（dm84.tv）源离线单元测试。

fixture 全部取自站点真实 DOM（2026-10-02 抓取）：
- 列表卡片：li > div.item > a.cover[data-bg] + a.title + span.desc
- 详情页：h1.v_title + p.v_desc（meta+类型链接）+ #intro（导演/主演/剧情）
  + 分集容器 ul.play_list（注意是 ul 不是 div）
- 搜索与列表同构（/s----------.html?wd=kw），分页 /list-1-2.html
"""
import json
import os
import sys
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.decrypter import Decrypter  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.parser import Parser  # noqa: E402
from framework.search import Search  # noqa: E402


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def source():
    with open(os.path.join(ROOT, "sources", "dm84.json"), encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), os.path.join(ROOT, "sources", "dm84.json"))


# 真实列表卡片（list-1.html / 搜索结果页同构）
_LIST_CARD = (
    '<li><div class="item">'
    '<a href="/v/4183.html" class="cover lazy" '
    'data-bg="https://puui.qpic.cn/vcover_vt_pic/0/mzc00200xf3rir61687481062678/260" '
    'title="斗罗大陆2：绝世唐门在线观看">斗罗大陆2：绝世唐门在线观看</a>'
    '<a class="title" href="/v/4183.html" title="斗罗大陆2：绝世唐门">斗罗大陆2：绝世唐门</a>'
    '<span class="desc">第173话</span></div></li>'
)

# 真实详情页（/v/28.html 裁剪）
_DETAIL_HTML = """<html><head>
<meta property="og:image" content="https://puui.qpic.cn/vcover_vt_pic/0/mzc00200ilydv1a1589510037678/0" />
</head><body>
<div class="card v_info mb20"><h1 class="v_title"><a href="/v/28.html" title="无上神帝在线播放">无上神帝</a></h1>
<p class="v_desc"><span class="desc">第645话</span><em class="hr">|</em>2020<em class="hr">|</em>大陆<em class="hr">|</em><a href="/show-1---%E5%A5%87%E5%B9%BB---.html">奇幻</a>,<a href="/show-1---%E7%8E%84%E5%B9%BB---.html">玄幻</a></p>
<div class="v_content" id="v_content"><div class="cover"><img src="https://puui.qpic.cn/vcover_vt_pic/0/mzc00200ilydv1a1589510037678/0" /></div>
<div class="content" id="intro_content"><div id="intro"><p>导演：项方</p><p>主演：牧云,秦梦瑶</p><p>剧情：万年之前，仙王牧云因持有诛仙图而遭人暗算。</p></div></div></div>
</div>
<div class="card mb20"><ul class="tab_control play_from"><li class="current">线路1</li></ul>
<div class="tab_content" id="play_list"><div class="play_list_sort" id="play_list_sort">⇅ 正序/倒序</div>
<ul class="play_list current"><li><a href="/p/28-1-645.html">645</a></li><li><a href="/p/28-1-644.html">644</a></li><li><a href="/p/28-1-643.html">643</a></li></ul></div></div>
</body></html>"""


class _FakeHttp:
    """离线假 HttpClient：按 URL 特征返回页面片段。"""

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


def test_dm84_config_and_categories():
    src = source()
    assert src.source_id == "dm84"
    assert src.content_type == "video"
    categories = src.get_discovery_config()["list_item"]["categories"]
    titles = [item["title"] for item in categories]
    assert titles[:5] == ["全部", "国漫", "日漫", "欧美动漫", "电影"]
    assert "科幻" in titles and "搞笑" in titles


def test_dm84_list_items_from_real_card():
    """列表卡片：根为 li(div.item)，标题取 a.title（无「在线观看」后缀），
    封面在 a.cover 的 data-bg（不是 img）。"""
    src = source()
    wi = src.get_discovery_config()["works_list_item"]
    doc = Parser().parse(_LIST_CARD)
    items = Parser().parse_items(doc, wi["root_selector"], wi["fields"], src.base_url)
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "斗罗大陆2：绝世唐门"
    assert "在线观看" not in it["title"]
    assert it["url"] == "https://dm84.tv/v/4183.html"
    assert it["cover"].startswith("https://puui.qpic.cn/")
    assert it["update"] == "第173话"


def test_dm84_detail_chapters_from_real_play_list():
    """详情页真实结构：分集在 ul.play_list（非 div），fetch_detail 必须给出分集；
    同时校验标题/封面/简介/导演/主演/标签字段。"""
    src = source()
    http = _FakeHttp({"/v/28.html": _DETAIL_HTML})
    detail = Content(http, Parser(), _Checker(), decrypter=Decrypter(http)).fetch_detail(
        src, "/v/28.html"
    )
    assert detail.title == "无上神帝"
    assert detail.cover.startswith("https://puui.qpic.cn/")
    assert "万年之前" in detail.summary
    assert detail.author == "项方"
    assert detail.actor == "牧云,秦梦瑶"
    assert detail.tags == ["奇幻", "玄幻"]
    urls = [ch.url for ch in detail.chapters]
    assert len(urls) == 3
    # 框架按集号自动升序（HTML 倒序 645→643 会归一为 643→645）
    assert urls == [
        "https://dm84.tv/p/28-1-643.html",
        "https://dm84.tv/p/28-1-644.html",
        "https://dm84.tv/p/28-1-645.html",
    ]
    assert [ch.title for ch in detail.chapters] == ["643", "644", "645"]
    # dm84 单线路站：无 source_switch 配置 → source_list 为空（正常）
    assert detail.source_list == []


def test_dm84_pagination_page_placeholder():
    """分类页分页：分类 URL 用 {page} 模板（真实站 /list-1-1.html=首页、
    /list-1-2.html=下一页、尾页 -10.html；标签页 /s-…--{n}.html 同规则）。"""
    src = source()
    cats = src.get_discovery_config()["list_item"]["categories"]
    assert cats[1]["url"] == "/list-1-{page}.html"
    assert cats[5]["url"] == "/s-----%E7%A7%91%E5%B9%BB-----{page}.html"
    disc = Discovery(_FakeHttp(), Parser(), _Checker())
    assert disc._build_page_url(src, cats[1]["url"], 1) == "/list-1-1.html"
    assert disc._build_page_url(src, cats[1]["url"], 2) == "/list-1-2.html"
    assert disc._build_page_url(src, cats[5]["url"], 2) == "/s-----%E7%A7%91%E5%B9%BB-----2.html"


def test_dm84_search_url_and_item():
    """搜索：表单 GET /s----------.html?wd=kw（路径式 /s-{kw}---------1.html 不出结果）；
    结果卡片与列表同构；单页搜索（站内分页路径式不可用）。"""
    src = source()
    scfg = src.get_search_config()
    assert scfg["paginator"]["url_template"] == "/s----------.html?wd={keyword}"
    url = Search._build_page_url(
        source=src,
        base_url=scfg.get("base_url") or "/",
        keyword="无上神帝",
        kw_param=scfg.get("keyword_param") or "wd",
        page=1,
        page_param="page",
        url_template=scfg["paginator"]["url_template"],
    )
    assert url == "https://dm84.tv/s----------.html?wd=" + quote("无上神帝")
    item = scfg["item"]
    doc = Parser().parse(_LIST_CARD)
    items = Parser().parse_items(doc, item["root_selector"], item["fields"], src.base_url)
    assert items[0]["title"] == "斗罗大陆2：绝世唐门"
    assert items[0]["cover"].startswith("https://puui.qpic.cn/")
    assert src.raw["constraints"]["search"]["max_pages"] == 1


def test_dm84_search_retries_after_rate_limit_page(monkeypatch):
    """dm84 搜索端点限流（距上次 /s- 请求 <10s 回「系统提示」页 → AntiScrapeError）
    需等待 11s 后重试一次，而不是直接返回 0 条。"""
    from framework.http import AntiScrapeError

    src = source()
    calls = {"n": 0}

    class _FlakyHttp(_FakeHttp):
        def get_text(self, url, *a, **k):
            if "/s-" in url:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise AntiScrapeError(f"反爬特征响应 {url}")
                return "<html><body><ul>" + _LIST_CARD + "</ul></body></html>"
            return ""

    sleeps = []
    monkeypatch.setattr("framework.search.time.sleep", lambda s: sleeps.append(s))
    res = Search(_FlakyHttp(), Parser()).search_one(src, "无上神帝")
    assert [r.title for r in res] == ["斗罗大陆2：绝世唐门"]
    assert calls["n"] == 2
    assert sleeps == [11]


def test_dm84_player_page_contains_stable_iframe_rule():
    play = source().raw["endpoints"]["content"]["episode"]["play_url"]
    assert "hhjx.hhplayer.com" in play["selector"]["css"]
    assert source().raw["endpoints"]["content"]["episode"]["render_config"]["timeout_ms"] >= 45000


def test_dm84_render_delay_bounded():
    """播放页渲染的固定等待必须有界：hhplayer 实测 delay=0/1000/2000 均可稳定
    出流（video src 为 artplayer 直填），7000ms 死等纯属浪费（11.2s → ~4s）。"""
    rc = source().raw["endpoints"]["content"]["episode"]["render_config"]
    assert rc["extra_delay_ms"] <= 2500
