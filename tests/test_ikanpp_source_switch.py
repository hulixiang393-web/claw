# -*- coding: utf-8 -*-
"""ikanpp 详情（/api/lines 多线路）+ 换源播放 + 中插广告过滤回归（全部离线）。

站点把原来的 `/api/detail`（已 404）拆成了两个接口：
- `GET /api/lines?title=<id>&source=<src>` → `{"lines":[{sourceId,sourceName,episodes:[{name,url}]}]}`
- `GET /api/play?id=<vodId>&ep=<n>&film=1` → `{"url":"https://…mp4|l…m3u8"}`
标题/封面/简介只剩详情页 `og:*`，由 `api_endpoints.detail.meta_html` 补。

覆盖：
- 详情按线路摊平，但**播放界面只显示当前线路的分集**（否则 21 条线各 1 集会
  显示成「21 集」）；其它线路进 `detail.source_list` 线路菜单。
- 菜单项带目标线路自己的详情 URL（source 坐标随 URL 一起变），
  `switch_source_url` 换线后 `Detail.url` 跟着走。
- 播放地址从 `/api/play` 的 JSON `url` 字段提取。
- `ad_block.hls_rules.drop_short_blocks`：真实清单里与正片同主机、同目录、
  URL 无广告词、时长 16.5~17.7s 的极短 DISCONTINUITY 块被删；片尾块需与中插
  广告的时长签名一致才删。超长块、大占比、纯短清单、未配置该规则的源均不受影响。
"""
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from framework.adblock import AdblockEngine  # noqa: E402
from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.parser import Parser  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SOURCE_PATH = os.path.join(ROOT, "sources", "ikanpp.json")
_BASE = "https://www.ikanpp.com"

# 真实接口形状（略去 season/remarks 等无关字段）
_LINES = [
    {
        "sourceId": "youxuan",
        "sourceName": "优选",
        "episodes": [{"name": "正片", "url": "/api/play?id=youxuan-main&ep=1&film=1"}],
    },
    {
        "sourceId": "juliang",
        "sourceName": "巨量",
        "episodes": [{"name": "播放", "url": "https://jimao.example/movie/index.m3u8"}],
    },
    {
        "sourceId": "jisu",
        "sourceName": "极速",
        "episodes": [{"name": "正片", "url": "https://vv.jisu.example/play/abc/index.m3u8"}],
    },
]

_DETAIL_HTML = """<!DOCTYPE html><html><head>
<title>流浪地球 (2019) 在线观看 - 电影 | iKanPP 爱看片片</title>
<meta property="og:title" content="流浪地球 (2019) 在线观看 - 电影 | iKanPP 爱看片片">
<meta property="og:image" content="https://img.example/wandering.jpg">
<meta name="description" content="太阳即将毁灭，人类启动流浪地球计划。">
</head><body></body></html>"""


def _source() -> SourceConfig:
    with open(_SOURCE_PATH, encoding="utf-8") as handle:
        return SourceConfig.from_dict(json.load(handle), _SOURCE_PATH)


class _LinesHttp:
    """离线 HttpClient：只认 /api/lines、/api/play 与详情页 HTML。"""

    defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def __init__(self, lines=None, play_url="https://cdn.example/a.m3u8", html=_DETAIL_HTML):
        self.lines = list(lines if lines is not None else _LINES)
        self.play_url = play_url
        self.html = html
        self.json_calls = []
        self.text_calls = []

    def get_json(self, url, headers=None, proxy=None, timeout=None, retries=None, **kw):
        self.json_calls.append(url)
        if "/api/lines" in url:
            return {"lines": self.lines}
        return {}

    def get_text(self, url, **kw):
        self.text_calls.append(url)
        if "/api/play" in url:
            return json.dumps({"url": self.play_url, "kind": "mp4", "line": "official:z"})
        return self.html

    def get_bytes(self, url, **kw):  # pragma: no cover - 详情链路不取字节
        raise AssertionError("详情/播放链路不应请求字节流")

    def post_json(self, url, json_body=None, **kw):  # pragma: no cover
        raise AssertionError(f"ikanpp 已没有 POST 接口，却被请求：{url}")

    def post_text(self, url, json_body=None, **kw):  # pragma: no cover
        raise AssertionError(f"ikanpp 已没有 POST 接口，却被请求：{url}")

    def close(self):
        pass


def _content(http=None):
    return Content(http or _LinesHttp(), Parser(), None)


def _detail_url(source="bfzy", vid="ik130467", title="流浪地球"):
    return f"{_BASE}/title/{vid}?title={title}&source={source}"


# --------------------------------------------------------------------------- #
# 详情：/api/lines 摊平 + 页面 og 元数据
# --------------------------------------------------------------------------- #
def test_detail_reads_lines_api_and_page_meta():
    http = _LinesHttp()
    detail = _content(http).fetch_detail(_source(), _detail_url())

    # 标题/封面/简介来自详情页 og:*（接口里没有）
    assert detail.title == "流浪地球"
    assert detail.cover == "https://img.example/wandering.jpg"
    assert "流浪地球计划" in detail.summary
    # 只显示首条线路的分集：21 条线各 1 集不能显示成 21 集
    assert len(detail.chapters) == 1
    assert detail.chapters[0].title == "正片"
    assert detail.chapters[0].provider == "优选"
    # 线路菜单覆盖全部线路
    assert [item["sid"] for item in detail.source_list] == ["youxuan", "juliang", "jisu"]
    assert [item["name"] for item in detail.source_list] == ["优选", "巨量", "极速"]
    # 详情只调一次 lines 接口 + 一次详情页（取 og），没有 POST
    assert len([u for u in http.json_calls if "/api/lines" in u]) == 1
    assert all("/api/play" not in u for u in http.json_calls)


def test_line_menu_items_carry_target_line_detail_url():
    detail = _content().fetch_detail(_source(), _detail_url())

    from urllib.parse import parse_qs, urlsplit

    # 换线目标 URL：同作品坐标，只把 source 换成目标线路
    coordinates = []
    for item in detail.source_list:
        parts = urlsplit(item["detail_url"])
        query = parse_qs(parts.query)
        coordinates.append((parts.path, query.get("source"), query.get("title")))
    assert coordinates == [
        ("/title/ik130467", ["youxuan"], ["流浪地球"]),
        ("/title/ik130467", ["juliang"], ["流浪地球"]),
        ("/title/ik130467", ["jisu"], ["流浪地球"]),
    ]
    # 每条线路都带自己的集，播放界面据此定位当前集
    assert [len(item["episodes"]) for item in detail.source_list] == [1, 1, 1]


def test_current_line_param_selects_that_line():
    """详情 URL 的 source 命中线路 id 时，分集取该线路（而不是首条）。"""
    detail = _content().fetch_detail(_source(), _detail_url("jisu"))

    assert detail.chapters[0].provider == "极速"
    assert detail.chapters[0].url == "https://vv.jisu.example/play/abc/index.m3u8"


def test_switch_source_url_moves_detail_url_and_chapters():
    http = _LinesHttp()
    content = _content(http)
    detail = content.fetch_detail(_source(), _detail_url())

    target = detail.source_list[2]  # 极速
    switched, chapters = content.switch_source_url(_source(), target["detail_url"])

    # URL 跟着目标线路走：否则下次取流仍按旧 source 切回原线路
    assert "source=jisu" in switched.url
    assert [ch.title for ch in chapters] == ["正片"]
    assert {ch.url for ch in chapters} == {"https://vv.jisu.example/play/abc/index.m3u8"}


def test_play_url_extracted_from_api_play_json():
    http = _LinesHttp(play_url="https://p6.example/video.mp4")
    content = _content(http)
    detail = content.fetch_detail(_source(), _detail_url())

    play = content.fetch_video_episode(_source(), detail.chapters[0].url)
    assert play == "https://p6.example/video.mp4"
    assert any("/api/play" in u for u in http.text_calls)


def test_detail_without_lines_falls_back_to_single_chapter():
    """接口没给线路时不能凭空造集：保持空分集，交给 single_chapter 兜底。"""
    detail = _content(_LinesHttp(lines=[])).fetch_detail(_source(), _detail_url())

    assert detail.chapters == []
    assert detail.source_list == []


def _manifest(pre=30, ad=6, post=30, ad_host="fengbao13.com",
              first_ad=False, last_ad=False):
    def good(start, count):
        return "".join(
            "#EXTINF:10.0,\nhttps://s2.bfllvip.com/video/x/%d.ts\n" % i
            for i in range(start, start + count)
        )

    block = "".join(
        "#EXTINF:3.0,\nhttps://%s/video/pp/%d.ts\n" % (ad_host, i) for i in range(ad)
    )
    if first_ad:
        return "#EXTM3U\n" + block + "#EXT-X-DISCONTINUITY\n" + good(0, pre + post) + "#EXT-X-ENDLIST\n"
    if last_ad:
        return "#EXTM3U\n" + good(0, pre + post) + "#EXT-X-DISCONTINUITY\n" + block + "#EXT-X-ENDLIST\n"
    return (
        "#EXTM3U\n"
        + good(0, pre)
        + "#EXT-X-DISCONTINUITY\n"
        + block
        + "#EXT-X-DISCONTINUITY\n"
        + good(pre, post)
        + "#EXT-X-ENDLIST\n"
    )


def _engine():
    with open(_SOURCE_PATH, encoding="utf-8") as handle:
        raw = json.load(handle)
    return AdblockEngine(type("Source", (), {"raw": {"ad_block": raw["ad_block"]}})())


_BASE_URL = "https://s2.bfllvip.com/video/x/index.m3u8"

def test_ikanpp_config_declares_short_block_rule():
    with open(_SOURCE_PATH, encoding="utf-8") as handle:
        raw = json.load(handle)
    rules = raw["ad_block"]["hls_rules"]["drop_short_blocks"]
    assert rules["enabled"] is True
    assert rules["require_boundary"] is True
    # 阈值要覆盖实测 16.5~17.7s 的广告块，同时不能大到误伤正片
    assert 20 <= rules["max_duration"] <= 60
    assert rules["max_ratio"] <= 0.05
    # 旧的 URL 规则必须保留（暴风源的 /video/adjump/ 广告地址）
    assert "/video/adjump(?:/|$)" in raw["ad_block"]["block_url_regex"]


def test_midroll_short_block_removed_even_on_same_host():
    """关键回归：广告与正片**同主机**时也必须删。

    旧 drop_inserted_blocks 要求「主机切换」才删，而真实清单里广告块与正片
    同主机同目录 → 一条都命中不了，广告原样播放（用户实际反馈的故障）。
    """
    ads, out = _engine().detect_m3u8_ads(
        _manifest(ad_host="s2.bfllvip.com"), _BASE_URL
    )
    assert len(ads) == 6
    assert "video/pp/" not in out
    assert out.count("#EXT-X-DISCONTINUITY") == 1  # 中插两侧边界合并成一个 PTS 边界
    assert out.count("#EXTINF:") == 60


@pytest.mark.parametrize(
    "case, manifest",
    [
        # 广告块 3s×40=120s，超过 max_duration(30s) → 不是「短块」，不动
        ("too_long", _manifest(ad=40)),
    ],
)
def test_short_block_rule_keeps_risky_blocks(case, manifest):
    ads, out = _engine().detect_m3u8_ads(manifest, _BASE_URL)
    assert ads == [], case
    assert out == manifest


def test_short_block_rule_ignored_when_disabled():
    engine = _engine()
    engine._hls_rules = {"drop_short_blocks": {"enabled": False}}
    text = _manifest()
    ads, out = engine.detect_m3u8_ads(text, _BASE_URL)
    assert ads == []
    assert out == text


def test_short_block_rule_requires_configured_source():
    # 未声明该规则的源走默认引擎，不能被 ikankpp 的调参波及
    text = _manifest()
    ads, out = AdblockEngine().detect_m3u8_ads(text, _BASE_URL)
    assert ads == []
    assert out == text


# --------------------------------------------------------------------------- #
# 真实清单结构（json1080 源，2026-10 实测）：同主机同目录、URL 无广告词
# --------------------------------------------------------------------------- #
# block0 124片 497.7s 正片 | block1 4片 16.47s 广告
# block2 325片 1299.0s 正片 | block3 5片 17.67s 广告
# block4 244片 973.2s 正片 | block5 4片 16.47s 广告（片尾，与 block1 签名相同）
_REAL_AD_DURS = (6.633333, 3.333333, 4.8, 1.7)
_REAL_AD_DURS2 = (5.933333, 3.333333, 2.8, 5.3, 0.3)


def _real_block(durs, prefix, base=0):
    return "".join(
        "#EXTINF:%f,\n"
        "https://yzzy.play-cdn11.com/20221108/5738/2000k/hls/%s%04d.ts\n"
        % (dur, prefix, base + i)
        for i, dur in enumerate(durs)
    )


def _real_manifest(trailing=True, trailing_durs=(_REAL_AD_DURS, "tail_")):
    parts = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:8"]
    parts.append("#EXT-X-DISCONTINUITY")
    parts.append(_real_block((4.0,) * 124, "c0_"))
    parts.append("#EXT-X-DISCONTINUITY")
    parts.append(_real_block(_REAL_AD_DURS, "ad1_", 8000))
    parts.append("#EXT-X-DISCONTINUITY")
    parts.append(_real_block((4.0,) * 325, "c1_", 1000))
    parts.append("#EXT-X-DISCONTINUITY")
    parts.append(_real_block(_REAL_AD_DURS2, "ad2_", 8100))
    parts.append("#EXT-X-DISCONTINUITY")
    parts.append(_real_block((4.0,) * 244, "c2_", 2000))
    if trailing:
        parts.append("#EXT-X-DISCONTINUITY")
        parts.append(_real_block(trailing_durs[0], trailing_durs[1]))
    parts.append("#EXT-X-ENDLIST")
    return "\n".join(parts) + "\n"


def _segments(text):
    return [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def test_real_structure_removes_all_three_ad_blocks():
    """真实结构：两个中插广告 + 一个片尾广告全部删除，正片原样保留。"""
    text = _real_manifest()
    ads, out = _engine().detect_m3u8_ads(text, _BASE_URL)

    kept = _segments(out)
    assert len(kept) == 124 + 325 + 244
    assert not any(p in s for s in kept for p in ("ad1_", "ad2_", "tail_"))
    assert len(ads) == 4 + 5 + 4
    # EXTINF 与媒体 URL 一一对应
    assert out.count("#EXTINF:") == len(kept)
    # 片尾不留孤立边界：最后一行是 ENDLIST，其前必须是媒体分片
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines[-1] == "#EXT-X-ENDLIST"
    assert not lines[-2].startswith("#EXT-X-DISCONTINUITY")


def test_real_structure_keeps_trailing_block_with_different_signature():
    """片尾块与中插广告签名不同 → 视为正常片尾，保留（防误删片尾内容）。"""
    text = _real_manifest(trailing_durs=((30.0, 30.0), "end_"))
    _ads, out = _engine().detect_m3u8_ads(text, _BASE_URL)
    assert any("end_" in s for s in _segments(out))


def test_real_structure_keeps_clean_manifest_untouched():
    """单块正片清单（光速/hongniu 实测 590 片、0 个 DISCONTINUITY）零改动。"""
    text = (
        "#EXTM3U\n#EXT-X-VERSION:3\n"
        + _real_block((4.0,) * 50, "plain_")
        + "#EXT-X-ENDLIST\n"
    )
    ads, out = _engine().detect_m3u8_ads(text, _BASE_URL)
    assert ads == []
    assert out == text
