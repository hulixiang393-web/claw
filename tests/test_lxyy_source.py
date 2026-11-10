# -*- coding: utf-8 -*-
"""洛雪TV（tv.lxyy.club，API 聚合影视站）源离线单元测试。

站点证据（2026-11-03 实测）：
- 搜索：GET /api/proxy.php?action=search&wd=<kw>&page=N → {results:[...]}，
  一次请求并回 8 个采集源；item 自带 source（采集源 id）与 id（该源侧主键）。
- 详情：GET /api/proxy.php?action=detail&source=<src>&id=<id> →
  {details:[{... episodes:[{group, episodes:[{name,url}]}]}]}，
  mj/dytt/ik/md 的分集是 CDN m3u8；jl 有 jlm3u8（直链）与 jlplayer（分享页）两条；
  qilin/fy 的分集是优酷/爱奇艺/B 站官方页（需站点自己的解析接口才能成型）。

覆盖（全部离线，无网络请求）：
- 源配置形态（GET 搜索/详情、exclude_providers、group.providers、pick_line）
- 搜索：请求参数、排除官采源、按标题聚合采集源线路、中文线路名、详情 URL 模板
- 详情：lines_path 嵌套结构 + pick_line=direct_media 只取直链线路且不建换线菜单
- 取流：分集 URL 已是 m3u8 → 直链 passthrough，不发任何请求
"""
import json
import os
import sys
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import load_source  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.parser import Parser  # noqa: E402
from framework.search import Search  # noqa: E402

_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources", "lxyy.json"
)
_MJ_M3U8 = "https://mujizybf08.com/movie/auto/69913b90ca01177c0d9c53c6.m3u8"
_JL_M3U8 = "https://jimaoys88.com/public/playback/42b9c2db9b901af75a9ef81ddc31b5af/smart.m3u8"
_JL_M3U8_BLOCKED = (
    "https://jimaoys94.com/public/playback/42b9c2db9b901af75a9ef81ddc31b5af/smart.m3u8"
)
_JL_PAGE = "https://jimaoys89.com/s/42b9c2db9b901af75a9ef81ddc31b5af"
_YOUKU = "https://v.youku.com/v_show/id_XNjI3MzgwNDM2.html"

_SEARCH = {
    "keyword": "狂飙",
    "page": 1,
    "success": True,
    "sources": {
        "qilin": "官采蓝光",
        "mj": "4k60帧",
        "dytt": "电影天堂",
        "fy": "官方源",
    },
    "results": [
        {"id": 23454, "name": "狂飙", "pic": "", "remarks": "已完结",
         "source": "qilin", "source_name": "官采蓝光", "type": "国产剧", "area": "大陆"},
        {"id": 14064, "name": "狂飙", "pic": "", "remarks": "已完结",
         "source": "mj", "source_name": "4k60帧", "type": "陆剧", "area": "大陆"},
        {"id": 20934, "name": "狂飙", "pic": "", "remarks": "全39集",
         "source": "dytt", "source_name": "电影天堂", "type": "国产剧", "area": "大陆"},
        {"id": 9, "name": "狂飙", "pic": "", "remarks": "全39集",
         "source": "fy", "source_name": "官方源", "type": "国产剧", "area": "大陆"},
        {"id": 31566, "name": "弹速狂飙", "pic": "", "remarks": "",
         "source": "mj", "source_name": "4k60帧", "type": "动作片", "area": ""},
    ],
}

_DETAIL_MJ = {
    "success": True,
    "details": [{
        "id": "14064",
        "name": "狂飙",
        "pic": "https://mujizybf09.com/videos/poster.jpg",
        "content": "安欣与高启强二十年缠斗。",
        "actor": "张译,张颂文",
        "remarks": "已完结",
        "type": "陆剧",
        "episodes": [{
            "group": "mjzy",
            "episodes": [
                {"name": "01", "url": _MJ_M3U8},
                {"name": "02", "url": "https://mujizybf08.com/movie/auto/ep2.m3u8"},
            ],
        }],
    }],
}

_DETAIL_JL = {
    "success": True,
    "details": [{
        "id": "629182741249785860",
        "name": "庆余年",
        "pic": "https://empimage.slcache.com/poster.webp",
        "content": "范闲入京都。",
        "actor": "张若昀",
        "remarks": "已完结",
        "type": "内地剧",
        "episodes": [
            {"group": "jlplayer",
             "episodes": [{"name": "第 1 集", "url": _JL_PAGE}]},
            {"group": "jlm3u8",
             "episodes": [{"name": "第 1 集", "url": _JL_M3U8}]},
        ],
    }],
}

_DETAIL_QILIN = {
    "success": True,
    "details": [{
        "id": "23454",
        "name": "狂飙",
        "pic": "",
        "content": "",
        "actor": "",
        "remarks": "",
        "type": "国产剧",
        "episodes": [{"group": "qiyi",
                      "episodes": [{"name": "第1集", "url": _YOUKU}]}],
    }],
}


class _FakeHttp:
    """离线假 HttpClient：按 URL query 分发到预置响应。"""

    defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def __init__(self):
        self.get_urls = []

    def get_json(self, url, headers=None, proxy=None, timeout=10, retries=3,
                 interval_ms=0, proxy_pool=None, direct=False, **kwargs):
        self.get_urls.append(url)
        query = parse_qs(urlsplit(url).query)
        if query.get("action", [""])[0] == "search":
            return _SEARCH
        if query.get("action", [""])[0] == "detail":
            return {"mj": _DETAIL_MJ, "jl": _DETAIL_JL, "qilin": _DETAIL_QILIN}.get(
                query.get("source", [""])[0], {"success": False, "details": []}
            )
        raise AssertionError(f"意外请求：{url}")

    def get_text(self, url, **kwargs):
        self.get_urls.append(url)
        return ""

    def get_bytes(self, url, **kwargs):
        self.get_urls.append(url)
        return b""

    def post_json(self, url, json_body=None, **kwargs):
        self.get_urls.append(url)
        return {}


class _Checker:
    def check(self, *args, **kwargs):
        return None

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _source():
    source = load_source(_SRC)
    source.raw.setdefault("diagnostics", {})["selfcheck"] = {"strategy": "off"}
    return source


# --------------------------------------------------------------------------- #
# 源配置形态
# --------------------------------------------------------------------------- #
def test_config_shape_is_get_api_and_no_discovery():
    raw = json.load(open(_SRC, encoding="utf-8"))
    assert raw["$id"] == "lxyy"
    assert raw["$type"] == "video"
    api = raw["api_endpoints"]
    assert api["search"]["method"] == "GET"
    assert api["search"]["params"]["wd"] == "{keyword}"
    assert api["search"]["response_path"] == "results"
    # 官采源（优酷/爱奇艺/B 站页）要第三方解析，本客户端拿不到流 → 排除
    assert set(api["search"]["exclude_providers"]) == {"qilin", "fy"}
    assert [p["id"] for p in api["search"]["group"]["providers"]] == [
        "mj", "jl", "dytt", "ik", "md", "360zy",
    ]
    assert api["detail"]["params"]["source"] == "{source}"
    assert api["detail"]["chapters"]["lines_path"] == "details.0.episodes"
    assert api["detail"]["chapters"]["pick_line"] == "direct_media"
    # 首页 /api/proxy.php?action=home 固定回官采源条目，切换参数无效 → 不做发现页
    assert not raw.get("endpoints", {}).get("discovery")
    assert not api.get("discovery")
    # jl 的 94 镜像对非浏览器出口一律 403 → 主机改写（配置项，非代码）
    assert raw["media"]["url_rewrite"][0]["pattern"] == r"https?://jimaoys94\.com/"


# --------------------------------------------------------------------------- #
# 搜索
# --------------------------------------------------------------------------- #
def test_search_requests_page_and_drops_official_sources():
    source = _source()
    http = _FakeHttp()
    Search(http, Parser()).search_one(source, "狂飙")
    url = http.get_urls[0]
    query = parse_qs(urlsplit(url).query)
    assert url.startswith("https://tv.lxyy.club/api/proxy.php?")
    assert query["action"] == ["search"]
    assert query["wd"] == ["狂飙"]
    assert query["page"] == ["1"]


def test_search_groups_collected_sources_into_lines():
    source = _source()
    results = Search(_FakeHttp(), Parser()).search_one(source, "狂飙")
    heads = [r for r in results if r.title == "狂飙"]
    assert len(heads) == 1  # 同名作品合成一张卡
    head = heads[0]
    providers = [head.provider] + [v.provider for v in head.variants]
    assert providers == ["mj", "dytt"]  # qilin/fy 已排除
    assert head.provider_name == "4K60帧"  # 展示名来自 group.providers
    assert head.url == (
        "https://tv.lxyy.club/watch.php?title=%E7%8B%82%E9%A3%99"
        "&source=mj&id=14064"
    )
    assert head.update == "已完结"
    assert head.author == "大陆"


# --------------------------------------------------------------------------- #
# 详情
# --------------------------------------------------------------------------- #
def test_detail_maps_metadata_and_single_line():
    source = _source()
    url = "https://tv.lxyy.club/watch.php?title=%E7%8B%82%E9%A3%99&source=mj&id=14064"
    detail = Content(_FakeHttp(), Parser(), _Checker()).fetch_detail(source, url)
    assert detail.title == "狂飙"
    assert detail.author == "张译,张颂文"
    assert detail.status == "已完结"
    assert detail.tags == ["陆剧"]
    assert detail.summary.startswith("安欣")
    assert [c.title for c in detail.chapters] == ["01", "02"]
    assert detail.chapters[0].url == _MJ_M3U8


def test_detail_pick_line_prefers_direct_media_line():
    """jl 同一条目有 jlplayer（分享页）与 jlm3u8（直链）两条线路：
    只留直链那条，且不生成换线菜单（线路坐标在同一 URL 里切不过去）。"""
    source = _source()
    url = "https://tv.lxyy.club/watch.php?title=%E5%BA%86%E4%BD%99%E5%B9%B4&source=jl&id=629182741249785860"
    detail = Content(_FakeHttp(), Parser(), _Checker()).fetch_detail(source, url)
    assert [c.url for c in detail.chapters] == [_JL_M3U8]
    assert detail.chapters[0].provider == "jlm3u8"
    assert not detail.source_list
    assert not detail.chapters[0].line_switch_url


def test_detail_official_source_keeps_page_url_as_chapter():
    """官采源（已从搜索里排除，但详情仍可达）：分集是内容页，
    pick_line 匹配不到直链时退回全部线路，不丢分集。"""
    source = _source()
    url = "https://tv.lxyy.club/watch.php?title=x&source=qilin&id=23454"
    detail = Content(_FakeHttp(), Parser(), _Checker()).fetch_detail(source, url)
    assert [c.url for c in detail.chapters] == [_YOUKU]


# --------------------------------------------------------------------------- #
# 取流
# --------------------------------------------------------------------------- #
def test_episode_m3u8_is_passthrough_without_requests():
    source = _source()
    http = _FakeHttp()
    url = Content(http, Parser(), _Checker()).fetch_video_episode(source, _MJ_M3U8)
    assert url == _MJ_M3U8
    assert http.get_urls == []


def test_episode_media_url_rewrite_fixes_blocked_cdn_host():
    """jl 的 CDN 镜像 jimaoys94 对非浏览器出口一律 403，同路径 95 正常：
    media.url_rewrite 把主机改掉，其它主机的地址原样不动。"""
    source = _source()
    url = Content(_FakeHttp(), Parser(), _Checker()).fetch_video_episode(source, _JL_M3U8_BLOCKED)
    assert url == _JL_M3U8_BLOCKED.replace("jimaoys94.com", "jimaoys95.com")
    assert Content(_FakeHttp(), Parser(), _Checker()).fetch_video_episode(
        source, _MJ_M3U8
    ) == _MJ_M3U8


def test_play_url_probe_follows_media_url_rewrite(monkeypatch):
    """可达性探测必须和播放走同一地址：拿被墙原地址探测会把好线路判死。"""
    import requests

    source = _source()
    seen = {}

    class _Resp:
        status_code = 200

        class raw:  # noqa: N801 —— 模仿 requests 的 raw 流
            @staticmethod
            def read(size):
                return b"#EXTM3U\n"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_get(url, headers=None, timeout=None, stream=False):
        seen["url"] = url
        return _Resp()

    monkeypatch.setattr(requests, "get", fake_get)
    ok = Content(_FakeHttp(), Parser(), _Checker())._play_url_probe(
        source, _JL_M3U8_BLOCKED
    )
    assert ok is True
    assert seen["url"] == _JL_M3U8_BLOCKED.replace("jimaoys94.com", "jimaoys95.com")


# --------------------------------------------------------------------------- #
# 线路可用性探测（播放界面「检测播放源」的同源线路分支）
# --------------------------------------------------------------------------- #
def test_probe_line_playable_ok_on_direct_media_line(monkeypatch):
    source = _source()
    content = Content(_FakeHttp(), Parser(), _Checker())
    monkeypatch.setattr(
        content, "_probe_playback_chain",
        lambda *a, **k: {"ok": True, "reason": "", "via": "proxy",
                         "segment": "https://cdn.test/a-1.ts"},
    )
    url = "https://tv.lxyy.club/watch.php?title=%E7%8B%82%E9%A3%99&source=mj&id=14064"
    result = content.probe_line_playable(source, url)
    assert result["ok"] is True
    assert result["episodes"] == 2
    assert result["url"] == _MJ_M3U8
    assert result["reason"] == ""
    assert result["via"] == "proxy"
    assert result["segment"] == "https://cdn.test/a-1.ts"


def test_probe_line_playable_flags_dead_cdn(monkeypatch):
    source = _source()
    content = Content(_FakeHttp(), Parser(), _Checker())
    monkeypatch.setattr(
        content, "_probe_playback_chain",
        lambda *a, **k: {"ok": False, "reason": "segment_http403", "via": "proxy",
                         "segment": "https://cdn.test/a-1.ts"},
    )
    url = "https://tv.lxyy.club/watch.php?title=x&source=jl&id=629182741249785860"
    result = content.probe_line_playable(source, url)
    assert result["ok"] is False
    # 清单 200 但分片 403 —— 只看可达性就会报「可播放」的那种死线路
    assert result["reason"] == "segment_http403"
    # pick_line 只留直链线路，所以探测的是 jlm3u8 那一集
    assert result["url"] == _JL_M3U8


def test_probe_line_playable_uses_current_episode_index(monkeypatch):
    """换线路后 App 按序号映射同一集，探测必须测同一集而不是首集。"""
    source = _source()
    content = Content(_FakeHttp(), Parser(), _Checker())
    seen = {}

    def chain(src, url, **kwargs):
        seen["url"] = url
        return {"ok": True, "reason": "", "via": "direct", "segment": ""}

    monkeypatch.setattr(content, "_probe_playback_chain", chain)
    url = "https://tv.lxyy.club/watch.php?title=%E7%8B%82%E9%A3%99&source=mj&id=14064"
    content.probe_line_playable(source, url, episode_index=1)
    assert seen["url"] == "https://mujizybf08.com/movie/auto/ep2.m3u8"


def test_probe_line_playable_reports_empty_detail(monkeypatch):
    source = _source()
    content = Content(_FakeHttp(), Parser(), _Checker())
    monkeypatch.setattr(content, "_play_url_probe", lambda *a, **k: True)
    result = content.probe_line_playable(
        source, "https://tv.lxyy.club/watch.php?title=x&source=md&id=404"
    )
    assert result["ok"] is False
    assert result["reason"] == "no_episodes"
    assert result["episodes"] == 0
