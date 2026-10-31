# -*- coding: utf-8 -*-
"""transports.prefer_direct / prefer_proxy 与解析预算的源级开关。"""
from types import SimpleNamespace

import framework.http as http_mod
from framework.config import SourceConfig
from framework.content import Content
from framework.external_player import ExternalPlayer
from framework.parser import Parser


def _source(transports_extra):
    transports = {"base_url": "https://example.com", "retries": 0}
    transports.update(transports_extra)
    return SourceConfig.from_dict({
        "$schema_version": 2,
        "$id": "demo-direct",
        "$type": "novel",
        "$name": "演示",
        "transports": transports,
        "endpoints": {
            "content": {
                "chapter": {
                    "list": {"root_selector": {"css": "ul li a"},
                             "fields": {"title": {"css": "a"},
                                        "url": {"css": "a", "attr": "href"}}},
                    "body": {"selector": {"css": ".c"}},
                }
            }
        },
    })


class _Http:
    defaults = SimpleNamespace(timeout=5, retries=0, interval_ms=0)

    def __init__(self):
        self.calls = []

    def get_text(self, url, **kwargs):
        self.calls.append(url)
        return "<div class='c'><p>正文一</p><p>正文二</p></div>"


def test_note_direct_preferred_marks_host_proxy_unusable():
    http_mod._reset_proxy_failures()
    try:
        http_mod.note_direct_preferred("https://quanben-xiaoshuo.com/?page=1")
        assert http_mod._proxy_failed_for("quanben-xiaoshuo.com") is True
        assert http_mod._proxy_failed_for("other.example") is False
    finally:
        http_mod._reset_proxy_failures()


def test_source_prefer_direct_calls_note(monkeypatch):
    seen = []
    monkeypatch.setattr(http_mod, "note_direct_preferred", lambda url: seen.append(url))
    queried = []
    monkeypatch.setattr(http_mod, "note_proxy_preferred", lambda url: queried.append(url))

    http = _Http()
    Content(http, Parser(), None).fetch_chapter(_source({"prefer_direct": True}),
                                               "https://example.com/1/1.html")
    assert seen and seen[0].startswith("https://example.com")
    assert queried == []


def test_source_prefer_proxy_takes_precedence(monkeypatch):
    seen, queried = [], []
    monkeypatch.setattr(http_mod, "note_direct_preferred", lambda url: seen.append(url))
    monkeypatch.setattr(http_mod, "note_proxy_preferred", lambda url: queried.append(url))

    http = _Http()
    Content(http, Parser(), None).fetch_chapter(
        _source({"prefer_proxy": True, "prefer_direct": True}),
        "https://example.com/1/1.html",
    )
    assert queried and seen == []


def test_external_player_honours_budget_from_source_config():
    player = ExternalPlayer(_Http())
    # from 列表不匹配 → 直接返回空串，只验证预算被源配置覆盖
    out = player.resolve({"from": ["nope"], "budget_s": 8}, {"url": "x"}, "https://e.com/play")
    assert out == ""
    assert player._budget == 8.0
