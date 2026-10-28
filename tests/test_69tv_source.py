"""69TV 源离线回归：分类结构镜像原站频道 + API 详情/取流/搜索契约。

站点结构证据（2026-10-26）：
- /movies/{频道id}?page=N 为 SSR 列表（每页 30 条），频道来自
  /api/vod/channelAndLabel 的 channelLists（最新影片 -1 … 绿帽NTR 1）。
- 详情/取流/搜索走独立 API 域名，POST JSON；/api/vod/play 的 GET 返回 405，
  必须 POST 才能拿到带 token/expires 的 m3u8。
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import load_source  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.parser import Parser  # noqa: E402
from framework.search import Search  # noqa: E402

SRC = os.path.join(os.path.dirname(__file__), "..", "sources", "69tv.json")

LIST_HTML = """
<html><body>
<div class="grid grid-cols-2 gap-3 sm:grid-cols-3">
  <div>
    <div class="group cursor-pointer h-full relative">
      <div class="aspect-3/2 bg-zinc-200 rounded-md overflow-hidden relative">
        <a href="/movies/content/3604" class="block h-full w-full">
          <img src="https://69FUNDMIT.b-cdn.net/pic/2026/7/1026/MIDA-814C.jpg" alt="A">
          <div class="text-white text-xs py-1 px-2 bg-zinc-900 rounded-sm bottom-2 right-2 absolute z-20">2026/10/26</div>
        </a>
      </div>
      <div class="mt-1"><h3 class="text-zinc-800 font-bold line-clamp-2">第一支影片</h3></div>
    </div>
  </div>
  <div>
    <div class="group cursor-pointer h-full relative">
      <div class="aspect-3/2 bg-zinc-200 rounded-md overflow-hidden relative">
        <a href="/movies/content/3603" class="block h-full w-full">
          <img src="https://69FUNDMIT.b-cdn.net/pic/2026/7/1026/MIDA-753C.jpg" alt="B">
          <div class="text-white text-xs py-1 px-2 bg-zinc-900 rounded-sm bottom-2 right-2 absolute z-20">2026/10/25</div>
        </a>
      </div>
      <div class="mt-1"><h3 class="text-zinc-800 font-bold line-clamp-2">第二支影片</h3></div>
    </div>
  </div>
</div>
</body></html>
"""

DETAIL_JSON = {
    "code": 200,
    "data": {
        "id": "3604",
        "vodName": "示例影片标题",
        "vodPic": "https://69FUNDMIT.b-cdn.net/pic/2026/7/1026/MIDA-814C.jpg",
        "createTime": "2026/10/26",
        "vodContent": "示例简介正文",
        "actors": [{"id": "349", "actorName": "神宫寺奈绪"}],
        "labels": [{"id": "19", "labelName": "中出"}, {"id": "26", "labelName": "痴女"}],
    },
}

PLAY_JSON = {
    "code": 200,
    "data": {
        "vodPlayUrl": "https://69FUNDMIT.b-cdn.net/movie/2026/7/1026/MIDA-814C/index.m3u8?token=t&expires=1",
        "timeout": "1",
    },
}

SEARCH_JSON = {
    "code": 200,
    "data": {
        "total": 2,
        "count": 2,
        "lists": [
            {"vodId": "3602", "vodName": "搜索结果一", "vodPic": "https://cdn/1.jpg", "createTime": "2026/10/26"},
            {"vodId": "3601", "vodName": "搜索结果二", "vodPic": "https://cdn/2.jpg", "createTime": "2026/10/25"},
        ],
    },
}


class _Http:
    defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def __init__(self, html=""):
        self.html = html
        self.posts = []
        self.gets = []

    def get_text(self, url, headers=None, **kwargs):
        self.gets.append(url)
        return self.html

    def post_json(self, url, json_body=None, **kwargs):
        self.posts.append((url, json_body))
        if url.endswith("/api/vod/content"):
            return DETAIL_JSON
        if url.endswith("/api/vod/play"):
            return PLAY_JSON
        return SEARCH_JSON


class _Checker:
    pass


def _source():
    source = load_source(SRC)
    source.raw.setdefault("diagnostics", {})["selfcheck"] = {"strategy": "off"}
    return source


def test_categories_mirror_original_channels():
    source = _source()
    cats = Discovery(_Http(), Parser(), _Checker()).list_categories(source)
    assert [c.title for c in cats] == [
        "最新影片", "免费试看", "自拍", "动画", "欧美", "Fc2", "黑料",
        "无码", "无码中文", "素人", "有码中文", "有码", "绿帽NTR",
    ]
    assert cats[0].url == "/movies/-1?page={page}"
    assert cats[6].url == "/movies/7?page={page}"
    assert source.raw["endpoints"]["discovery"]["list_url"] == "/movies/-1?page={page}"


@pytest.mark.skipif(not Parser()._lxml, reason="lxml 才能跑选择器回归")
def test_list_page_parses_cards():
    source = _source()
    works = Discovery(_Http(LIST_HTML), Parser(), _Checker()).list_works(
        source, "/movies/-1?page={page}", 1
    )
    assert [w.title for w in works] == ["第一支影片", "第二支影片"]
    assert works[0].url == "https://69tv.fun/movies/content/3604"
    assert works[0].cover.endswith("MIDA-814C.jpg")
    assert works[0].update == "2026/10/26"


def test_detail_uses_post_api_with_tags_and_actor():
    source = _source()
    http = _Http()
    detail = Content(http, Parser(), _Checker()).fetch_detail(
        source, "https://69tv.fun/movies/content/3604"
    )
    assert http.posts == [
        ("https://6t9vcb.69tv.fun/api/vod/content", {"vodId": "3604"})
    ]
    assert detail.title == "示例影片标题"
    assert detail.tags == ["中出", "痴女"]
    assert detail.actor == "神宫寺奈绪"
    assert detail.status == "2026/10/26"
    # 单集视频：详情接口不给章节时按 single_chapter 补唯一分集
    assert len(detail.chapters) == 1
    assert detail.chapters[0].url == "https://69tv.fun/movies/content/3604"


def test_episode_posts_vod_id_and_returns_hls():
    source = _source()
    http = _Http()
    url = Content(http, Parser(), _Checker()).fetch_video_episode(
        source, "https://69tv.fun/movies/content/3604"
    )
    assert url.startswith("https://69FUNDMIT.b-cdn.net/") and ".m3u8" in url
    assert ("https://6t9vcb.69tv.fun/api/vod/play", {"vodId": "3604"}) in http.posts


def test_search_posts_keyword_and_maps_cards():
    source = _source()
    http = _Http()
    results = Search(http, Parser()).search_one(source, "巨乳")
    assert http.posts[0][1] == {"keyword": "巨乳", "pageNum": "1", "pageSize": 60}
    assert [r.title for r in results][:2] == ["搜索结果一", "搜索结果二"]
    assert results[0].url == "https://69tv.fun/movies/content/3602"
