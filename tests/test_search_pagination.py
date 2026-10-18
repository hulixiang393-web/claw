"""搜索分页统一化：模板修正 + 框架翻页逻辑（关键词 base_url / 重复页去重）。"""
import json
from pathlib import Path

from framework.config import load_source
from framework.parser import Parser
from framework.search import Search


ROOT = Path(__file__).resolve().parents[1]


class _SrcStub:
    base_url = "https://ex.com"


class FakeHttp:
    """get_text 回一个含「本页 URL」的条目，保证不同页产出不同 url（便于计数）。"""

    def __init__(self):
        self.calls = []
        self.defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def get_text(self, url, **kwargs):
        self.calls.append(("GET", url))
        return f'<div><a class="i" href="{url}">t</a></div>'

    def post_form(self, url, form_data, **kwargs):
        self.calls.append(("POST", url, dict(form_data)))
        page = dict(form_data).get("page", 1)
        return f'<div><a class="i" href="{url}?p={page}">t</a></div>'

    def close(self):
        pass


def _synth(tmp_path, search_cfg, constraints=None):
    """写一份最小可加载的合成源，用于验证翻页行为。"""
    cfg = {
        "$schema_version": 2,
        "$id": "tmppage",
        "$type": "comic",
        "$name": "tmp",
        "$enabled": True,
        "$weight": 1.0,
        "transports": {"base_url": "https://x.test"},
        "endpoints": {"search": search_cfg},
        "constraints": {"search": constraints or {}},
    }
    p = tmp_path / "tmppage.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return load_source(p)


def _item_cfg():
    return {
        "root_selector": {"css": "div"},
        "fields": {"title": {"css": "a.i"}, "url": {"css": "a.i", "attr": "href"}},
    }


# --------------------------------------------------------------------------- #
# 1) 源配置：分页模板修正 + max_pages 统一
# --------------------------------------------------------------------------- #
def test_fixed_source_pagination_templates():
    def tpl(sid):
        return load_source(ROOT / "sources" / f"{sid}.json").get_search_config()[
            "paginator"
        ]["url_template"]

    assert tpl("manben") == "/search?page={page}&title={keyword}&language=1"
    assert tpl("badnews") == "/dm/search/q-{keyword}/page-{page}"
    assert tpl("czbooks") == "/s/{keyword}/{page}"
    assert tpl("ho5ho") == "/page/{page}/?s={keyword}"
    assert tpl("rouvideo") == "/search?q={keyword}&page={page}"


def test_fixed_source_max_pages_unified():
    for sid in ("manben", "badnews", "czbooks", "ho5ho", "18av", "dm5", "rouvideo"):
        cons = load_source(ROOT / "sources" / f"{sid}.json").raw["constraints"]["search"]
        assert cons["max_pages"] == 30, sid
        assert cons["max_results"] == 0, sid


# --------------------------------------------------------------------------- #
# 2) 框架：_build_page_url
# --------------------------------------------------------------------------- #
def test_build_page_url_substitutes_keyword_in_base_url():
    url = Search._build_page_url(
        source=_SrcStub(),
        base_url="/search?q={keyword}",
        keyword="a b",
        kw_param="q",
        page=2,
        page_param="page",
        url_template="",
    )
    assert url == "https://ex.com/search?q=a%20b&page=2"


def test_build_page_url_template_keyword_and_page():
    url = Search._build_page_url(
        source=_SrcStub(),
        base_url="/s/{keyword}",
        keyword="kw",
        kw_param="keyword",
        page=3,
        page_param="page",
        url_template="/s/{keyword}/{page}",
    )
    assert url == "https://ex.com/s/kw/3"


# --------------------------------------------------------------------------- #
# 3) 框架：重复页去重保护
# --------------------------------------------------------------------------- #
def test_repeat_page_guard_stops_at_first_duplicate(tmp_path):
    # 模板缺 {page} → 每页 URL 相同，应只抓第 1 页，不把 max_pages 白抓成重复
    src = _synth(
        tmp_path,
        {"base_url": "/s/{keyword}", "method": "GET",
         "paginator": {"url_template": "/s/{keyword}"}, "item": _item_cfg()},
        {"max_pages": 10, "max_results": 0},
    )
    http = FakeHttp()
    Search(http, Parser()).search_one(src, "kw")
    assert len(http.calls) == 1


def test_repeat_guard_skips_post_body_paging(tmp_path):
    # POST 无 url_template：页码在 body（URL 恒定属正常）→ 应抓满 max_pages
    src = _synth(
        tmp_path,
        {"base_url": "/search", "method": "POST", "keyword_param": "s",
         "item": _item_cfg()},
        {"max_pages": 3, "max_results": 0},
    )
    http = FakeHttp()
    Search(http, Parser()).search_one(src, "kw")
    assert len(http.calls) == 3
    assert all(c[0] == "POST" for c in http.calls)


def test_paginated_source_fetches_all_pages(tmp_path):
    src = _synth(
        tmp_path,
        {"base_url": "/s", "method": "GET",
         "paginator": {"url_template": "/s?q={keyword}&p={page}"}, "item": _item_cfg()},
        {"max_pages": 3, "max_results": 0},
    )
    http = FakeHttp()
    results = Search(http, Parser()).search_one(src, "kw")
    assert len(http.calls) == 3
    assert len(results) == 3
