# -*- coding: utf-8 -*-
"""番茄混合字体动态解析（fanqie_font）接入测试。

逻辑：decrypter.translit 拿到页面 HTML（含 @font-face 混淆字体）时，先尝试
按字体字形动态解析（PUA->汉字，IoU 模板匹配）；字体不可达/无字体 → 安全
回落静态表，网络异常不泄露、不崩溃。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework import fanqie_font
from framework.config import SourceConfig
from framework.decrypter import Decrypter
from framework.http import HttpClient
from framework.search import Search

PUA = "\ue3ea"  # 静态表：→ 主


class _FakeHttp:
    defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def get_text(self, url, **kwargs):
        return "<html></html>"

    def close(self):
        pass


_SOURCE = {
    "$schema_version": 2,
    "$id": "fanqie-font-test",
    "$type": "novel",
    "$name": "番茄字体测试源",
    "$enabled": True,
    "$weight": 1.0,
    "$metadata": {"homepage": "https://fanqie.com/", "lang": "zh-CN", "region": "cn"},
    "transports": {"base_url": "https://fanqie.com"},
    "endpoints": {"search": {"base_url": "/s", "item": {"root_selector": {"css": "a"}, "fields": {"title": {"css": "a"}}}}, "detail": {"fields": {}}},
    "decryption": {
        "targets": {"title": {"strategy": "translit"}}
    },
    "constraints": {},
}


# --------------------------------------------------------------------- #
def test_extract_font_urls_plain_and_escaped():
    html = (
        '<style>@font-face{font-family:F;'
        'src:url("https://lf6-awef.bytetos.com/obj/awesome-font/c/abc.woff2")'
        ' format("woff2");}</style>'
        '<style>@font-face{font-family:B;font-style:bold;'
        'src:url("https://x/bold.woff2");}</style>'
    )
    urls = fanqie_font.extract_font_urls(html)
    assert any("abc.woff2" in u for u in urls)
    assert not any("bold" in u for u in urls) or "bold.woff2" not in str(urls)


def test_extract_font_urls_empty_html():
    assert fanqie_font.extract_font_urls("") == []
    assert fanqie_font.extract_font_urls("<html><body>无字体</body></html>") == []


# --------------------------------------------------------------------- #
def test_resolve_html_no_html_returns_none():
    resolved, mapping = fanqie_font.resolve_html("", "剑来\ue3ea")
    assert resolved is None
    assert mapping == {}


def test_resolve_html_no_pua_passthrough():
    resolved, mapping = fanqie_font.resolve_html("<html></html>", "纯文本无密文")
    assert resolved == "纯文本无密文"
    assert mapping == {}


def test_resolve_html_unreachable_font_safe():
    """字体 URL 网络不可达 → 不抛（resolve_html 内部捕获），返回 None。"""
    html = '<style>@font-face{src:url("https://x.invalid.nowhere/a.woff2")}</style>'
    resolved, mapping = fanqie_font.resolve_html(html, "剑\ue3ea来")
    assert resolved is None or resolved != "剑\ue3ea来"
    assert mapping == {}


# --------------------------------------------------------------------- #
def test_translit_falls_back_to_static_when_font_unresolvable():
    src = SourceConfig.from_dict(dict(_SOURCE), "<test>")
    http = _FakeHttp()
    dec = Decrypter(http)
    html = '<style>@font-face{src:url("https://x.invalid.nowhere/a.woff2")}</style>'
    out = dec.decrypt(src, "剑\ue3ea来", target="title", html=html)
    assert out == "剑主来"  # 静态表兜底（\ue3ea→主）


def test_translit_without_html_keeps_static_behavior():
    src = SourceConfig.from_dict(dict(_SOURCE), "<test>")
    dec = Decrypter(_FakeHttp())
    out = dec.decrypt(src, "剑\ue3ea来", target="title")
    assert out == "剑主来"


def test_translit_static_map_wins_when_dynamic_font_is_present(monkeypatch):
    """A fuzzy font match must not replace the verified bundled glyph table."""
    src = SourceConfig.from_dict(dict(_SOURCE), "<test>")
    dec = Decrypter(_FakeHttp())

    monkeypatch.setattr(
        fanqie_font,
        "resolve_urls",
        lambda urls, text: (text.replace(PUA, "错"), {PUA: "错"}),
    )
    out = dec.decrypt(src, f"剑{PUA}来", target="title", font_urls=["font.woff2"])
    assert out == "剑主来"


def test_translit_plain_source_unchanged():
    src = SourceConfig.from_dict(dict(_SOURCE), "<test>")
    plain = dict(_SOURCE)
    plain.pop("decryption")
    dec = Decrypter(_FakeHttp())
    out = dec.decrypt(
        SourceConfig.from_dict(plain, "<test>"), "剑\ue3ea来", target="title", html="<html/>"
    )
    assert out == "剑\ue3ea来"


def test_search_uses_fanqie_search_table():
    """官网搜索页使用 _search 表，不能套用正文表。"""
    source = dict(_SOURCE)
    source["decryption"] = {
        "targets": {
            "search_title": {
                "strategy": "translit",
                "map_module": "framework.fanqie_search_glyph_map",
                "map_attr": "FANQIE_SEARCH_GLYPH_MAP",
            }
        }
    }
    src = SourceConfig.from_dict(source, "<test>")
    out = Decrypter(_FakeHttp()).decrypt(src, "\ue3eb", target="search_title")
    assert out == "作"


def test_search_decrypter_enables_search_only_tables():
    """只有搜索专用策略时，HTML 搜索引擎也必须启用解密。"""
    source = dict(_SOURCE)
    source["decryption"] = {
        "targets": {
            "search_title": {
                "strategy": "translit",
                "map_module": "framework.fanqie_search_glyph_map",
                "map_attr": "FANQIE_SEARCH_GLYPH_MAP",
            }
        }
    }
    src = SourceConfig.from_dict(source, "<test>")
    search = Search(_FakeHttp(), None)
    dec = search._search_decrypter(src)
    assert dec is not None
    assert dec.decrypt(src, "\ue3eb", target="search_title") == "作"
