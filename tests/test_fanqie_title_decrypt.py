# -*- coding: utf-8 -*-
"""番茄标题/作者字体混淆解密接入测试。

对应 Q1：discovery 列表已解密 title/author，但搜索（search.py）与详情
（content.py）此前缺失 → GUI 搜索卡片/详情页显示乱码。本次把
decryption.targets.title/author（translit）接入两处，用真实字形表驱动：

- \uE3EA → 主（title 样例：最强\uE3EA人 → 最强主人）
- \uE3EC → 家（author 样例：作\uE3EC → 作家）

覆盖：
- search HTML selector 引擎（逐条还原，on_page 增量也是明文）
- search render=playwright 引擎（整批还原后再返回）
- search _decrypt_results 统一出口（yt-dlp / api_endpoints 引擎共用）
- content fetch_detail 详情 title/author
- 未配置解密策略的源 → 原样（零影响回归）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.config import SourceConfig
from framework.content import Content
from framework.decrypter import Decrypter
from framework.http import HttpClient
from framework.parser import Parser
from framework.search import Search, SearchResult

RAW_TITLE = "最强\uE3EA人"  # \uE3EA→主
EXPECT_TITLE = "最强主人"
RAW_AUTHOR = "作\uE3EC"  # \uE3EC→家
EXPECT_AUTHOR = "作家"

SEARCH_SOURCE = {
    "$schema_version": 2,
    "$id": "fanqie-title-test",
    "$type": "novel",
    "$name": "番茄标题测试源",
    "$enabled": True,
    "$weight": 1.0,
    "$metadata": {"homepage": "https://fanqie.com/", "lang": "zh-CN", "region": "cn"},
    "transports": {"base_url": "https://fanqie.com"},
    "endpoints": {
        "search": {
            "base_url": "/search/{keyword}",
            "method": "GET",
            "keyword_param": "keyword",
            "paginator": {"url_template": "/search/?keyword={keyword}&p={page}"},
            "item": {
                "root_selector": {"css": "div.result-card"},
                "fields": {
                    "title": {"css": "h3 a"},
                    "url": {"css": "h3 a", "attr": "href"},
                    "author": {"css": "div.author"},
                },
            },
        }
    },
    "decryption": {
        "targets": {
            "title": {"strategy": "translit"},
            "author": {"strategy": "translit"},
        }
    },
    "constraints": {"search": {"max_pages": 2, "max_results": 10}},
}

NO_DECRYPT_SOURCE = dict(SEARCH_SOURCE)
NO_DECRYPT_SOURCE["$id"] = "plain-source"
NO_DECRYPT_SOURCE.pop("decryption")


class _FakeHttp:
    def __init__(self, html):
        self._html = html
        from framework.http import NetworkDefaults

        self.defaults = NetworkDefaults()

    def get_text(self, url, **kwargs):
        return self._html

    def post_form(self, url, form_data=None, **kwargs):
        return self._html


def _search_html(cards_html: str, source: dict = None) -> list:
    src = SourceConfig.from_dict(source or SEARCH_SOURCE, "<test>")
    search = Search(http=_FakeHttp(cards_html), parser=Parser())
    return search._search_html(src, "kw")


# --------------------------------------------------------------------- #
# 1) HTML selector 引擎：逐条解密 + on_page 增量也是明文
# --------------------------------------------------------------------- #
def test_html_search_results_decrypted():
    html = (
        '<html><body><div class="result-card">'
        f'<h3><a href="/v/1.html">{RAW_TITLE}</a></h3>'
        f'<div class="author">{RAW_AUTHOR}</div></div></body></html>'
    )
    results = _search_html(html)
    assert len(results) == 1
    assert results[0].title == EXPECT_TITLE
    assert results[0].author == EXPECT_AUTHOR


def test_html_search_on_page_slice_decrypted():
    """on_page 增量回调拿到的就是解密后明文字段。"""
    html = (
        '<html><body><div class="result-card">'
        f'<h3><a href="/v/9.html">{RAW_TITLE}</a></h3>'
        f'<div class="author">{RAW_AUTHOR}</div></div></body></html>'
    )
    src = SourceConfig.from_dict(SEARCH_SOURCE, "<test>")
    search = Search(http=_FakeHttp(html), parser=Parser())
    slices = []

    search._search_html(src, "kw", on_page=lambda _s, _p, new: slices.extend(list(new)))

    assert slices and slices[0].title == EXPECT_TITLE
    assert slices[0].author == EXPECT_AUTHOR


# --------------------------------------------------------------------- #
# 2) render=playwright 引擎：整批还原后再返回（fanqie 实际路径）
# --------------------------------------------------------------------- #
def test_rendered_search_results_decrypted(monkeypatch):
    raw_results = [
        SearchResult(title=RAW_TITLE, url="https://fanqie.com/page/1/", author=RAW_AUTHOR)
    ]

    def _fake_rendered(self, source, abs_url, item_cfg, keyword):
        return list(raw_results)

    monkeypatch.setattr(Search, "_search_html_rendered", _fake_rendered)
    src = SourceConfig.from_dict(SEARCH_SOURCE, "<test>")
    src.raw["endpoints"]["search"]["render"] = "playwright"
    search = Search(http=_FakeHttp("<html></html>"), parser=Parser())

    results = search._search_html(src, "kw")
    assert len(results) == 1
    assert results[0].title == EXPECT_TITLE
    assert results[0].author == EXPECT_AUTHOR


def test_decrypt_results_covers_api_ytdlp_engines():
    """search_one 统一出口 _decrypt_results：yt-dlp / api_endpoints 共用。"""
    raw = [
        SearchResult(title=RAW_TITLE, url="https://fanqie.com/page/1/", author=RAW_AUTHOR)
    ]
    src = SourceConfig.from_dict(SEARCH_SOURCE, "<test>")

    out = Search(http=_FakeHttp(""), parser=Parser())._decrypt_results(src, raw, None)
    assert out[0].title == EXPECT_TITLE
    assert out[0].author == EXPECT_AUTHOR


def test_decrypt_results_no_op_without_config():
    raw = [SearchResult(title=RAW_TITLE, url="https://x.com/v/1", author=RAW_AUTHOR)]
    src = SourceConfig.from_dict(NO_DECRYPT_SOURCE, "<test>")

    out = Search(http=_FakeHttp(""), parser=Parser())._decrypt_results(src, raw, None)
    assert out[0].title == RAW_TITLE  # 未配置 → 原样（不误解密）
    assert out[0].author == RAW_AUTHOR


# --------------------------------------------------------------------- #
# 3) content 详情 title/author
# --------------------------------------------------------------------- #
_DETAIL_HTML = f"""<html><body>
<h1>{RAW_TITLE}</h1>
<div class="author-name-text">{RAW_AUTHOR}</div>
<p class="book-cover-img">https://fanqie.com/c.jpg</p>
</body></html>"""


class _DetailChecker:
    pass


class _DetailHttp:
    defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def get_text(self, url, **kwargs):
        return _DETAIL_HTML

    def close(self):
        pass


def _make_detail_source(decrypt: bool = True):
    class S:
        source_id = "fanqie-detail-test"
        base_url = "https://fanqie.com"
        content_type = "novel"
        _decrypt = decrypt
        _raw = {
            "endpoints": {
                "detail": {
                    "fields": {
                        "title": {"css": "h1"},
                        "author": {"css": ".author-name-text"},
                    }
                }
            }
        }

        @property
        def raw(self):
            if self._decrypt:
                self._raw["decryption"] = {
                    "targets": {
                        "title": {"strategy": "translit"},
                        "author": {"strategy": "translit"},
                    }
                }
            return self._raw

        def get_detail_config(self):
            return self._raw["endpoints"]["detail"]

        def get_discovery_config(self):
            return {}

        def get_search_config(self):
            return {}

        def transports(self):
            return {}

        def request_headers(self):
            return {}

        def proxy_pool(self):
            return None

    return S()


def test_detail_title_author_decrypted():
    http = _DetailHttp()
    content = Content(http, Parser(), _DetailChecker(), decrypter=Decrypter(http))
    detail = content.fetch_detail(_make_detail_source(), "https://fanqie.com/page/1/")
    assert detail.title == EXPECT_TITLE
    assert detail.author == EXPECT_AUTHOR


def test_detail_unchanged_without_decrypter():
    """未注入 Decrypter（老调用方）→ 标题原样，不崩。"""
    http = _DetailHttp()
    content = Content(http, Parser(), _DetailChecker())
    detail = content.fetch_detail(_make_detail_source(), "https://fanqie.com/page/1/")
    assert detail.title == RAW_TITLE


def test_detail_unchanged_for_plain_source():
    """源未配置解密策略 → 原样（零影响回归）。"""
    http = _DetailHttp()
    content = Content(http, Parser(), _DetailChecker(), decrypter=Decrypter(http))
    detail = content.fetch_detail(_make_detail_source(decrypt=False), "https://fanqie.com/page/1/")
    assert detail.title == RAW_TITLE


# --------------------------------------------------------------------- #
# 4) 字形表一致性守卫
# --------------------------------------------------------------------- #
def test_glyph_map_has_used_entries():
    from framework.data.fanqie_glyph_map import FANQIE_GLYPH_MAP

    assert FANQIE_GLYPH_MAP["\uE3EA"] == "主"
    assert FANQIE_GLYPH_MAP["\uE3EC"] == "家"