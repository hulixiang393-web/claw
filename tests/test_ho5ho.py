# -*- coding: utf-8 -*-
"""ho5ho（HO5HO 全彩成人漫畫 ho5ho.com）源离线单元测试。

覆盖（全部基于真实页面 HTML 快照，无网络请求）：
- 源配置加载关键形态（content.page.body 阅读翻页、page_placeholder 增量分页、
  selfcheck 关闭、搜索 keyword=s）
- 发现：静态分类入口 ≥15、分类 URL 带 /page/N/ 增量分页位、分页 URL 构造
- 发现列表卡片解析（section.ho5ho-v2-grid > article.ho5ho-v2-card，16 条）
- 搜索 /?s= 结果解析（同一卡片结构，5 条）
- 详情页字段解析（h1 标题、meta 简介、封面 -300x225.x32686 去后缀回源图、
  h漫分類 标签）
- fetch_detail：返回标题/简介/标签 + 章节列表，chapters[0].url = 阅读按钮 URL
  （…/server-1/），chapters[0].title =「開始閱讀」
- fetch_comic_pages：阅读正文图片爪取 src/data-src 双通道（懒加载第 2、3 图
  无 src 只有 data-src）+ next_page 翻页拼全（server-1 3 张 + p/2 3 张）

说明：
- 站点为 WordPress + Madara 仿站，阅读页每页固定 3 张 img.wp-manga-chapter-img，
  「下一页」a.btn.next_page 的 p/N/ 路径翻页；末页无 next 链接。
- 列表/详情/阅读页通用 .x32686 伪扩展名控制反爬清理 CDN 链接：列表封面保留，
  详情/阅读页按原图 URL 重写（去除）。

运行需求：本机以系统 Python（3.10，含 pytest + lxml）运行本测试。
"""
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

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "ho5ho")
_BASE = "https://www.ho5ho.com"
_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources", "ho5ho.json"
)


class _FakeChecker:
    pass


class _FakeHttp:
    """离线假 HttpClient：按 URL 特征返回对应 fixture 快照。"""

    cache = None
    _sleeper = staticmethod(lambda *_a: None)

    def __init__(self, sleeper=None, defaults=None):
        self.defaults = defaults or NetworkDefaults(timeout=15, retries=0, interval_ms=0)
        self.calls = []

    def get_text(self, url, headers=None, timeout=None, retries=None,
                 interval_ms=None, encoding=None, proxy_pool=None, direct=False):
        self.calls.append(url)
        if "/server-1/p/2/" in url:
            name = "server2.html"
        elif "/server-1/" in url:
            name = "server1.html"
        else:
            name = "detail.html"
        with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
            return f.read()

    def close(self):
        pass


def _parse(name: str):
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
        return Parser().parse(f.read())


def _source() -> SourceConfig:
    with open(_SOURCE_PATH, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), _SOURCE_PATH)


def _content(http=None):
    return Content(http or _FakeHttp(), Parser(), _FakeChecker())


def _work_card(doc):
    """从详情 fixture 取：([阅读按钮读 URL], [详情 URL 基底])。"""
    p = Parser()
    read_url = p.extract_first(
        doc, {"css": "a.ho5ho-v3-read-button", "attr": "href"}, _BASE
    )
    assert read_url.endswith("/server-1/")
    detail_url = read_url[: -len("/server-1/")]
    return read_url, detail_url


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_source_config_loads():
    src = _source()
    assert src.source_id == "ho5ho"
    assert src.content_type == "comic"
    assert src.source_name == "HO5HO（全彩成人漫畫）"
    assert src.base_url == _BASE
    assert src.transports().get("interval_ms") == 800
    assert src.transports().get("timeout") == 15
    headers = src.request_headers()
    assert headers.get("Accept-Language", "").startswith("zh-TW")
    assert src.get_detail_config() is not None


def test_selfcheck_off():
    src = _source()
    assert ((src.raw.get("diagnostics") or {}).get("selfcheck") or {}).get(
        "strategy"
    ) == "off"


def test_search_config():
    src = _source()
    search = src.get_search_config()
    assert search.get("base_url") == "/"
    assert search.get("keyword_param") == "s"
    assert search.get("method", "GET") == "GET"
    item = search.get("item") or {}
    assert item.get("root_selector", {}).get("css") == "article.ho5ho-v2-card"


def test_discovery_categories_and_pagination_config():
    src = _source()
    disc = src.get_discovery_config()
    cats = (disc.get("list_item") or {}).get("categories") or []
    assert len(cats) >= 15
    latest = next(c for c in cats if c["title"] == "最新漫畫")
    assert latest["url"] == "/page/1/"
    no_code = next(c for c in cats if c["title"] == "無碼")
    assert "h%E6%BC%AB%E6%A8%99%E7%B1%A4" in no_code["url"]
    assert no_code["url"].endswith("/page/1/")
    pag = disc.get("list_paginator") or {}
    assert pag.get("type") == "increment"
    assert pag.get("page_placeholder")  # 只有尾置 /page/N/ 才被替换
    works_li = disc.get("works_list_item") or {}
    assert works_li.get("root_selector", {}).get("css") == "article.ho5ho-v2-card"


def test_discovery_increment_page_url_built_with_placeholder():
    src = _source()
    disc = Discovery(_FakeHttp(), Parser(), _FakeChecker())
    cats = {c["title"]: c["url"] for c in (src.get_discovery_config().get("list_item") or {}).get("categories") or []}
    no_code = cats["無碼"]
    assert disc._build_page_url(src, no_code, 1) == no_code
    assert disc._build_page_url(src, no_code, 2) == no_code.replace("/page/1/", "/page/2/")
    p5 = disc._build_page_url(src, cats["最新漫畫"], 5)
    assert p5 == "/page/5/"


# --------------------------------------------------------------------------- #
# 发现列表
# --------------------------------------------------------------------------- #
def test_discovery_works_extract():
    doc = _parse("list.html")  # 首页 section.ho5ho-v2-grid 快照（原页 16 条）
    items = Parser().parse_items(
        doc,
        {"css": "article.ho5ho-v2-card"},
        {
            "title": {"css": "h3.ho5ho-v2-card-title a"},
            "url": {"css": "a.ho5ho-v2-card-cover", "attr": "href"},
            "cover": {"css": "img.ho5ho-v2-card-image", "attr": "src"},
        },
        _BASE,
    )
    assert len(items) == 16
    first = items[0]
    assert "色誘" in first["title"]
    assert first["url"].startswith("https://www.ho5ho.com/中字h漫/")
    assert first["cover"].startswith("https://ho5hocdn1.b-cdn.net/wp-content/uploads/")
    assert first["cover"].endswith(".x32686.jpg")  # 列表封面保留伪扩展名
    assert all(it["url"].startswith("https://www.ho5ho.com/") for it in items)


# --------------------------------------------------------------------------- #
# 搜索
# --------------------------------------------------------------------------- #
def test_search_items_extract():
    doc = _parse("search.html")  # /?s=色誘 结果网格快照（5 条）
    items = Parser().parse_items(
        doc,
        {"css": "article.ho5ho-v2-card"},
        {
            "title": {"css": "h3.ho5ho-v2-card-title a"},
            "url": {"css": "a.ho5ho-v2-card-cover", "attr": "href"},
            "cover": {"css": "img.ho5ho-v2-card-image", "attr": "src"},
        },
        _BASE,
    )
    assert len(items) == 5
    assert "色誘" in items[0]["title"]
    assert items[0]["url"].startswith("https://www.ho5ho.com/中字h漫/")


# --------------------------------------------------------------------------- #
# 详情页
# --------------------------------------------------------------------------- #
def test_detail_cover_url_replace_config():
    """详情封面 fields.cover.url_replace 把 -300x225.x32686 伪扩展名还原为原图。"""
    src = _source()
    cover_cfg = (src.get_detail_config().get("fields") or {}).get("cover") or {}
    assert cover_cfg.get("css") == "img.attachment-medium.size-medium.skip-lazy"
    ur = cover_cfg.get("url_replace") or []
    assert ur and "x32686" in ur[0] and "{1}" in ur[1]
    doc = _parse("detail.html")
    cover = Parser().extract_first(doc, cover_cfg, _BASE)
    assert cover.endswith("/2026/09/1.jpg")


def test_detail_fields_extract():
    doc = _parse("detail.html")
    p = Parser()
    title = p.extract_first(doc, {"css": "h1"})
    assert "色誘" in title
    summary = p.extract_first(doc, {"css": "meta[name='description']", "attr": "content"})
    assert summary.startswith("中字彩色成人漫畫")
    tags = p.extract(doc, {"css": "a[href*='h漫分類']"})
    assert "NTR" in tags and "亂倫" in tags


def test_fetch_detail_returns_title_chapters_and_read_button_url():
    src = _source()
    c = _content()
    doc = _parse("detail.html")
    read_url, detail_url = _work_card(doc)
    d = c.fetch_detail(src, detail_url)
    assert d.title and "色誘" in d.title
    assert d.summary.startswith("中字彩色成人漫畫")
    assert "NTR" in d.tags
    assert d.cover.endswith("/2026/09/1.jpg")
    assert len(d.chapters) == 1
    ch = d.chapters[0]
    assert ch.title == "開始閱讀"
    assert ch.url == read_url


# --------------------------------------------------------------------------- #
# 阅读页
# --------------------------------------------------------------------------- #
def test_fetch_comic_pages_src_and_datasrc_two_pages():
    """阅读正文：图片 src/data-src 双通道（懒加载图无 src 只有 data-src），
    a.btn.next_page 翻页 server-1 → p/2 拼全 6 张，末页无 next 停止。"""
    src = _source()
    http = _FakeHttp()
    c = _content(http)
    read_url, _detail_url = _work_card(_parse("detail.html"))
    imgs = c.fetch_comic_pages(src, read_url)
    assert len(imgs) == 6
    assert [u.rsplit("/", 1)[-1] for u in imgs] == ["1.jpg", "2.jpg", "3.jpg", "4.jpg", "5.jpg", "6.jpg"]
    assert all(u.startswith("https://hhmg2.b-cdn.net/") for u in imgs)
    assert len(http.calls) == 2  # 第 1 页 + next_page 翻页一页
    assert http.calls[0].endswith("/server-1/")
    assert "/server-1/p/2/" in http.calls[1]


def test_fetch_comic_pages_stops_at_last_page():
    """末页无 next_page（server2 fixture 已摘除）→ 不追第 3 页。"""
    src = _source()
    http = _FakeHttp()
    c = _content(http)
    read_url, _detail_url = _work_card(_parse("detail.html"))
    c.fetch_comic_pages(src, read_url)
    assert len(http.calls) == 2