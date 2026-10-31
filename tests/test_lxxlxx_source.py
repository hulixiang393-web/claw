# -*- coding: utf-8 -*-
"""lxxlxx（LXXLXX 标签站）源离线单元测试。

全部基于真实页面快照（tests/fixtures/lxxlxx），不发网络请求。覆盖：

- 源配置：$id/$type/base_url、page_placeholder 分页位、单章视频、mp4 媒体
- 分页 URL 构造：第 1 页 /tags/X//、第 N 页 /tags/X/N/（站点第 1 页无数字，
  缺尾斜杠的 /tags/X/N 会被 301 到 http 并被阻断，故必须保持尾斜杠）
- 分类：32 个标签/榜单入口，全部是 // 结尾的标签页
- 列表解析：div.thumb → u 标题 / a.href / img 封面 / t 时长
- 搜索：/search/?key= 结果卡片与列表同构
- 详情：h1 标题、og:image 封面、description 简介、video-tags 后的分类链接、
  video-info 首个 b（时长）
- 取流三段链：/vjs/{vid}.js → m4-player 脚本 → <source src=...mp4>；
  链尾不做额外请求（mp4 直链直接返回）
"""

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.http import NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402
from framework.search import Search  # noqa: E402

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "lxxlxx")
_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources", "lxxlxx.json"
)
_BASE = "https://www.lxxlxx.com"
_DETAIL = "/video/Lesbian-chinese-flight-attendant.html"
_STAGE2_HOST = "m4-player-a-lx-en.gotojs.xyz"
_EXPECTED_MP4 = (
    "https://m4-a-mail-en2p.gotojs.xyz/b_rE9bq7/move/202608/18908_piS9d.mp4"
)


class _FakeChecker:
    """结构自检桩：指定 URL 为空 → 不起后台检查线程。"""

    def _selfcheck_url(self, source):
        return ""

    def check(self, source, url):
        return True


class _FakeHttp:
    """离线 HttpClient：按 URL 特征返回对应快照。"""

    cache = None
    _sleeper = staticmethod(lambda *_a: None)

    def __init__(self):
        self.defaults = NetworkDefaults(timeout=10, retries=0, interval_ms=0)
        self.calls = []

    def get_text(self, url, headers=None, timeout=None, retries=None,
                 interval_ms=None, encoding=None, proxy_pool=None, direct=False,
                 method=None, data=None, json=None):
        self.calls.append(url)
        if "/tags/Lesbians/2/" in url:
            name = "list_p2.html"
        elif "/tags/Lesbians/" in url:
            name = "list_p1.html"
        elif "/search/" in url:
            name = "search.html"
        elif "/vjs/" in url and "gotojs" not in url:
            name = "vjs1.js"
        elif _STAGE2_HOST in url:
            name = "vjs2.js"
        elif "/video/" in url:
            name = "detail.html"
        else:  # 未预期的请求：直接暴露，避免测试偷偷联网
            raise AssertionError("unexpected fetch: %s" % url)
        with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
            return f.read()

    def close(self):
        pass


def _source() -> SourceConfig:
    with open(_SOURCE_PATH, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), _SOURCE_PATH)


def _parser() -> Parser:
    return Parser()


def _discovery(http=None):
    http = http or _FakeHttp()
    return Discovery(http, _parser(), _FakeChecker())


def _content(http=None):
    http = http or _FakeHttp()
    return Content(http, _parser(), _FakeChecker())


# --------------------------------------------------------------------------- #
# 源配置
# --------------------------------------------------------------------------- #
def test_source_config_loads():
    src = _source()
    assert src.source_id == "lxxlxx"
    assert src.content_type == "video"
    assert src.base_url == _BASE
    assert (src.raw.get("$metadata") or {}).get("adult") is True
    assert src.transports().get("charset") == "utf-8"
    assert src.get_detail_config() is not None


def test_paginator_page_placeholder():
    src = _source()
    disc = _discovery()
    paginator = src.get_discovery_config().get("list_paginator") or {}
    assert paginator.get("type") == "increment"
    # 第 1 页保持原样（站点第 1 页无数字），第 N 页在末尾两个斜杠之间插入页码
    assert disc._build_page_url(src, "/tags/Lesbians//", 1) == "/tags/Lesbians//"
    assert disc._build_page_url(src, "/tags/Lesbians//", 2) == "/tags/Lesbians/2/"
    assert disc._build_page_url(src, "/tags/Lesbians//", 21) == "/tags/Lesbians/21/"


def test_single_chapter_and_mp4_media():
    src = _source()
    episode = ((src.raw["endpoints"]["content"]["episode"]) or {})
    assert episode.get("single_chapter") is True
    chain = episode["play_url"]["http_chain"]
    assert [step["name"] for step in chain] == ["vid", "stage2", "stream"]
    assert chain[-1].get("fetch") is False
    assert (src.raw.get("media") or {}).get("format") == "mp4"


# --------------------------------------------------------------------------- #
# 发现
# --------------------------------------------------------------------------- #
def test_categories_are_tag_pages():
    src = _source()
    cats = _discovery().list_categories(src)
    assert len(cats) == 32
    assert cats[0].title.startswith("Lesbians")
    assert cats[0].url == "/tags/Lesbians//"
    assert all(c.url.endswith("//") for c in cats)
    assert "/tags/Korean//" not in {c.url for c in cats}  # 该标签站内为空


def test_list_works_page1():
    src = _source()
    works = _discovery().list_works(src, "/tags/Lesbians//", 1)
    assert len(works) == 28
    first = works[0]
    assert first.title == "Lesbian chinese flight attendant"
    assert first.url == _BASE + "/video/Lesbian-chinese-flight-attendant.html"
    assert first.cover == _BASE + "/imgs/sfiles/190/202608231907136775.jpg"
    assert first.update == "17:52"
    assert all(w.url.startswith(_BASE + "/video/") for w in works)


def test_list_works_page2():
    src = _source()
    works = _discovery().list_works(src, "/tags/Lesbians//", 2)
    assert len(works) == 28
    assert works[0].title == "Lesbians Enjoys Kissing"
    assert works[0].url.endswith("/video/Lesbians-Enjoys-Kissing.html")


# --------------------------------------------------------------------------- #
# 搜索
# --------------------------------------------------------------------------- #
def test_search_results():
    src = _source()
    http = _FakeHttp()
    results = Search(http, _parser(), _discovery(http)).search_one(src, "lesbian")
    assert len(results) >= 20
    assert all(r.url.startswith(_BASE + "/video/") for r in results)
    assert all(r.title for r in results)
    assert any("/search/" in url for url in http.calls)


# --------------------------------------------------------------------------- #
# 详情
# --------------------------------------------------------------------------- #
def test_detail_fields():
    src = _source()
    detail = _content().fetch_detail(src, _DETAIL)
    assert detail.title == "Lesbian chinese flight attendant"
    assert detail.cover == "https://img.lxxlxx.net/imgs/files/190/202608231907136775.jpg"
    assert "Lesbians" in detail.tags and "Stewardess" in detail.tags
    assert detail.status == "17:52"
    assert (detail.summary or "").startswith("Lesbian chinese flight attendant free video")
    # 单章源：详情页即唯一分集
    assert len(detail.chapters) == 1
    assert detail.chapters[0].url.endswith(_DETAIL)


# --------------------------------------------------------------------------- #
# 取流
# --------------------------------------------------------------------------- #
def test_play_url_three_stage_chain():
    src = _source()
    http = _FakeHttp()
    content = _content(http)
    play = content.fetch_video_episode(src, _DETAIL)
    assert play == _EXPECTED_MP4
    # 只请求了详情页 + 两级播放器脚本，mp4 直链不再回源（避免多一次 404/403）
    fetched = [u for u in http.calls if "/video/" in u or "gotojs" in u or "/vjs/" in u]
    assert fetched == [
        _BASE + _DETAIL,
        _BASE + "/vjs/18908.js",
        "https://" + _STAGE2_HOST + "/0510_mByje/vjs/18908.js",
    ]
