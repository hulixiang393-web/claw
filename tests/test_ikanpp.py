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
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.content import Chapter, Content, Detail  # noqa: E402
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
        self.last_post_kw = None

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
        self.last_post_kw = dict(kw)
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
    assert w.url == f"{_BASE}/title/ik020581?title=%E7%94%9F%E5%8C%96%E5%8D%B1%E6%9C%BA%EF%BC%9A%E7%88%86%E5%8F%91%E5%A4%9C&source=bfzy"


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
    assert zl2.url == f"{_BASE}/title/20711?title=%E6%88%98%E7%8B%BC2&source=jisu"
    assert zl2.cover == "https://img.jisuimage.com/cover/48aedb.jpg"
    assert zl2.author == "中国大陆"
    assert zl2.update == "正片"
    assert all(r.source_id == "ikanpp" for r in res)


def test_search_groups_same_title_and_keeps_provider_variants():
    from framework.search import Search

    src = _source()
    searcher = Search(_GroupHttp(), Parser())
    results = searcher.search_one(src, "测试剧")
    assert len(results) == 1
    assert results[0].title == "测试剧"
    assert results[0].provider == "juliang"
    assert results[0].provider_name == "巨量资源"
    assert [v.provider for v in results[0].variants] == ["baofeng", "jisu"]


def test_grouped_detail_exposes_source_list_and_switches_by_detail_url():
    from framework.search import Search

    src = _source()
    http = _GroupHttp()
    searcher = Search(http, Parser())
    result = searcher.search_one(src, "测试剧")[0]
    content = Content(http, Parser(), _Checker())
    detail = content.fetch_detail(src, result.url)
    assert [item["source_id"] for item in detail.source_list] == [
        "juliang", "baofeng", "jisu",
    ]
    assert [item["name"] for item in detail.source_list] == [
        "巨量资源", "暴风资源", "极速资源",
    ]
    variant_url = detail.source_list[1]["detail_url"]
    switched, chapters = content.switch_source_url(src, variant_url)
    assert http.last_body["id"] == "100"
    assert http.last_body["source"] == "baofeng"
    assert switched.source_list[0]["source_id"] == "baofeng"
    assert chapters[0].url == _M3U8


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


def test_search_parallel_sse_passes_charset_encoding():
    """SSE 分支 post_text 应传 transports.charset，避免 text/event-stream
    无 charset 时 requests 按 iso-8859-1 解码导致中文标题乱码。"""
    from framework.search import Search

    http, _ = _content()
    src = _source()
    searcher = Search(http, Parser())
    searcher.search_one(src, "战狼")
    assert http.last_post_kw is not None
    assert http.last_post_kw.get("encoding") == "utf-8"


class _GroupHttp(_FakeHttp):
    def post_text(self, url, json_body=None, **kw):
        self.last_post_kw = dict(kw)
        self.last_body = dict(json_body or {})
        rows = [
            {"source": "baofeng", "vod_id": 100, "vod_name": "测试剧", "vod_pic": "", "vod_area": "中国大陆", "vod_remarks": "HD"},
            {"source": "juliang", "vod_id": 1, "vod_name": "测试剧", "vod_pic": "", "vod_area": "中国大陆", "vod_remarks": "HD"},
            {"source": "jisu", "vod_id": 7, "vod_name": "测试剧", "vod_pic": "", "vod_area": "中国大陆", "vod_remarks": "HD"},
        ]
        return "data: " + json.dumps({"type": "videos", "videos": rows}, ensure_ascii=False) + "\n\n"

    def post_json(self, url, json_body=None, **kw):
        self.last_body = dict(json_body or {})
        payload = json.loads(json.dumps(_DETAIL))
        payload["data"]["vod_name"] = "测试剧"
        payload["data"]["episodes"] = [{"name": "第1集", "url": _M3U8, "index": 1}]
        return payload


class _IkanppFallbackHttp(_FakeHttp):
    def __init__(self):
        super().__init__()
        self.provider_calls = []

    def post_text(self, url, json_body=None, **kw):
        self.last_post_kw = dict(kw)
        self.last_body = dict(json_body or {})
        return 'data: {"type":"videos","source":"zuida","videos":[{"source":"zuida","vod_id":999,"vod_name":"���","vod_pic":"","vod_area":"","vod_remarks":""}]}\n\n'

    def get_json(self, url, **kw):
        self.provider_calls.append(url)
        return {
            "code": 1,
            "list": [{
                "vod_id": 123456,
                "vod_name": "扑杀天使",
                "vod_sub": "",
                "vod_en": "Bokusatsu Tenshi",
                "vod_pic": "https://img.zuidapi.example/push.jpg",
                "vod_area": "日本",
                "vod_remarks": "全1集",
            }],
        }


def test_search_parallel_falls_back_to_matching_provider_for_garbled_title():
    from framework.search import Search

    http = _IkanppFallbackHttp()
    src = _source()
    searcher = Search(http, Parser())
    res = searcher.search_one(src, "扑杀天使")
    assert [(r.title, r.url) for r in res] == [
        ("扑杀天使", f"{_BASE}/title/123456?title=%E6%89%91%E6%9D%80%E5%A4%A9%E4%BD%BF&source=zuida")
    ]
    assert len(http.provider_calls) == 1
    assert "api.zuidapi.com/api.php/provide/vod" in http.provider_calls[0]
    assert "wd=%E6%89%91%E6%9D%80%E5%A4%A9%E4%BD%BF" in http.provider_calls[0]


def test_fallback_result_detail_keeps_provider_and_m3u8():
    from framework.search import Search

    http = _IkanppFallbackHttp()
    src = _source()
    result = Search(http, Parser()).search_one(src, "扑杀天使")[0]
    content = Content(http, Parser(), _Checker())
    detail = content.fetch_detail(src, result.url)
    assert http.last_body["id"] == "123456"
    assert http.last_body["source"] == "zuida"
    assert detail.chapters[0].url == _M3U8


class _NoMatchProviderHttp(_IkanppFallbackHttp):
    def get_json(self, url, **kw):
        self.provider_calls.append(url)
        return {"code": 1, "list": []}


class _MatchingProviderHttp(_IkanppFallbackHttp):
    def post_text(self, url, json_body=None, **kw):
        self.last_post_kw = dict(kw)
        self.last_body = dict(json_body or {})
        return 'data: {"type":"videos","source":"jisu","videos":[{"source":"jisu","vod_id":7,"vod_name":"战狼2","vod_pic":"","vod_area":"","vod_remarks":""}]}\n\n'


def test_provider_fallback_config_above_cap_still_requests_only_three_providers():
    from framework.search import Search

    http = _NoMatchProviderHttp()
    src = _source()
    src.raw["api_endpoints"]["search"]["provider_fallback"]["max_providers"] = 10
    Search(http, Parser()).search_one(src, "扑杀天使")
    assert len(http.provider_calls) == 3


def test_matching_normalized_title_does_not_trigger_provider_fallback():
    from framework.search import Search

    http = _MatchingProviderHttp()
    src = _source()
    results = Search(http, Parser()).search_one(src, "战狼")
    assert [result.title for result in results] == ["战狼2"]
    assert http.provider_calls == []


def test_title_query_is_encoded_and_detail_decodes_special_characters():
    from framework.search import Search

    class SpecialTitleHttp(_FakeHttp):
        def post_text(self, url, json_body=None, **kw):
            self.last_body = dict(json_body or {})
            return 'data: {"type":"videos","source":"jisu","videos":[{"source":"jisu","vod_id":8,"vod_name":"A&B/#?","vod_pic":"","vod_area":"","vod_remarks":""}]}\n\n'

    http = SpecialTitleHttp()
    src = _source()
    result = Search(http, Parser()).search_one(src, "A&B/#?")[0]
    query = parse_qs(urlsplit(result.url).query)
    assert query["title"] == ["A&B/#?"]
    assert query["source"] == ["jisu"]
    Content(http, Parser(), _Checker()).fetch_detail(src, result.url)
    assert http.last_body["title"] == "A&B/#?"


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


def test_direct_media_detail_is_not_persisted_in_long_detail_cache():
    class Cache:
        def __init__(self):
            self.calls = []

        def set(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    _, content = _content()
    cache = Cache()
    content._cache = cache
    detail = Detail(title="demo", source_id="ikanpp", content_type="video", url="demo", chapters=[Chapter(title="1", url=_M3U8)])
    content._cache_set_detail("detail:ikanpp:demo", detail)
    assert cache.calls == []
