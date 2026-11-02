# -*- coding: utf-8 -*-
"""跨站能力回归：http→https 升级、线路坐标回退、占位符不外发。

这些都是「站点改版后从 JSON 改不动」的通用能力，故与具体源解耦：
- upgrade_insecure_scheme：站点自己吐 http:// 绝对地址（agedm 目录 href）
- _fetch_chapters 的 sid 回退：default_sid 指向的线路在详情页不存在（omofuna）
- _fetch_detail_api 的 id_regex / 占位符跳过
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.content.base import upgrade_insecure_scheme  # noqa: E402
from framework.parser import Parser  # noqa: E402


class _Http:
    defaults = type("D", (), {"timeout": 5, "retries": 0, "interval_ms": 0})()

    def __init__(self, html="", json_map=None):
        self.html = html
        self.json_map = json_map or {}
        self.urls = []

    def get_text(self, url, **kw):
        self.urls.append(url)
        return self.html

    def get_json(self, url, **kw):
        self.urls.append(url)
        for key, value in self.json_map.items():
            if key in url:
                return value
        return {}

    def post_text(self, url, json_body=None, **kw):
        self.urls.append(url)
        return ""

    def close(self):
        pass


def _src(**over) -> SourceConfig:
    raw = {
        "$id": "demo",
        "$name": "demo",
        "$type": "video",
        "transports": {"base_url": "https://www.demo.com", "timeout": 5, "retries": 0},
    }
    raw.update(over)
    return SourceConfig.from_dict(raw)


# --------------------------------------------------------------------------- #
# http → https 升级
# --------------------------------------------------------------------------- #
def test_upgrade_same_host_http_to_https():
    base = "https://www.agedm.io"
    assert upgrade_insecure_scheme(base, "http://www.agedm.io/detail/1") == \
        "https://www.agedm.io/detail/1"
    # 跨主机不动（CDN 可能只支持 http）
    assert upgrade_insecure_scheme(base, "http://cdn.example/detail/1") == \
        "http://cdn.example/detail/1"
    # base 本身是 http 的源不受影响
    assert upgrade_insecure_scheme("http://a.com", "http://a.com/x") == "http://a.com/x"
    assert upgrade_insecure_scheme(base, "https://www.agedm.io/x") == \
        "https://www.agedm.io/x"


def test_abs_url_upgrade_can_be_disabled_per_source():
    c = Content(_Http(), Parser(), None)
    src = _src()
    assert c._abs_url(src, "http://www.demo.com/a") == "https://www.demo.com/a"
    src2 = _src(transports={"base_url": "https://www.demo.com", "allow_http": True})
    assert c._abs_url(src2, "http://www.demo.com/a") == "http://www.demo.com/a"


# --------------------------------------------------------------------------- #
# 线路坐标回退（default_sid 指向的线路不存在）
# --------------------------------------------------------------------------- #
_EP_HTML = """<html><body>
<ul class="play-list-content">
  <li><a href="/anime/abc/play/1/1.html">1集</a></li>
  <li><a href="/anime/abc/play/1/2.html">2集</a></li>
</ul></body></html>"""


def _switch_source() -> SourceConfig:
    return _src(
        endpoints={
            "detail": {"url": "/anime/{id}.html"},
            "content": {
                "episode": {
                    "source_switch": {
                        "default_sid": "2",
                        "ep_list_selector": ".play-list-content a[href*='/play/{sid}/']",
                        "ep_href_contains": "/play/",
                    }
                }
            },
        }
    )


def test_chapters_fall_back_to_present_sid_when_default_missing():
    c = Content(_Http(html=_EP_HTML), Parser(), None)
    src = _switch_source()
    doc = Parser().parse(_EP_HTML)
    chapters = c._fetch_chapters(
        src, doc, detail_url="https://www.demo.com/anime/abc.html"
    )
    # default_sid=2 在页面上不存在 → 用真实存在的 sid=1，而不是返回空列表
    assert [ch.title for ch in chapters] == ["1集", "2集"]
    assert chapters[0].url == "https://www.demo.com/anime/abc/play/1/1.html?sid=1"


def test_chapters_fall_back_to_all_play_links_when_sid_unknown():
    html = """<html><body>
    <a href="/anime/abc/play/x/1.html">第1集</a>
    <a href="/anime/abc/play/x/2.html">第2集</a>
    </body></html>"""
    c = Content(_Http(html=html), Parser(), None)
    src = _switch_source()
    doc = Parser().parse(html)
    chapters = c._fetch_chapters(
        src, doc, detail_url="https://www.demo.com/anime/abc.html"
    )
    assert [ch.title for ch in chapters] == ["第1集", "第2集"]


# --------------------------------------------------------------------------- #
# 详情 API：id_regex + 未填充占位符不外发
# --------------------------------------------------------------------------- #
def test_detail_api_strips_slug_and_drops_unfilled_params():
    http = _Http(json_map={"/api/lines": {"lines": []}})
    c = Content(http, Parser(), None)
    src = _src(
        endpoints={
            "detail": {"url": "/detail/{id}"},
            "content": {"episode": {}},
        },
        api_endpoints={
            "detail": {
                "url": "/api/lines",
                "method": "GET",
                "id_regex": "^(ik\\d+)",
                "params": {"title": "{id}", "source": "{source}"},
            }
        },
    )
    c.fetch_detail(src, "https://www.demo.com/title/ik130109-恐怖收集者")
    detail_url = next(u for u in http.urls if "/api/lines" in u)
    assert "title=ik130109" in detail_url
    assert "{source}" not in detail_url and "%7B" not in detail_url
