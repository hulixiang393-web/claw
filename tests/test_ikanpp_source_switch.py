# -*- coding: utf-8 -*-
"""ikanpp 播放界面换源（同作品多线路）与中插广告过滤回归测试（全部离线）。

覆盖：
- 详情接口只给一条线路时，按 `chapters.related_episodes` 把同作品其它线路的集
  并进来，`detail.source_list` 给出线路菜单（与 omofuna 的 source_switch 同一份
  播放界面契约：source_id/name/detail_url）。
- 换线路后 `switch_source_url` 把 `Detail.url` 更新到目标线路：否则下次取流
  又从旧 URL 推导线路坐标，切回原线路。
- 换过去再切回（命中详情缓存）时线路菜单仍完整，不丢当前线路。
- 任一线路请求失败只跳过该线路，详情主体不受影响；未配置的源零影响。
- `ad_block.hls_rules.drop_short_blocks`：真实清单（json1080 源）里与正片同主机
  同目录、URL 无广告词的极短 DISCONTINUITY 块（16.5~17.7s）被删；片尾块需与中插
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

# 各线路自己的作品坐标与分集（暴风为当前线路）
_LINE_IDS = {"baofeng": 20717, "guangsu": 41107, "jisu": 4664, "zuida": 9}
_LINE_HOSTS = {
    "baofeng": "c1.rrcdnbf6.com",
    "guangsu": "v.gsuus.com",
    "jisu": "vv.jisuzyv.com",
    "zuida": "api.zuidapi.example",
}


def _source() -> SourceConfig:
    with open(_SOURCE_PATH, encoding="utf-8") as handle:
        return SourceConfig.from_dict(json.load(handle), _SOURCE_PATH)


class _LineHttp:
    """离线假 HttpClient，**复刻真实 API 的危险行为**：

    - 搜索（post_text，SSE）：只带目标线路一个源，返回该线路自己的 vod_id。
    - 详情（post_json）：若 id 不是该线路自己的 vod_id，**静默返回另一部作品**
      （真实实测：json1080 的 id + source=jisu → 「窒息的阳光」，success=True）。
      这样才能保证「换线路必须用目标线路自己的 id」有测试兜底。
    """

    defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def __init__(self, failing=(), episodes_per_line=2, line_titles=None):
        self.bodies = []
        self.search_bodies = []
        self.failing = set(failing)
        self.episodes_per_line = episodes_per_line
        # 各线路的显示标题（默认与当前作品同名；可注入「国语版」这类差异）
        self.line_titles = dict(line_titles or {})

    # ---- 搜索：拿到目标线路自己的 vod_id ----
    def post_text(self, url, json_body=None, **kwargs):
        body = dict(json_body or {})
        self.search_bodies.append(body)
        entries = body.get("sources") or []
        videos = []
        for entry in entries:
            source = str(entry.get("id") or "")
            if source in self.failing:
                continue
            videos.append({
                "source": source,
                "vod_id": _LINE_IDS[source],
                "vod_name": self.line_titles.get(source, "流浪地球"),
            })
        return "data: " + json.dumps(
            {"type": "videos", "videos": videos}, ensure_ascii=False
        ) + "\n\n"

    # ---- 详情 ----
    def post_json(self, url, json_body=None, **kwargs):
        body = dict(json_body or {})
        self.bodies.append(body)
        source = body.get("source") or "baofeng"
        if source in self.failing:
            raise RuntimeError("line down")
        own_id = str(_LINE_IDS[source])
        if str(body.get("id") or "") != own_id:
            # 真实 API 的行为：id 属于别的线路时，返回一部**无关作品**
            return {
                "success": True,
                "data": {
                    "vod_id": body.get("id"),
                    "vod_name": "窒息的阳光",
                    "source": source,
                    "episodes": [{
                        "name": "正片",
                        "url": "https://wrong.example/movie.m3u8",
                        "index": 0,
                    }],
                },
            }
        host = _LINE_HOSTS.get(source, "cdn.example")
        episodes = [
            {
                "name": f"第{i + 1}集",
                "url": f"https://{host}/play/{own_id}/{i + 1}/index.m3u8",
                "index": i,
            }
            for i in range(self.episodes_per_line)
        ]
        return {
            "success": True,
            "data": {
                "vod_id": _LINE_IDS[source],
                "vod_name": self.line_titles.get(source, "流浪地球"),
                "source": source,
                "episodes": episodes,
            },
        }

    def get_text(self, url, **kwargs):
        return "{}"

    def get_json(self, url, **kwargs):
        return {}

    def close(self):
        pass


def _content(http=None):
    return Content(http or _LineHttp(), Parser(), None)


def _detail_url(source="baofeng", vid=20717, title="流浪地球"):
    return f"{_BASE}/title/{vid}?title={title}&source={source}"


def _with_lines(content=None, http=None, source=None, url=None):
    """模拟播放界面的新流程：详情只打标记，线路菜单单独按需取。

    对应实现：`fetch_detail` 不发线路请求（`line_menu_available=True`），
    `fetch_line_menu` 在后台补；补到之后把线路分集并进详情、构建 source_list。
    """
    content = content or _content(http)
    source = source or _source()
    url = url or _detail_url()
    detail = content.fetch_detail(source, url)
    # 当前线路的分集标记来源（与旧合并逻辑一致；菜单靠 URL 的 source 定位）
    from urllib.parse import parse_qs, urlsplit

    cur = (parse_qs(urlsplit(detail.url).query).get("source") or [""])[0]
    for chapter in detail.chapters:
        if not getattr(chapter, "provider", ""):
            chapter.provider = cur
    chapters = content.fetch_line_menu(source, detail.url, detail.title)
    if chapters:
        detail.chapters = list(detail.chapters) + list(chapters)
    if not detail.source_list:
        content._attach_line_sources(source, detail)
    return content, detail


# --------------------------------------------------------------------------- #
# 播放界面换线路（related_episodes）
# --------------------------------------------------------------------------- #
def test_detail_defers_line_menu_and_makes_no_extra_requests():
    """详情加载不得为换线路多花网络往返（实测 4.5s → 1.2s 的关键）。"""
    http = _LineHttp()
    detail = _content(http).fetch_detail(_source(), _detail_url())

    assert detail.line_menu_available is True
    assert detail.source_list == []
    assert len(http.bodies) == 1        # 只有详情本身
    assert http.search_bodies == []     # 没有为线路发检索


def test_line_menu_fetched_on_demand():
    http = _LineHttp()
    content, detail = _with_lines(http=http)

    assert [item["source_id"] for item in detail.source_list] == ["guangsu", "jisu"]
    assert [item["name"] for item in detail.source_list] == ["光速资源", "极速资源"]
    # 菜单项带上目标线路自己的详情 URL（线路坐标随 URL 一起变）
    assert [item["detail_url"] for item in detail.source_list] == [
        _detail_url("guangsu", 41107),
        _detail_url("jisu", 4664),
    ]
    # 每个菜单项都带该线路的集，播放界面据此定位当前集
    assert [len(item["episodes"]) for item in detail.source_list] == [2, 2]


def test_related_lines_resolve_their_own_vod_id_by_title():
    """换线路必须先在该线路内按标题查到它自己的 vod_id。"""
    http = _LineHttp()
    _with_lines(http=http)

    # 首次详情
    assert http.bodies[0] == {"id": "20717", "source": "baofeng", "title": "流浪地球"}
    # 每条相关线路：先按标题搜索（只带该源），再用**它自己的 id** 请求详情
    assert len(http.search_bodies) == 2
    for body in http.search_bodies:
        assert body["query"] == "流浪地球"
        assert len(body["sources"]) == 1  # 只查目标线路，不重复拉 25 个源
    line_bodies = [b for b in http.bodies[1:] if b.get("source") != "baofeng"]
    by_source = {b["source"]: b for b in line_bodies}
    assert by_source["guangsu"]["id"] == str(_LINE_IDS["guangsu"])
    assert by_source["jisu"]["id"] == str(_LINE_IDS["jisu"])
    assert all(b["title"] == "流浪地球" for b in line_bodies)


def test_wrong_line_id_would_return_unrelated_work():
    """守住故障现场：用别的线路 id 请求会拿到无关作品，绝不能并进分集列表。"""
    _content_, detail = _with_lines()

    # 假 HTTP 在 id 不匹配时会返回「窒息的阳光」单集片；验证它没混进来
    urls = [ch.url for ch in detail.chapters]
    assert not any("wrong.example" in u for u in urls)
    assert all(ch.line_switch_url for ch in detail.chapters if ch.provider != "baofeng")


def test_line_with_mismatched_title_is_skipped():
    """目标线路返回的作品标题不匹配 → 宁可不并该线路，也不混入无关作品。"""
    http = _LineHttp(line_titles={"jisu": "完全无关的另一部剧"})
    _content_, detail = _with_lines(http=http)

    assert [item["source_id"] for item in detail.source_list] == ["guangsu"]
    assert not any(ch.provider == "jisu" for ch in detail.chapters)


def test_line_title_with_language_suffix_still_matches():
    """「国語版」这类后缀差异不应导致线路被丢弃（真实数据即如此）。"""
    http = _LineHttp(line_titles={"jisu": "流浪地球国语版"})
    _content_, detail = _with_lines(http=http)

    assert [item["source_id"] for item in detail.source_list] == ["guangsu", "jisu"]


def test_line_vod_id_is_resolved_once_per_title():
    """同一 (线路, 标题) 只搜索一次：反复取线路菜单不得重复请求。"""
    http = _LineHttp()
    content, detail = _with_lines(http=http)
    searches = len(http.search_bodies)
    assert searches == 2  # guangsu + jisu 各搜一次

    again = content.fetch_line_menu(_source(), detail.url, detail.title)
    assert again  # 仍然能拿到线路集
    assert len(http.search_bodies) == searches  # 但没有任何新检索


def test_merged_chapters_are_tagged_with_line_switch_info():
    _content_, detail = _with_lines()

    current = [ch for ch in detail.chapters if ch.provider == "baofeng"]
    assert len(current) == 2
    assert all(ch.line_switch_url == "" for ch in current)

    guangsu = [ch for ch in detail.chapters if ch.provider == "guangsu"]
    assert [ch.title for ch in guangsu] == ["第1集", "第2集"]
    assert all(ch.line_name == "光速资源" for ch in guangsu)
    assert all(
        ch.line_switch_url == _detail_url("guangsu", 41107) for ch in guangsu
    )


def test_switch_source_url_moves_detail_url_to_target_line():
    http = _LineHttp()
    content, detail = _with_lines(http=http)
    before = len(http.bodies)

    target = detail.source_list[0]
    switched, chapters = content.switch_source_url(_source(), target["detail_url"])

    # URL 跟着目标线路走：否则下次取流仍按旧 URL 的 source 再切回原线路
    assert switched.url == _detail_url("guangsu", 41107)
    switch_bodies = http.bodies[before:]
    assert switch_bodies[0]["source"] == "guangsu"
    assert switch_bodies[0]["title"] == "流浪地球"
    # 切换后的详情只含目标线路自己的集（其它线路是菜单项，不混进集列表）
    assert {ch.url.split("/")[2] for ch in chapters} == {"v.gsuus.com"}


def test_switch_back_keeps_full_line_menu_from_cache():
    """无论当前在哪条线路，按需取线路菜单都应拿到「其它线路」完整列表。"""
    content, detail = _with_lines()
    source = _source()

    switched, _ = content.switch_source_url(source, detail.source_list[0]["detail_url"])
    # 切到光速后，菜单应变成「光速之外的两条线」（当前线路不再列出）
    lines = content.fetch_line_menu(source, switched.url, switched.title)
    menu = content.line_menu_from_chapters(lines)
    assert [item["source_id"] for item in menu] == ["jisu", "baofeng"]

    back, _ = content.switch_source_url(source, _detail_url())
    lines = content.fetch_line_menu(source, back.url, back.title)
    menu = content.line_menu_from_chapters(lines)
    assert [item["source_id"] for item in menu] == ["guangsu", "jisu"]


def test_single_failing_line_is_skipped_without_breaking_detail():
    http = _LineHttp(failing={"jisu"})
    _content_, detail = _with_lines(http=http)

    assert [item["source_id"] for item in detail.source_list] == ["guangsu"]
    # 当前线路的分集照常可用
    assert any(ch.provider == "baofeng" for ch in detail.chapters)


def test_line_requests_are_bounded_by_max_lines():
    http = _LineHttp()
    source = _source()
    source.raw["api_endpoints"]["detail"]["chapters"]["related_episodes"][
        "max_lines"
    ] = 1
    _with_lines(http=http, source=source)
    # 首次详情 + 1 条相关线路（该线路一次搜索 + 一次详情）
    assert len(http.bodies) == 1 + 1
    assert len(http.search_bodies) == 1


def test_related_episodes_disabled_has_no_effect():
    http = _LineHttp()
    source = _source()
    source.raw["api_endpoints"]["detail"]["chapters"]["related_episodes"][
        "enabled"
    ] = False
    detail = _content(http).fetch_detail(source, _detail_url())

    assert len(http.bodies) == 1
    assert detail.line_menu_available is False
    assert detail.source_list == []
    # 关掉之后按需取也应当拿不到线路
    assert _content(http).fetch_line_menu(source, _detail_url(), "流浪地球") == []


def test_search_provider_group_takes_precedence_over_related_episodes(monkeypatch):
    from framework import search_groups

    http = _LineHttp()
    content = _content(http)
    content.fetch_detail(_source(), _detail_url())
    search_groups.remember_group(
        "ikanpp",
        "流浪地球",
        [
            {"provider": "baofeng", "name": "暴风资源", "url": _detail_url(), "order": 1},
            {
                "provider": "zuida",
                "name": "最大资源",
                "url": _detail_url("zuida", 9),
                "order": 2,
            },
        ],
    )
    before = len(http.bodies)
    try:
        # 搜索分组记得这一组作品：详情走分组给出的采集源菜单（当前源排首位）
        grouped = content.fetch_detail(_source(), _detail_url("zuida", 9))
    finally:
        search_groups.clear_groups()

    assert [item["source_id"] for item in grouped.source_list] == ["zuida", "baofeng"]
    # 分组菜单来自搜索缓存，不含 related_episodes 的线路集字段
    assert all("episodes" not in item for item in grouped.source_list)
    assert http.bodies[before]["source"] == "zuida"


def test_group_menu_skips_related_line_requests(monkeypatch):
    """已有分组线路列表时不再请求同作品线路（省掉两条线路的详情往返）。"""
    from framework import search_groups

    http = _LineHttp()
    content = _content(http)
    search_groups.remember_group(
        "ikanpp",
        "流浪地球",
        [
            {"provider": "baofeng", "name": "暴风资源", "url": _detail_url(), "order": 1},
            {
                "provider": "zuida",
                "name": "最大资源",
                "url": _detail_url("zuida", 9),
                "order": 2,
            },
        ],
    )
    try:
        grouped = content.fetch_detail(_source(), _detail_url())
    finally:
        search_groups.clear_groups()

    assert [item["source_id"] for item in grouped.source_list] == ["baofeng", "zuida"]
    # 只有本次详情请求，没有 guangsu/jisu 两条线路请求
    assert [body["source"] for body in http.bodies] == ["baofeng"]


def test_source_without_related_config_is_unaffected():
    with open(_SOURCE_PATH, encoding="utf-8") as handle:
        raw = json.load(handle)
    config = dict(raw)
    detail_api = json.loads(json.dumps(raw["api_endpoints"]["detail"]))
    detail_api["chapters"].pop("related_episodes")
    config["api_endpoints"] = dict(raw["api_endpoints"], detail=detail_api)
    config["$id"] = "ikanpp-no-lines"
    source = SourceConfig.from_dict(config, _SOURCE_PATH)

    http = _LineHttp()
    detail = _content(http).fetch_detail(source, _detail_url())
    assert len(http.bodies) == 1
    assert detail.source_list == []


# --------------------------------------------------------------------------- #
# 中插广告：drop_short_blocks（依据真实清单定标）
# --------------------------------------------------------------------------- #
_MAIN = "https://s2.bfllvip.com/video/x/%d.ts"
_AD = "https://fengbao13.com/video/pp/%d.ts"


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
