# -*- coding: utf-8 -*-
"""h-comic（H-Comic 成人漫畫 https://h-comic.com）源离线单元测试。

站点事实（已实测，见 fixtures）：
- SvelteKit SPA：首页/详情 DOM 由 JS 渲染；SSR HTML 只含骨架 + 内嵌 JS 数据。
- 数据端点 ``/__data.json`` 返回 devalue 扁平结构
  ``{"type":"data","nodes":[null,{"type":"data","data":[...]}]}``，对象/数组成员
  用整数索引指向 data 数组；字符串/布尔/null 为字面量，负索引 = undefined/null。
- 首页数据根 ``{comics, pages, tagTranslate}``；``pages`` = {pages, total, limit}。
- 作品详情/阅读路由 ``/comics/{id}/{chunk}``（chunk 为 20 图一批，步进 10，可重叠）。
- 封面 ``https://h-comic.link/api/{comic_source}/{media_id}``；
  正文图 ``https://h-comic.link/api/{comic_source}/{media_id}/pages/{n}``（n 从 1 起）。
- 搜索 ``/?q={keyword}&tag=``；分类 ``/?q=&tag={english_tag}``；分页 ``?page=N``。

本测试全部离线（FakeHttp / 真实响应切片），不触网。渲染型 content 用假渲染器
monkeypatch，验证框架取图链路与图片 URL 模板。

运行：
    $env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\\code\\claw"
    python -m pytest tests\\test_h_comic.py -q
"""
from __future__ import annotations

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

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "h_comic")
_BASE = "https://h-comic.com"
_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources", "h-comic.json"
)


class _FakeChecker:
    pass


class _FakeHttp:
    """离线假 HttpClient：按 URL 返回对应 fixture 快照。"""

    cache = None
    _sleeper = staticmethod(lambda *_a: None)

    def __init__(self, sleeper=None, defaults=None):
        self.defaults = defaults or NetworkDefaults(timeout=15, retries=0, interval_ms=0)
        self.calls = []

    def get_text(self, url, headers=None, timeout=None, retries=None,
                 interval_ms=None, encoding=None, proxy_pool=None, direct=False):
        self.calls.append(url)
        # 详情/阅读页 SSR：返回 reader.html（含内嵌作品数据）
        with open(os.path.join(_FIXTURES, "reader.html"), encoding="utf-8") as f:
            return f.read()

    def close(self):
        pass


def _load(name: str) -> str:
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _source() -> SourceConfig:
    with open(_SOURCE_PATH, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), _SOURCE_PATH)


def _content(http=None):
    return Content(http or _FakeHttp(), Parser(), _FakeChecker())


# --------------------------------------------------------------------------- #
# devalue（SvelteKit 扁平结构）解码器
# --------------------------------------------------------------------------- #
def _deref(data, v):
    if isinstance(v, int) and not isinstance(v, bool):
        return None if v < 0 else data[v]
    return v


def _build(data, v):
    v = _deref(data, v)
    if isinstance(v, list):
        return [_build(data, x) for x in v]
    if isinstance(v, dict):
        return {k: _build(data, x) for k, x in v.items()}
    return v


def _devalue_root(name: str):
    payload = json.loads(_load(name))
    node = payload["nodes"][1]
    return _build(node["data"], 0)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_source_config_loads():
    src = _source()
    assert src.source_id == "h-comic"
    assert src.content_type == "comic"
    assert src.base_url == _BASE
    assert src.has_discovery() is True
    assert src.transports().get("timeout") == 20
    assert src.transports().get("interval_ms") == 500
    headers = src.request_headers()
    assert headers.get("Referer") == "https://h-comic.com/"
    assert src.get_detail_config() is not None


def test_selfcheck_off():
    src = _source()
    assert ((src.raw.get("diagnostics") or {}).get("selfcheck") or {}).get(
        "strategy"
    ) == "off"


def test_search_config():
    src = _source()
    search = src.get_search_config()
    assert search.get("base_url") == "/?q={keyword}&tag=&page={page}"
    assert search.get("render") == "playwright"
    item = search.get("item") or {}
    assert item.get("root_selector", {}).get("css") == "div.card"
    # render_config 必须挂在 search.item 下（框架 _search_html_rendered 读取位置）
    assert (item.get("render_config") or {}).get("filter_keyword") is False


def test_discovery_categories_and_pagination_config():
    src = _source()
    disc = src.get_discovery_config()
    cats = (disc.get("list_item") or {}).get("categories") or []
    assert len(cats) >= 20
    titles = {c["title"] for c in cats}
    assert {"全部", "全彩", "巨乳", "NTR", "同志 BL", "肛交"} <= titles
    full = next(c for c in cats if c["title"] == "全彩")
    assert "tag=full%20color" in full["url"]
    assert full["url"].endswith("page={page}")
    pag = disc.get("list_paginator") or {}
    assert pag.get("type") == "increment"
    assert pag.get("param") == "page"
    works_li = disc.get("works_list_item") or {}
    assert works_li.get("root_selector", {}).get("css") == "div.card"
    # SPA 站：发现页需 Playwright 渲染（配置已写，待框架支持 discovery render）
    assert disc.get("render") == "playwright"


def test_discovery_page_url_built():
    src = _source()
    disc = Discovery(_FakeHttp(), Parser(), _FakeChecker())
    cats = {c["title"]: c["url"] for c in (src.get_discovery_config().get("list_item") or {}).get("categories") or []}
    assert disc._build_page_url(src, cats["全部"], 1) == "/?q=&tag=&page=1"
    assert disc._build_page_url(src, cats["全部"], 5) == "/?q=&tag=&page=5"
    assert disc._build_page_url(src, cats["全彩"], 3) == "/?q=&tag=full%20color&page=3"


# --------------------------------------------------------------------------- #
# 列表（渲染后 DOM 切片）
# --------------------------------------------------------------------------- #
def test_discovery_works_extract_from_rendered_html():
    """对 Playwright 渲染后的首页 DOM 快照，按 works_list_item 提取作品。"""
    src = _source()
    disc = src.get_discovery_config()
    works_li = disc["works_list_item"]
    doc = Parser().parse(_load("home_rendered.html"))
    items = Parser().parse_items(
        doc, works_li["root_selector"], works_li["fields"], src.base_url
    )
    good = [it for it in items if it.get("title") and it.get("url") and it.get("cover")]
    assert len(good) >= 5
    first = good[0]
    assert "草太" in first["title"]
    assert first["url"] == "/comics/681118/1?id=681118"
    assert first["cover"] == "https://h-comic.link/api/nh/4181625"
    assert all(it["url"].startswith("/comics/") for it in good)
    assert all("h-comic.link/api" in it["cover"] for it in good)


def test_devalue_home_data_lists_comics():
    """首页 __data.json devalue 解码 → ≥5 部作品，字段齐全。"""
    root = _devalue_root("home_data.json")
    assert set(root.keys()) >= {"comics", "pages"}
    comics = root["comics"]
    assert len(comics) >= 5
    c = comics[0]
    assert c["id"] == "681118"
    assert c["media_id"] == "4181625"
    assert c["comic_source"] == "nh"
    assert isinstance(c["title"], dict) and c["title"].get("japanese")
    assert c["images"]["cover"]["w"] > 0
    assert c["num_pages"] > 0
    # 首页分页信息
    pages = root["pages"]
    assert pages["limit"] == 10
    assert pages["total"] > 1000


# --------------------------------------------------------------------------- #
# 详情
# --------------------------------------------------------------------------- #
def test_detail_fields_extract_from_ssr_script():
    src = _source()
    fields = src.get_detail_config()["fields"]
    doc = Parser().parse(_load("reader.html"))
    p = Parser()
    title = p.extract_first(doc, fields["title"])
    assert "草太" in title
    cover = p.extract_first(doc, fields["cover"], src.base_url)
    assert cover == "https://h-comic.link/api/nh/4181625"


def test_fetch_detail_returns_title_and_single_chapter():
    src = _source()
    c = _content()
    url = "/comics/681118/1?id=681118"
    d = c.fetch_detail(src, url)
    assert d.title and "草太" in d.title
    assert d.cover == "https://h-comic.link/api/nh/4181625"
    assert len(d.chapters) == 1
    assert d.chapters[0].url.endswith("/comics/681118/1?id=681118")


# --------------------------------------------------------------------------- #
# 正文（图片 URL 模板 + 框架取图链路）
# --------------------------------------------------------------------------- #
def _page_urls(comic_source: str, media_id: str, num_pages: int):
    return [
        f"https://h-comic.link/api/{comic_source}/{media_id}/pages/{n}"
        for n in range(1, num_pages + 1)
    ]


def test_content_image_url_template_from_reader_data():
    root = _devalue_root("reader_data.json")
    comic = root["comic"]
    assert root["page"] == 1
    urls = _page_urls(comic["comic_source"], comic["media_id"], comic["num_pages"])
    assert len(urls) >= 3
    assert urls[0] == "https://h-comic.link/api/nh/4181625/pages/1"
    assert urls[-1].endswith("/pages/39")


def test_fetch_comic_pages_via_render(monkeypatch):
    """假渲染器返回 20 张正文图 → 框架取图链路返回 ≥3 张。"""
    import framework.playwright_helper as ph

    rendered = _page_urls("nh", "4181625", 39)[:20]
    captured = {}

    def _fake_render(url, **kw):
        captured["url"] = url
        captured["cfg"] = kw
        return list(rendered)

    monkeypatch.setattr(ph, "fetch_rendered_images_sync", _fake_render)
    src = _source()
    c = _content()
    imgs = c.fetch_comic_pages(src, "/comics/681118/1?id=681118")
    assert len(imgs) >= 3
    assert imgs[:3] == rendered[:3]
    assert captured["url"] == "https://h-comic.com/comics/681118/1?id=681118"
    assert captured["cfg"].get("extract_mode") == "img"
    assert captured["cfg"].get("img_selector") == "img.fixed.w-0.h-0"
