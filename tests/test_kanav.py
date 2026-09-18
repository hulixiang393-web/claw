# -*- coding: utf-8 -*-
"""KanAV（kanav.ad，MacCMS 视频站）源离线单元测试。

覆盖（全部离线，无网络请求）：
- 源配置加载关键形态（$type=video、source_switch play_regex、decryption maccms_url、
  discovery/search/detail 配置、selfcheck soft）
- 发现列表：div.video-item 条目在站点两种 HTML 变体下都能解析——
  (a) type 分类页：entry-title 是 video-item 的「兄弟」（视频模板奇偶布局），
      fallback 链走 following-sibling XPath；
  (b) 搜索页：entry-title 是 video-item 的「子级」，fallback 链走 CSS。
  标题/URL 用 entry-title > a 的 title/href（相对 URL 由框架上层绝对化），
  封面用 img.lazy data-original。
- fetch_detail 集成：h1 正则提取标题（标题：… 作者：… 串）、MacPlayer.Pic 封面
  （cover regex 对整页文本兜底）、meta description 简介（clean 去站点后缀）、
  hr-tags 块内联标签、single_chapter 唯一分集。
- Decrypter._maccms_url 兼容两种密码顺序：urlencode(base64(url))（旧标准）
  与 base64(urlencode(url))（kanav 等 unescape 型，二次 unquote），以及
  明文 URL 原样返回（round-trip 校验不误伤）。
"""
import base64
import json
import os
import sys
from urllib.parse import quote

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.decrypter import Decrypter  # noqa: E402
from framework.parser import Parser  # noqa: E402

_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources", "kanav.json"
)
_BASE = "https://kanav.ad"

# MacCMS encrypt=2：player_aaaa.url 为 base64(urlencode(url)) 的真实密文
_TRUE_URL = "https://cdn6.11yun.space/DAV3/340447/340447.m3u8"
_KANAV_CIPHER = base64.b64encode(quote(_TRUE_URL, safe="").encode("ascii")).decode("ascii")


class _FakeHttp:
    """离线假 HttpClient：按 URL 特征返回对应页面片段。"""

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


def _source() -> SourceConfig:
    with open(_SOURCE_PATH, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), _SOURCE_PATH)


def _content(pages):
    http = _FakeHttp(pages)
    return Content(
        http, Parser(), _Checker(), decrypter=Decrypter(http)
    )


def _play_snippet(title="标题：银两伊织 [各种循环/静态图像]作者：Fuen Gomi Taro发布日期：2026/09/05文件大小：228.1 MB"):
    """详情/播放页片段：h1 + meta description + MacPlayer.Pic + hr-tags 标签块。"""
    return f"""<html><body>
<h1>{title}</h1>
<meta name="description" content="[不燃ごみ太郎]銀鏡イオリ - KanAV-迅雷影视AV在线观看" />
<script>var player_aaaa={{"flag":"play","encrypt":2,"url":"{_KANAV_CIPHER}","ps":"0"}}</script>
<script>var MacPlayer={{}};MacPlayer.Pic="https://img.11yun.xyz/BDAV3/340447/340447.jpg";</script>
<div class="video-countext-tags col-md-12"><div class="hr-style hr-tags info"></div>
<a href="/index.php/vod/search.html?wd=沙滩&amp;by=time_add">沙滩</a>
<a href="/index.php/vod/search.html?wd=脚交&amp;by=time_add">脚交</a></div>
<div class="video-countext-tags col-md-12"><div class="hr-style hr-vod info"></div>
<a href="/index.php/vod/play/id/999/sid/1/nid/1.html">相关视频</a></div>
</body></html>"""


# 变体 A：type 分类页 —— entry-title 是 div.video-item 的兄弟（同列下一格）
_LIST_SIBLING = """<html><body>
<div class="col-md-3 col-sm-6 col-xs-6">
  <div class="video-item">
    <div class="featured-content-image"><a href="/index.php/vod/play/id/125150/sid/1/nid/1.html" rel="nofollow">
      <img class="lazy" data-original="https://img.11yun.xyz/BDAV3/340447/340447.jpg" /></a>
      <span class="model-view">2 over 33</span>
    </div>
  </div>
  <div class="entry-title"><a title="[不燃ごみ太郎]銀鏡イオリ" href="/index.php/vod/play/id/125150/sid/1/nid/1.html">[不燃ごみ太郎]銀鏡イオリ</a> 2026 / 09 / 17</div>
</div>
</body></html>"""

# 变体 B：搜索页 —— entry-title 是 div.video-item 的子级
_LIST_DESC = """<html><body>
<div class="col-md-3 col-sm-6 col-xs-6">
  <div class="video-item">
    <div class="featured-content-image"><a href="/index.php/vod/play/id/125135/sid/1/nid/1.html" rel="nofollow">
      <img class="lazy" data-original="https://img.11yun.xyz/JDAV1/340198/340198.jpg" /></a>
    </div>
    <div class="entry-title"><a title="[Harechippai(はれ)]ヒカリ3" href="/index.php/vod/play/id/125135/sid/1/nid/1.html">[Harechippai(はれ)]ヒカリ3</a> 2026 / 09 / 17</div>
  </div>
</div>
</body></html>"""


# 变体 C：label/hot.html 「热映影片」（全站聚合）—— entry-title 为子级，且
# <a> 无 title 属性（仅文本），标题需回退到文本提取
_LIST_HOT = """<html><body>
<div class="col-md-3 col-sm-6 col-xs-6">
  <div class="video-item">
    <div class="featured-content-image"><a href="/index.php/vod/play/id/45952/sid/1/nid/1.html" rel="nofollow">
      <img class="lazy" referrerpolicy="no-referrer" data-original="https://img.11yun.xyz/ATAV2/67504/67504.jpg" src="https://img.11yun.xyz/ATAV2/67504/67504.jpg" /></a>
      <span class="model-view-left">探花约炮</span><span class="model-view">1小时 2分钟 51秒</span>
    </div>
    <div class="entry-title"><a href="/index.php/vod/play/id/45952/sid/1/nid/1.html">小姐这里楼梯新来的，超清4K偷拍设备，极品女优出来接客，笑得真开心</a> 2024 / 02 / 26 </div>
  </div>
</div>
</body></html>"""


def _parse_items(html: str):
    cfg = (_source().get_discovery_config().get("works_list_item") or {})
    doc = Parser().parse(html)
    return Parser().parse_items(doc, cfg.get("root_selector"), cfg.get("fields"), _BASE)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_source_config_loads():
    src = _source()
    assert src.source_id == "kanav"
    assert src.content_type == "video"
    assert src.source_name.startswith("KanAV")
    assert src.base_url == _BASE
    assert src.transports().get("charset") == "utf-8"
    episode = (src.raw.get("endpoints") or {}).get("content", {}).get("episode") or {}
    assert episode.get("single_chapter") is True
    switch = episode.get("source_switch") or {}
    assert "player_aaaa" in (switch.get("play_regex") or "")
    dec = (src.raw.get("decryption") or {}).get("targets", {}).get("video_url") or {}
    assert dec.get("strategy") == "maccms_url"
    st = ((src.raw.get("diagnostics") or {}).get("selfcheck") or {}).get("strategy")
    assert st == "soft"


def test_discovery_categories():
    src = _source()
    disc = src.get_discovery_config()
    cats = (disc.get("list_item") or {}).get("categories") or []
    # 全站 14 个导航分类 + 全站热映，一个不少
    assert len(cats) == 15
    assert cats[0]["title"] == "中文字幕"
    assert cats[0]["url"] == "/index.php/vod/type/id/1/page/{page}.html"
    titles = [c["title"] for c in cats]
    for name in ("中文字幕", "日韩有码", "日韩无码", "国产AV", "流出自拍",
                 "自拍泄密", "探花约炮", "主播录制", "动漫番剧", "里番",
                 "泡面番", "Motion Anime", "3D动画", "同人作品", "热映影片（全站）"):
        assert name in titles, name
    # 默认「全部」/回退列表 = 全站热映聚合页（非动漫专锁）
    assert disc.get("works_list_url") == "/index.php/label/hot.html"
    assert disc.get("list_url") == "/index.php/label/hot.html"
    pag = disc.get("list_paginator") or {}
    assert pag.get("type") == "increment"
    assert pag.get("param") == "page"


def test_search_config():
    src = _source()
    search = src.get_search_config()
    assert search.get("base_url") == "/index.php/vod/search.html"
    assert search.get("keyword_param") == "wd"
    tpl = search.get("paginator", {}).get("url_template") or ""
    assert "{keyword}" in tpl and "{page}" in tpl


# --------------------------------------------------------------------------- #
# 发现列表两种 HTML 变体
# --------------------------------------------------------------------------- #
def test_list_parse_sibling_variant():
    items = _parse_items(_LIST_SIBLING)
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "[不燃ごみ太郎]銀鏡イオリ"
    assert it["url"] == "/index.php/vod/play/id/125150/sid/1/nid/1.html"
    assert it["cover"] == "https://img.11yun.xyz/BDAV3/340447/340447.jpg"


def test_list_parse_descendant_variant():
    items = _parse_items(_LIST_DESC)
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "[Harechippai(はれ)]ヒカリ3"
    assert it["url"] == "/index.php/vod/play/id/125135/sid/1/nid/1.html"
    assert it["cover"] == "https://img.11yun.xyz/JDAV1/340198/340198.jpg"


def test_list_parse_label_hot_titleless_anchor():
    items = _parse_items(_LIST_HOT)
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "小姐这里楼梯新来的，超清4K偷拍设备，极品女优出来接客，笑得真开心"
    assert it["url"] == "/index.php/vod/play/id/45952/sid/1/nid/1.html"
    assert it["cover"] == "https://img.11yun.xyz/ATAV2/67504/67504.jpg"


# --------------------------------------------------------------------------- #
# fetch_detail 集成（真实框架路径 + 离线假 HttpClient）
# --------------------------------------------------------------------------- #
_DETAIL_URL = "https://kanav.ad/index.php/vod/play/id/125150/sid/1/nid/1.html"


def test_fetch_detail_fields_and_single_chapter():
    c = _content({_DETAIL_URL: _play_snippet()})
    d = c.fetch_detail(_source(), _DETAIL_URL)
    assert d.title == "银两伊织 [各种循环/静态图像]"
    assert d.cover == "https://img.11yun.xyz/BDAV3/340447/340447.jpg"
    assert d.summary == "[不燃ごみ太郎]銀鏡イオリ"  # 站点后缀已清洗
    assert "沙滩" in d.tags and "脚交" in d.tags
    assert "相关视频" not in d.tags  # hr-vod 块不混入标签
    assert len(d.chapters) == 1
    assert d.chapters[0].url == _DETAIL_URL


def test_fetch_detail_title_tolerates_no_author():
    c = _content({_DETAIL_URL: _play_snippet(title="标题：某动画")})
    d = c.fetch_detail(_source(), _DETAIL_URL)
    assert d.title == "某动画"


def test_fetch_video_episode_decrypts_maccms():
    c = _content({_DETAIL_URL: _play_snippet()})
    # _fetch_play_url_once：走 source_switch(play_regex) 分支 + decryption 解密，
    # 不做真实网络探测（fetch_video_episode 会 live-probe CDN，非离线）
    play = c._fetch_play_url_once(_source(), _DETAIL_URL)
    assert play == _TRUE_URL


# --------------------------------------------------------------------------- #
# MacCMS 播放地址两种密码顺序
# --------------------------------------------------------------------------- #
def test_maccms_order_base64_of_urlencode():
    d = Decrypter(_FakeHttp())
    assert d._maccms_url(_KANAV_CIPHER) == _TRUE_URL


def test_maccms_order_urlencode_of_base64():
    d = Decrypter(_FakeHttp())
    cipher = quote(base64.b64encode(_TRUE_URL.encode("ascii")).decode("ascii"), safe="")
    assert d._maccms_url(cipher) == _TRUE_URL


def test_maccms_pure_base64():
    d = Decrypter(_FakeHttp())
    cipher = base64.b64encode(_TRUE_URL.encode("ascii")).decode("ascii")
    assert d._maccms_url(cipher) == _TRUE_URL


def test_maccms_plain_url_untouched():
    d = Decrypter(_FakeHttp())
    assert d._maccms_url(_TRUE_URL) == _TRUE_URL