# -*- coding: utf-8 -*-
"""ikanpp（www.ikanpp.com，API 聚合影视站）源离线单元测试。

覆盖（全部离线，无网络请求）：
- 源配置关键形态（$type=video、api_endpoints 发现/搜索/详情、detail 为 POST、
  chapters url_template={url} 直链 m3u8、selfcheck off）
- Discovery API：browse 顶层 list → 作品；url 模板 /title/{id}?title={title}
  （?title= 注入精确标题参数，绕过 detail POST 必须 title 才能命中正确 vod）
- 搜索：POST /api/search-parallel（text/event-stream，SSE 流）→ sse_field=videos
  合并全部采集源结果，item_fields 映射 MacCMS vod_* 字段；url /title/{vod_id}?title={vod_name}
- fetch_detail API：POST body 从详情 URL query 解析 {title} 填充、field_extractors
  映射 vod_*/type_name、episodes → Chapter（直链 m3u8 不进播放页转换）
- fetch_video_episode 直链 passthrough：分集 URL 已是 .m3u8 → 原样返回，
  不误走 HTML 播放页解析
"""
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.parser import Parser  # noqa: E402

_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources", "ikanpp.json"
)
_BASE = "https://www.ikanpp.com"
_M3U8 = "https://v.gsuus.com/play/erkYX1wa/index.m3u8"

_BROWSE = {
    "code": 200,
    "page": 1,
    "pagecount": 320,
    "total": 11504,
    "limit": 36,
    "list": [
        {
            "id": "ik020581",
            "title": "生化危机：爆发夜",
            "cover": "https://image.tmdb.org/t/p/w500/abc.jpg",
            "rate": "8.1",
            "year": "2026",
            "types": ["恐怖", "科幻"],
            "remarks": "全高清",
            "area": "华语",
            "updatedAt": "2026-09-17",
            "url": "/title/ik020581-生化危机-爆发夜",
        }
    ],
}

_DETAIL = {
    "code": 200,
    "success": True,
    "data": {
        "vod_id": 218720,
        "vod_name": "生化危机：爆发夜2026",
        "vod_pic": "https://img.guangsuimage.com/cover/69b183.jpg",
        "vod_remarks": "抢先版",
        "vod_content": "某医院突然爆发病毒，全城失守的夺命狂奔。",
        "vod_actor": "爱丽丝,马特·安德森",
        "vod_director": "保罗·安德森",
        "type_name": "恐怖片",
        "episodes": [
            {"name": "抢先版", "url": _M3U8, "index": 1},
        ],
    },
    "healed": True,
}


class _FakeHttp:
    """离线假 HttpClient：按 URL/正文特征返回相应 JSON 或 SSE 文本。"""

    defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def __init__(self):
        self.last_body = None

    def get_text(self, url, headers=None, proxy=None, timeout=10, retries=3,
                 interval_ms=0, encoding=None, proxy_pool=None, direct=False):
        if "library/browse" in url:
            return json.dumps(_BROWSE, ensure_ascii=False)
        return "{}"

    def get_json(self, url, **kw):
        return json.loads(self.get_text(url))

    def post_json(self, url, json_body=None, **kw):
        self.last_body = dict(json_body or {})
        return dict(_DETAIL)

    def post_text(self, url, json_body=None, **kw):
        if "search-parallel" in url:
            self.last_body = dict(json_body or {})
            # 模拟 ikanpp 非标准 SSE：超大 videos 事件被服务器拆成多物理行，
            # 仅首行带 data: 前缀，续行为裸 UTF-8 片段（多字节字符被截断跨行）
            ev1 = json.dumps(
                {"type": "videos", "source": "jisu", "videos": [
                    {"source": "jisu", "vod_id": 20711, "vod_name": "战狼2", "vod_pic": "https://img.jisuimage.com/cover/48aedb.jpg", "vod_area": "中国大陆", "vod_remarks": "正片"},
                    {"source": "guangsu", "vod_id": 209244, "vod_name": "战狼之父亲", "vod_pic": "https://img.guangsuimage.com/cover/cb4247.jpg", "vod_area": "中国大陆", "vod_remarks": "第81-99集完结"},
                ]},
                ensure_ascii=False,
            )
            ev2 = json.dumps(
                {"type": "videos", "source": "baofeng", "videos": [
                    {"source": "baofeng", "vod_id": 44913, "vod_name": "战狼", "vod_pic": "https://img.bfzypic.com/upload/a6b55f.jpg", "vod_area": "中国大陆", "vod_remarks": "HD"},
                    {"source": "jisu", "vod_id": 20711, "vod_name": "战狼2", "vod_pic": "https://img.jisuimage.com/cover/48aedb.jpg", "vod_area": "中国大陆", "vod_remarks": "正片"},
                ]},
                ensure_ascii=False,
            )

            def _chunk800(ev):
                # 拆成 800 字节物理行：首行 data:，续行裸 JSON（含跨字符截断）
                lines = ["data: " + ev[:800]]
                rest = ev[800:]
                while rest:
                    lines.append(rest[:800])
                    rest = rest[800:]
                return lines

            return (
                'data: {"type":"start","totalSources":2}\n\n'
                + "\n".join(_chunk800(ev1)) + "\n\n"
                + "\n".join(_chunk800(ev2)) + "\n\n"
                + 'data: {"type":"complete","totalVideosFound":4}\n\n'
            )
        return "{}"

    def close(self):
        pass


class _Checker:
    pass


def _source() -> SourceConfig:
    with open(_SOURCE_PATH, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), _SOURCE_PATH)


def _content():
    http = _FakeHttp()
    return http, Content(http, Parser(), _Checker())


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_source_config_loads():
    src = _source()
    assert src.source_id == "ikanpp"
    assert src.content_type == "video"
    assert src.source_name.startswith("ikanpp")
    assert src.base_url == _BASE
    api = src.raw.get("api_endpoints") or {}
    assert api.get("discovery", {}).get("url") == "/api/library/browse"
    assert api.get("discovery", {}).get("response_path") == "list"
    se = api.get("search") or {}
    assert se.get("url") == "/api/search-parallel"
    assert se.get("method") == "POST"
    assert se.get("sse") is True
    assert se.get("sse_field") == "videos"
    assert (se.get("body") or {}).get("query") == "{keyword}"
    sources = (se.get("body") or {}).get("sources") or []
    assert len(sources) == 25
    assert sources[0]["id"] == "juliang"
    assert sources[-1]["id"] == "modu_dm"
    assert (se.get("item_fields") or {}).get("title") == "vod_name"
    assert (se.get("item_fields") or {}).get("url") == "/title/{vod_id}?title={vod_name}&source={source}"
    det = api.get("detail") or {}
    assert det.get("method") == "POST"
    assert det.get("body") == {"id": "{id}", "source": "{source}", "title": "{title}"}
    assert (det.get("chapters") or {}).get("items") == "episodes"
    assert (det.get("chapters") or {}).get("url_template") == "{url}"
    assert ((src.raw.get("diagnostics") or {}).get("selfcheck") or {}).get("strategy") == "off"
    cats = ((src.raw.get("endpoints") or {}).get("discovery") or {}).get("list_item", {}).get("categories") or []
    assert len(cats) >= 5
    assert any(c["title"] == "电影" for c in cats)


# --------------------------------------------------------------------------- #
# 发现列表（browse API）
# --------------------------------------------------------------------------- #
def test_discovery_browse_works():
    http, _ = _content()
    src = _source()
    disc = Discovery(http, Parser(), _Checker())
    works = disc.list_works(src, "/api/library/browse?type=movie", 1)
    assert len(works) == 1
    w = works[0]
    assert w.title == "生化危机：爆发夜"
    assert w.cover == "https://image.tmdb.org/t/p/w500/abc.jpg"
    # 详情 URL 携带 ?title=<精确标题>（detail POST 按 title 命中逻辑必需）
    assert w.url == f"{_BASE}/title/ik020581?title=生化危机：爆发夜"


# --------------------------------------------------------------------------- #
# 搜索（search-parallel：POST + SSE 流）
# --------------------------------------------------------------------------- #
def test_search_parallel_sse_aggregates_sources():
    from framework.search import Search

    http, _ = _content()
    src = _source()
    searcher = Search(http, Parser())
    res = searcher.search_one(src, "战狼")
    # 两批 SSE videos 事件合并，同 URL（同 vod_id）去重
    assert len(res) == 3
    titles = {r.title for r in res}
    assert "战狼2" in titles
    assert "战狼之父亲" in titles
    zl2 = next(r for r in res if r.title == "战狼2")
    assert zl2.url == f"{_BASE}/title/20711?title=战狼2&source=jisu"
    assert zl2.cover == "https://img.jisuimage.com/cover/48aedb.jpg"
    assert zl2.author == "中国大陆"
    assert zl2.update == "正片"
    assert all(r.source_id == "ikanpp" for r in res)


def test_search_parallel_posts_query_and_sources():
    from framework.search import Search

    http, _ = _content()
    src = _source()
    searcher = Search(http, Parser())
    searcher.search_one(src, "战狼")
    body = http.last_body
    assert body["query"] == "战狼"
    assert body["page"] == "1"
    assert len(body["sources"]) == 25


# --------------------------------------------------------------------------- #
# fetch_detail API（POST body 注入 {title}）
# --------------------------------------------------------------------------- #
def test_fetch_detail_posts_title_from_query():
    http, c = _content()
    src = _source()
    d = c.fetch_detail(src, f"{_BASE}/title/ik020581?title=生化危机：爆发夜&source=jisu")
    assert http.last_body["id"] == "ik020581"
    assert http.last_body["title"] == "生化危机：爆发夜"
    assert http.last_body["source"] == "jisu"
    assert d.title == "生化危机：爆发夜2026"
    assert d.cover == "https://img.guangsuimage.com/cover/69b183.jpg"
    assert d.status == "抢先版"
    assert d.author == "保罗·安德森"
    assert "爱丽丝" in d.actor
    assert d.tags == ["恐怖片"]
    assert len(d.chapters) == 1
    assert d.chapters[0].title == "抢先版"
    assert d.chapters[0].url == _M3U8  # 直链 m3u8，未进播放页转换


# --------------------------------------------------------------------------- #
# 直链 passthrough（fetch_video_episode）
# --------------------------------------------------------------------------- #
def test_fetch_video_episode_m3u8_passthrough():
    http, c = _content()
    src = _source()
    got = c.fetch_video_episode(src, _M3U8)
    assert got == _M3U8


def test_fetch_video_streams_m3u8_passthrough():
    from framework.adblock import adblock_for

    http, c = _content()
    src = _source()
    if not adblock_for(src).enabled:
        v, a = c.fetch_video_streams(src, _M3U8)
        assert v == _M3U8
        assert a == ""


def test_looks_like_direct_media():
    assert Content._looks_like_direct_media("https://x/a.m3u8") is True
    assert Content._looks_like_direct_media("https://x/a.mpd") is True
    assert Content._looks_like_direct_media("https://x/a.mp4") is True
    assert Content._looks_like_direct_media("https://x/a.html") is False
    assert Content._looks_like_direct_media("/title/ik020581?title=x") is False
    assert Content._looks_like_direct_media("") is False