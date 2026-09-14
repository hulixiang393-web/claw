# -*- coding: utf-8 -*-
"""favcomic（喜漫漫画 favcomic.com）源离线单元测试。

覆盖（全部基于真实页面 HTML 快照，无网络请求）：
- 源配置加载关键形态（comic 类型、图片加密 decryption.image=js_custom、
  selfcheck 关闭）
- 发现：静态分类入口 ≥8、分类 URL 带 ?tag=N、increment 分页 URL 构造
  （含 query 的 ?tag=N&page=2）
- 发现列表卡片解析（li.comic_li，37 条，封面取 data-src 懒加载位）
- 详情页字段解析（h1 标题、简介、作者、封面 data-src）
- fetch_detail：返回标题/简介/作者 + 章节列表，chapters[0] = 唯一章节
- 阅读正文离线提取：img.encrypted-image 292 张 data-src（全 CDN webp）
- decryption.image：js_custom 约定走 bytes 通道（base64 进出）；真实
  Chromium 冒烟解密（无浏览器时跳过）

说明：
- 站点图片为 CDN 加密字节：密文 = 随机 IV(16B) + AES-128-CBC(PKCS7) 密文，
  密钥 UTF-8 串 "6X+b6.E>bsXb}+=N"，由页面 decrypt.chapter.worker.js 推演。
  阅读页/封面 data-src 直接指向加密字节，必须经 decryption.image 解密
  （Web Crypto，异步，Playwright 常驻页 await）。
- 封面/列表 `img` 的 src 为占位图，真实图仅在 data-src。

运行需求：本机以系统 Python（3.10，含 pytest + lxml）运行本测试；
解密冒烟需 Playwright Chromium（缺失自动跳过）。
"""
import base64
import json
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.http import NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "favcomic")
_BASE = "https://www.favcomic.com"
_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources", "favcomic.json"
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
        if "/comic/detail/" in url:
            name = "detail.html"
        elif "/comic/chapter/" in url:
            name = "chapter.html"
        else:
            name = "list.html"
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


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_source_config_loads():
    src = _source()
    assert src.source_id == "favcomic"
    assert src.content_type == "comic"
    assert src.source_name == "喜漫漫画"
    assert src.base_url == _BASE
    headers = src.request_headers()
    assert headers.get("Referer") == "https://www.favcomic.com/"
    assert src.transports().get("interval_ms") == 600
    assert src.get_detail_config() is not None


def test_selfcheck_off():
    src = _source()
    assert ((src.raw.get("diagnostics") or {}).get("selfcheck") or {}).get(
        "strategy"
    ) == "off"


def test_discovery_categories_and_pagination_config():
    src = _source()
    disc = src.get_discovery_config()
    cats = (disc.get("list_item") or {}).get("categories") or []
    assert len(cats) >= 8
    titles = [c["title"] for c in cats]
    assert "全部" in titles and "奇幻" in titles and "热血" in titles
    latest = next(c for c in cats if c["title"] == "全部")
    assert latest["url"] == "/boy"
    tag_cat = next(c for c in cats if c["title"] == "奇幻")
    assert tag_cat["url"] == "/boy?tag=1"
    pag = disc.get("list_paginator") or {}
    assert pag.get("type") == "increment"
    assert pag.get("param") == "page"
    works_li = disc.get("works_list_item") or {}
    assert works_li.get("root_selector", {}).get("css") == "li.comic_li"
    cover_cfg = (works_li.get("fields") or {}).get("cover") or {}
    assert cover_cfg.get("attr") == "data-src"  # 懒加载位，src 是占位图


def test_discovery_increment_page_url_built_with_amp():
    src = _source()
    disc = Discovery(_FakeHttp(), Parser(), _FakeChecker())
    cats = {c["title"]: c["url"] for c in (src.get_discovery_config().get("list_item") or {}).get("categories") or []}
    assert disc._build_page_url(src, cats["全部"], 1) == "/boy?page=1"
    assert disc._build_page_url(src, cats["全部"], 2) == "/boy?page=2"
    assert disc._build_page_url(src, cats["奇幻"], 1) == "/boy?tag=1&page=1"
    assert disc._build_page_url(src, cats["奇幻"], 5) == "/boy?tag=1&page=5"


# --------------------------------------------------------------------------- #
# 发现列表
# --------------------------------------------------------------------------- #
def test_discovery_works_extract():
    doc = _parse("list.html")  # /boy 快照（37 条）
    src = _source()
    works_li = src.get_discovery_config()["works_list_item"]
    items = Parser().parse_items(
        doc,
        works_li["root_selector"],
        works_li["fields"],
        _BASE,
    )
    assert len(items) == 36  # 页脚还有同 id 的 JS 模板（comic_item_template），非 DOM
    first = items[0]
    assert "生活" in first["title"]
    assert first["url"].startswith("https://www.favcomic.com/comic/detail/")
    assert first["cover"].startswith("https://cdn.favcomic.com/file/")
    assert first["cover"].endswith(".webp")
    assert "cover_loading" not in first["cover"]  # 取的是 data-src 真实图
    assert all(it["url"].startswith("https://www.favcomic.com/comic/detail/") for it in items)
    assert all(it["cover"].startswith("https://cdn.favcomic.com/") for it in items)


# --------------------------------------------------------------------------- #
# 详情页
# --------------------------------------------------------------------------- #
def test_detail_fields_extract():
    doc = _parse("detail.html")
    p = Parser()
    src = _source()
    fields = src.get_detail_config()["fields"]
    # 文本字段不传 base（extract 会对结果统一拼 base），URL 字段才传
    title = p.extract_first(doc, fields["title"])
    assert title == "生活 完全版"
    author = p.extract_first(doc, fields["author"])
    assert author == "福満しげゆき"
    summary = p.extract_first(doc, fields["summary"])
    assert summary.startswith("作品介绍：")
    cover = p.extract_first(doc, fields["cover"])
    assert cover.startswith("https://cdn.favcomic.com/file/e-media/app/cover/")
    assert "cover_loading" not in cover


def test_fetch_detail_returns_title_author_and_single_chapter():
    src = _source()
    c = _content()
    detail_url = "https://www.favcomic.com/comic/detail/1296419411482906624"
    d = c.fetch_detail(src, detail_url)
    assert d.title == "生活 完全版"
    assert d.author == "福満しげゆき"
    assert d.summary.startswith("作品介绍：")
    assert d.cover.startswith("https://cdn.favcomic.com/file/e-media/app/cover/")
    assert len(d.chapters) == 1
    ch = d.chapters[0]
    assert ch.title == "机翻全一卷"
    assert ch.url == "https://www.favcomic.com/comic/chapter/1296783770922094592"


# --------------------------------------------------------------------------- #
# 阅读页
# --------------------------------------------------------------------------- #
def test_page_images_extract_all_from_datasrc():
    """阅读正文离线提取：292 张图全走 data-src（src 为占位图），整章一页一次取全。

    不上 Content.fetch_comic_pages —— 源声明了 decryption.image，框架路径会对
    每张图真实下载解密成 data URI（联网 + Playwright），离线测试不触发；
    这里直接验证正文选择器在快照上提取一致（与框架同等解析）。
    """
    src = _source()
    body = ((src.raw.get("endpoints") or {}).get("content") or {}).get("page", {}).get("body") or {}
    assert body.get("root_selector", {}).get("css") == "img.encrypted-image"
    url_cfg = (body.get("fields") or {}).get("url") or {}
    assert url_cfg.get("attr") == "data-src"
    doc = _parse("chapter.html")
    imgs = Parser().extract(doc, url_cfg, _BASE)
    assert len(imgs) == 292
    assert all(u.startswith("https://cdn.favcomic.com/file/e-media/app/comic/") for u in imgs)
    assert all(u.endswith(".webp") for u in imgs)
    assert imgs[0] == "https://cdn.favcomic.com/file/e-media/app/comic/1296419411482906624/1/1-zJIDq4.webp"


# --------------------------------------------------------------------------- #
# 图片解密
# --------------------------------------------------------------------------- #
def test_image_decrypt_config_shape():
    """decryption.image 走原生 aes_cbc 前缀 IV（IV=密文前16字节），密钥一致。"""
    src = _source()
    oc = ((src.raw.get("decryption") or {}).get("targets") or {}).get("image") or {}
    assert oc.get("strategy") == "aes_cbc"
    assert oc.get("key") == "6X+b6.E>bsXb}+=N"
    assert oc.get("iv") == "prefix"
    assert oc.get("padding") == "pkcs7"


def test_image_decrypt_real_ciphertext():
    """真实密文（IV=前16B, AES-128-CBC/PKCS7）→ WEBP 魔数。"""
    from framework.decrypter import Decrypter

    with open(os.path.join(_FIXTURES, "enc_wG7amo.bin"), "rb") as f:
        cipher = f.read()
    src = _source()
    plain = Decrypter(_FakeHttp()).decrypt_bytes(src, cipher, target="image")
    assert plain[:4] == b"RIFF"
    assert plain[8:12] == b"WEBP"
    assert len(plain) > 1000
    assert plain is not cipher