# -*- coding: utf-8 -*-
"""详情封面兜底链路测试（test_cover_fallback.py）。

覆盖「收藏/书架缺封面」修复：
- fetch_cover 与 fetch_detail 同链路：选择器封面为空/占位/base_url 垃圾值时，
  按源 cover 配置的 regex / state 从 SSR JSON 抽真实封面；
- _extract_cover_fallback 对「current == base_url」的退化值强制走兜底，
  防止把 base_url 写进收藏库（历史 bug：番茄收藏 cover 被写成 https://fanqienovel.com）。

真实站点取证（2026-09-12）：番茄小说详情页 <img class="book-cover-img">
的 src 被 SSR 模板清空 → extract_first 退化返回 base_url；真实封面位于
window.__INITIAL_STATE__ / "image":["https://..."] 的 JSON-LD。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.config import SourceConfig
from framework.content import Content
from framework.parser import Parser

RAW = {
    "$schema_version": 2,
    "$id": "fanqie-fake",
    "$type": "novel",
    "$name": "番茄(假)",
    "$enabled": True,
    "$weight": 1.0,
    "transports": {"base_url": "https://x.example"},
    "endpoints": {
        "detail": {
            "fields": {
                "cover": {
                    "css": ".cover img",
                    "attr": "src",
                    "placeholder": "5cb03bd",
                    "regex": '"image":\\["(https://[^"]+)"\\]',
                    "state": "page.thumbUri",
                }
            }
        }
    },
}

_REAL_IMG = "https://cdn.x.example/real.jpg"
_SCRIPT_IMAGES = (
    '<script>window.__INITIAL_STATE__={"a":1};document.q={};'
    f'"image":["{_REAL_IMG}"]</script>'
)


class _Checker:
    pass


class _FakeHttp:
    cache = None

    def __init__(self, html=""):
        self.html = html
        from framework.http import NetworkDefaults

        self.defaults = NetworkDefaults()

    def get_text(self, url, **kw):
        return self.html

    def close(self):
        pass


def _content(html):
    from framework.cache_service import RedisLikeStore

    store = RedisLikeStore(quota=1024 * 1024, persist_path=None)
    return Content(_FakeHttp(html), Parser(), _Checker(), cache=store)


SRC = SourceConfig.from_dict(RAW, "<mem>")


# ------------------------------------------------------------------------- #
def test_cover_fallback_placeholder_src():
    """选择器命中但 src 是占位图 → regex 兜底拿真实封面。"""
    c = _content('<div class="cover"><img src="//p1.x.example/5cb03bdabc"/></div>' + _SCRIPT_IMAGES)
    assert c.fetch_cover(SRC, "https://x.example/book/1") == _REAL_IMG


def test_cover_fallback_empty_src():
    """选择器命中但 src 为空（番茄 SSR 模板清空）→ 兜底拿真实封面。"""
    c = _content('<div class="cover"><img src=""/></div>' + _SCRIPT_IMAGES)
    assert c.fetch_cover(SRC, "https://x.example/book/1") == _REAL_IMG


def test_cover_valid_kept_as_is():
    """选择器直接命中有效封面且无 regex 配置 → 原样返回。"""
    c = _content('<div class="cover"><img src="https://cdn.x.example/good.jpg"/></div>')
    detail_cfg = dict(SRC.get_detail_config())
    fields = dict(detail_cfg["fields"])
    fields["cover"] = {"css": ".cover img", "attr": "src"}
    detail_cfg["fields"] = fields
    from framework.config import SourceConfig

    plain = SourceConfig.from_dict(
        {k: v for k, v in RAW.items()}
        | {"endpoints": {"detail": detail_cfg}},
        "<mem>",
    )
    assert c.fetch_cover(plain, "https://x.example/book/1") == "https://cdn.x.example/good.jpg"


def test_cover_fallback_no_match_returns_empty():
    """带 regex 配置但无任意兜底命中（无 SSR JSON）→ 返回空，绝不写 base_url 垃圾值。"""
    c = _content('<div class="cover"><img src="https://cdn.x.example/good.jpg"/></div>')
    got = c.fetch_cover(SRC, "https://x.example/book/1")
    assert got == ""
    assert got != SRC.base_url


def test_cover_fallback_state_from_initial_state():
    """regex 未配置/未命中时，state（INITIAL_STATE JSONPath）兜底。"""
    html = (
        '<div class="cover"><img src="//p1.x.example/5cb03bd"/></div>'
        '<script>window.__INITIAL_STATE__ = {"page": {"thumbUri": "https://cdn.x.example/state.jpg"}};</script>'
    )
    c = _content(html)
    cover_sel = (SRC.get_detail_config()["fields"])["cover"]
    got = c._extract_cover_fallback(SRC, html, cover_sel, "https://x.example")
    assert got == "https://cdn.x.example/state.jpg"


def test_cover_fallback_baseurl_degrades_to_regex():
    """current 退化为 base_url 本体 → 视为无效，强制走 regex 兜底。"""
    c = _content(_SCRIPT_IMAGES)
    cover_sel = (SRC.get_detail_config()["fields"])["cover"]
    got = c._extract_cover_fallback(
        SRC, _SCRIPT_IMAGES, cover_sel, "https://x.example"
    )
    assert got == _REAL_IMG


def test_cover_fallback_valid_url_passthrough():
    """非占位且非 base_url 的有效封面 → 原样返回（不绕过避免逻辑）。"""
    c = _content(_SCRIPT_IMAGES)
    cover_sel = (SRC.get_detail_config()["fields"])["cover"]
    good = "https://cdn.x.example/set.jpg"
    assert c._extract_cover_fallback(SRC, _SCRIPT_IMAGES, cover_sel, good) == good


def test_fetch_cover_no_cover_config_returns_empty():
    """源未配置 detail.fields.cover → 空（无网络、无异常）。"""
    raw = {"$schema_version": 2, "$id": "nosrc", "$type": "novel",
           "$name": "无封面源", "$enabled": True, "$weight": 1.0,
           "transports": {"base_url": "https://n.example"},
           "endpoints": {"detail": {"fields": {"title": {"css": "h1"}}}}}
    ns = SourceConfig.from_dict(raw, "<mem>")
    c = _content('<h1>标题</h1>')
    assert c.fetch_cover(ns, "https://n.example/book/1") == ""


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))