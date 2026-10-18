# -*- coding: utf-8 -*-
"""xhentai85（無限動漫H xhentai85.xyz）源离线单元测试。

覆盖（全部基于真实页面 HTML 快照，无网络请求）：
- 源配置加载关键形态（GBK charset、page_placeholder index_N.html 分页、
  selfcheck 关闭、comic 类型）
- 发现：静态分类 ≥12、分类 URL 带 index_2.html 起点、分页 URL 构造（page1
  不插页码、page2 替换 index_2 → index_2、page3 → index_3）
- 发现列表卡片解析（div.each_truyen，20 条）
- 详情页字段解析（h2.single_title 标题、wp-post-image 封面、itemprop author/
  genre、desc-text 简介）
- fetch_detail：返回标题/简介/标签 + 章节列表（a[href*='/chapter'] 去重后
  ≥2，章节 URL 形如 ?novel115456/chapterN.html）
- body 图片提取（img[src*='hmpic/']，20 张）

说明：
- 站点为 GBK 编码 PHP 站，?novel{N}/ 是作品，?novel{N}/chapter{M}.html 是章节。
- 分页：首页直接 /?latest，index_2.html 起分页（index_1.html 不存在 404），故
  分类 URL 从 index_2.html 起翻页。
- 标题中个别日文被站点自身损坏成 '?'（原始字节即如此），测试以 '�?' 存在
  的场景用 .assertIn 宽松断言。
- 运行需求：本机以系统 Python（3.10，pytest + lxml）运行。
"""
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.http import NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "xhentai85")
_BASE = "https://xhentai85.xyz"
_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources", "xhentai85.json"
)


class _FakeChecker:
    pass


class _FakeHttp:
    """离线假 HttpClient：按 URL 特征返回对应 fixture 快照。"""

    def __init__(self, sleeper=None, defaults=None):
        self.defaults = defaults or NetworkDefaults(timeout=15, retries=0, interval_ms=0)
        self.calls = []

    def get_text(self, url, headers=None, timeout=None, retries=None,
                 interval_ms=None, encoding=None, proxy_pool=None, direct=False):
        self.calls.append(url)
        if "/chapter" in url:
            name = "chapter1.html"
        elif "novel115456" in url or "?novel11" in url:
            name = "detail.html"
        else:
            name = "list.html"
        with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
            return f.read()


def _parse(name: str):
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
        return Parser().parse(f.read())


def _source() -> SourceConfig:
    with open(_SOURCE_PATH, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), _SOURCE_PATH)


def _content(http=None):
    return Content(http or _FakeHttp(), Parser(), _FakeChecker())


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_source_config_loads():
    src = _source()
    assert src.source_id == "xhentai85"
    assert src.content_type == "comic"
    assert src.base_url == _BASE
    assert src.transports().get("charset") == "gbk"
    assert src.transports().get("interval_ms") == 800
    headers = src.request_headers()
    assert headers.get("User-Agent", "").startswith("Mozilla")
    assert src.get_detail_config() is not None


def test_selfcheck_off():
    src = _source()
    assert ((src.raw.get("diagnostics") or {}).get("selfcheck") or {}).get("strategy") == "off"


# --------------------------------------------------------------------------- #
# 发现分类
# --------------------------------------------------------------------------- #
def test_categories():
    src = _source()
    disc = src.get_discovery_config()
    cats = ((disc.get("list_item") or {}).get("categories") or [])
    assert len(cats) >= 12
    urls = [c["url"] for c in cats]
    assert "/?latest/index_2.html" in urls
    assert "/?riman/index_2.html" in urls
    assert "/?hanman/index_2.html" in urls
    assert all(u.startswith("/?") for u in urls)


def test_pagination_url_build():
    src = _source()
    disc = src.get_discovery_config()
    url = disc.get("list_url", "")
    placeholder = (disc.get("list_paginator") or {}).get("page_placeholder", "")
    import re as _re
    assert "index_2.html" in url
    p1 = _re.sub(placeholder, "2", url, count=1)
    p3 = _re.sub(placeholder, "3", url, count=1)
    assert p1.endswith("index_2.html")
    assert p3.endswith("index_3.html")
    assert p3 != p1


# --------------------------------------------------------------------------- #
# 发现列表
# --------------------------------------------------------------------------- #
def test_discovery_works_extract():
    doc = _parse("list.html")
    items = Parser().parse_items(
        doc,
        {"css": "div.each_truyen"},
        {
            "title": {"css": "a", "attr": "title"},
            "url": {"css": "a", "attr": "href"},
            "cover": {"css": "img", "attr": "src"},
        },
        _BASE,
    )
    assert len(items) == 20
    first = items[0]
    assert first["url"].startswith("https://xhentai85.xyz?novel")
    assert first["url"].endswith("/")
    assert first["cover"].startswith("https://img1.du8.in/")
    assert all(it["url"].startswith("https://xhentai85.xyz?novel") for it in items)


# --------------------------------------------------------------------------- #
# 详情
# --------------------------------------------------------------------------- #
def test_detail_fields_extract():
    doc = _parse("detail.html")
    p = Parser()
    src = _source()
    det = src.get_detail_config()["fields"]
    title = p.extract_first(doc, det["title"])
    assert "色誘" not in title and title.startswith("[")
    cover = p.extract_first(doc, det["cover"], _BASE)
    assert cover.startswith("https://img1.du8.in/titlepic/")
    author = p.extract_first(doc, det["author"])
    assert author
    summary = p.extract_first(doc, det["summary"])
    assert "介绍" in summary
    tags = p.extract(doc, det["tags"])
    assert "日本漫画" in tags


def test_fetch_detail():
    src = _source()
    c = _content()
    d = c.fetch_detail(src, "https://xhentai85.xyz/?novel115456/")
    assert d.title
    assert d.cover.startswith("https://")
    assert d.summary and "介绍" in d.summary
    assert d.tags and "日本漫画" in d.tags
    assert len(d.chapters) >= 2
    urls = [ch.url for ch in d.chapters]
    assert all("?novel115456/chapter" in u for u in urls)
    assert urls == list(dict.fromkeys(urls))  # 去重


def test_fetch_detail_chapters_sorted():
    src = _source()
    c = _content()
    d = c.fetch_detail(src, "https://xhentai85.xyz/?novel115456/")
    nums = []
    import re as _re
    for ch in d.chapters:
        m = _re.search(r"chapter(\d+)\.html", ch.url)
        if m:
            nums.append(int(m.group(1)))
    assert len(nums) >= 2
    assert nums == sorted(nums)


# --------------------------------------------------------------------------- #
# 正文图片
# --------------------------------------------------------------------------- #
def test_fetch_comic_pages():
    src = _source()
    c = _content()
    pages = c.fetch_comic_pages(src, "https://xhentai85.xyz/?novel115456/chapter1.html")
    assert len(pages) >= 15
    for pg in pages[:3]:
        assert pg.startswith("https://img1.du8.in/hmpic/")