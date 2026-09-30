"""Detail.related（站内相关推荐）解析测试。

- Detail.related 默认空列表（所有源零影响）
- hanime1 HTML 详情路径：endpoints.detail.related 解析 #related-tabcontent 卡片，
  剔除自身 url、按 max 截断、每项只含 title/url/cover 键
- 未配置 related 块 → related == []（普通源回归）
- pornhub ytdlp 详情路径：api_endpoints.detail.related 从详情 HTML 的
  relatedVideos JS 变量提取，URL 由 vkey 拼接、cover 留空
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DETAIL_HTML = """<html><body><h1>测试视频</h1></body></html>"""


class _Http:
    def __init__(self, page=""):
        self.page = page
        self.calls = {}
        self.defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def get_text(self, url, headers=None, proxy=None, timeout=10, retries=3,
                 interval_ms=0, encoding=None, proxy_pool=None, direct=False):
        self.calls[url] = self.calls.get(url, 0) + 1
        return self.page

    def close(self):
        pass


def _make_source(endpoints, api_endpoints=None, base_url="https://example.com"):
    def _raw_for():
        return {"endpoints": endpoints, "api_endpoints": api_endpoints or {}}

    S = type("S", (), {
        "source_id": "demo",
        "base_url": base_url,
        "content_type": "video",
        "_raw": _raw_for(),
    })

    def _raw(self):
        return self._raw

    def _get_detail_config(self):
        return self._raw["endpoints"]["detail"]

    S.raw = property(_raw)
    S.transports = lambda self: {}
    S.request_headers = lambda self: {}
    S.proxy_pool = lambda self: None
    S.get_detail_config = _get_detail_config
    S.get_discovery_config = lambda self: {}
    S.get_search_config = lambda self: {}
    return S()


class _Checker:
    pass


def _make_content(http, yt=None):
    from framework.content import Content
    from framework.parser import Parser

    c = Content(http, Parser(), _Checker())
    if yt is not None:
        c._ytdlp = yt  # 复用懒加载单例槽位注入伪造 yt-dlp
    return c


def _related_sel():
    return {
        "root_selector": {"css": "div#related-tabcontent div.video-item-container"},
        "fields": {
            "title": {"css": "h4.video-title a, .video-title a"},
            "url": {"css": "a[href*='watch?v=']", "attr": "href"},
            "cover": {
                "fallback": [
                    {"css": "img.main-thumb", "attr": "src"},
                    {"css": "img", "attr": "src"}
                ]
            },
        },
        "max": 8,
    }


def _related_html(cards, own_v="1"):
    """构造含 #related-tabcontent 卡片的详情页 HTML。"""
    cards_html = ""
    for title, v, cover in cards:
        cards_html += (
            '<div class="video-item-container">'
            '<h4 class="video-title"><a href="/watch?v=' + v + '">' + title + "</a></h4>"
            '<a href="/watch?v=' + v + '"><img class="main-thumb" src="' + cover + '"></a>'
            "</div>"
        )
    h = """<html><body><h1>测试视频</h1><div id="related-tabcontent">"""
    h += cards_html
    h += "</div></body></html>"
    return h


def test_detail_related_default_empty():
    """无 related 配置 → Detail.related 默认 []（普通源零影响）。"""
    src = _make_source({"detail": {"fields": {"title": {"css": "h1"}}}})
    detail = _make_content(_Http(DETAIL_HTML)).fetch_detail(
        src, "https://example.com/watch?v=1"
    )
    assert detail.related == []


def test_detail_related_hanime_parsed():
    """hanime 路径：related 卡片解析为 [{title, url, cover}]，剔除自身、只留三键。"""
    html = _related_html([
        ("相关A", "2", "https://cdn.example.com/imgs/a.jpg"),
        ("相关B", "3", "https://cdn.example.com/imgs/b.jpg"),
    ], own_v="1")
    fields = {"title": {"css": "h1"}, "related": _related_sel()}
    src = _make_source({"detail": {"fields": fields["title"], "related": fields["related"]}})
    detail = _make_content(_Http(html)).fetch_detail(
        src, "https://example.com/watch?v=1"
    )
    assert detail.related == [
        {"title": "相关A", "url": "https://example.com/watch?v=2", "cover": "https://cdn.example.com/imgs/a.jpg"},
        {"title": "相关B", "url": "https://example.com/watch?v=3", "cover": "https://cdn.example.com/imgs/b.jpg"},
    ]
    for it in detail.related:
        assert set(it.keys()) == {"title", "url", "cover"}


def test_detail_related_hanime_own_removed():
    """自身 url 从相关推荐剔除；封面缺失时 cover 为空串。"""
    html = _related_html([
        ("我自己", "1", "/self.jpg"),
        ("相关C", "5", ""),
    ], own_v="1")
    fields = {"related": _related_sel()}
    src = _make_source({"detail": {"fields": {"title": {"css": "h1"}}, "related": fields["related"]}})
    detail = _make_content(_Http(html)).fetch_detail(
        src, "https://example.com/watch?v=1"
    )
    assert [it["title"] for it in detail.related] == ["相关C"]
    assert detail.related == [
        {"title": "相关C", "url": "https://example.com/watch?v=5", "cover": ""}
    ]


def test_detail_related_hanime_max_truncated():
    """超过 max 条只取前 max（默认 8，用 max=2 验证截断）。"""
    cards = [(f"相关{i}", str(i + 2), f"/imgs/{i}.jpg") for i in range(5)]
    html = _related_html(cards, own_v="1")
    sel = _related_sel()
    sel["max"] = 2
    src = _make_source({"detail": {"fields": {"title": {"css": "h1"}}, "related": sel}})
    detail = _make_content(_Http(html)).fetch_detail(
        src, "https://example.com/watch?v=1"
    )
    assert len(detail.related) == 2
    assert [it["title"] for it in detail.related] == ["相关0", "相关1"]


def test_detail_related_hanime_missing_selector_empty():
    """related 配置存在但选择器未命中 → related 空列表（不抛异常）。"""
    sel = _related_sel()
    sel["root_selector"] = {"css": "div#no-such-block div.video-item-container"}
    src = _make_source({"detail": {"fields": {"title": {"css": "h1"}}, "related": sel}})
    detail = _make_content(_Http(DETAIL_HTML)).fetch_detail(
        src, "https://example.com/watch?v=1"
    )
    assert detail.related == []


class _FakeYt:
    def __init__(self, d):
        self._d = d

    def fetch_detail(self, url):
        return self._d


def _ph_source(detail_api):
    return _make_source(
        {"detail": {"fields": {"title": {"css": "h1"}}}},
        api_endpoints={"detail": detail_api},
        base_url="https://cn.pornhub.com",
    )


def _ph_detail_api(extra=None):
    api = {
        "engine": "ytdlp",
        "url": "{url}",
        "related": {"js_regex": "relatedVideos\\s*=\\s*(\\[.*?\\]);", "max": 8},
    }
    api.update(extra or {})
    return api


def _ph_html(rel_list):
    import json as _json

    return "<html><head><script>var relatedVideos = " + _json.dumps(rel_list) + ";</script></head><body></body></html>"


def _ph_ytdata(title="测试视频"):
    return {
        "title": title,
        "author": "up",
        "cover": "https://cn.pornhub.com/c.jpg",
        "status": "10:00 · 100 播放",
        "summary": "简介",
        "actor": "",
        "chapters": [{"title": title, "url": "https://cn.pornhub.com/view_video.php?viewkey=MYKEY"}],
    }


def test_detail_related_pornhub_parsed():
    """pornhub ytdlp 路径：relatedVideos JSON → [{title, url, cover:""}]，剔除自身。"""
    html = _ph_html([
        {"title": "热门A", "vkey": "aaa111"},
        {"title": "我自己", "vkey": "MYKEY"},
        {"title": "热门B", "vkey": "bbb222"},
    ])
    src = _ph_source(_ph_detail_api())
    detail = _make_content(_Http(html), yt=_FakeYt(_ph_ytdata())).fetch_detail(
        src, "https://cn.pornhub.com/view_video.php?viewkey=MYKEY"
    )
    assert detail.related == [
        {"title": "热门A", "url": "https://cn.pornhub.com/view_video.php?viewkey=aaa111", "cover": ""},
        {"title": "热门B", "url": "https://cn.pornhub.com/view_video.php?viewkey=bbb222", "cover": ""},
    ]
    assert detail.related[0]["url"].startswith("https://cn.pornhub.com/view_video.php?viewkey=")


def test_detail_related_pornhub_missing_data_skipped():
    """缺 title/vkey 的项跳过；数组前 8 条截断（max=1 验证）。"""
    html = _ph_html([
        {"vkey": "nokey"},
        {"title": "无key", "vkey": None},
        {"title": "有效", "vkey": "good1"},
        {"title": "多余", "vkey": "extra2"},
    ])
    api = _ph_detail_api({"related": {"js_regex": "relatedVideos\\s*=\\s*(\\[.*?\\]);", "max": 1}})
    src = _ph_source(api)
    detail = _make_content(_Http(html), yt=_FakeYt(_ph_ytdata())).fetch_detail(
        src, "https://cn.pornhub.com/view_video.php?viewkey=MYKEY"
    )
    assert detail.related == [
        {"title": "有效", "url": "https://cn.pornhub.com/view_video.php?viewkey=good1", "cover": ""}
    ]


def test_detail_related_pornhub_no_related_block():
    """ytdlp 路径未配置 related 块 → related 空列表（pornhub 其他源零影响）。"""
    src = _ph_source({"engine": "ytdlp", "url": "{url}"})
    detail = _make_content(_Http(""), yt=_FakeYt(_ph_ytdata())).fetch_detail(
        src, "https://cn.pornhub.com/view_video.php?viewkey=MYKEY"
    )
    assert detail.related == []