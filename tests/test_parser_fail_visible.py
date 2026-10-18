# -*- coding: utf-8 -*-
"""解析器失败可见（fail-visible）测试（test_parser_fail_visible.py）。

覆盖 D7：
- lxml 缺失时 parse_items 不再把整页当单条目（静默降级），而是返回空列表
  并记录一条 ERROR 日志（可观察失败）；
- lxml 解析失败时 parse 记录 ERROR 并向上抛出，不静默吞掉。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework import parser as parser_mod
from framework.parser import Parser

FIELDS = {"title": {"css": "a.title"}}


def test_fallback_url_selector_keeps_base_resolution():
    assert parser_mod._selector_attr({
        "fallback": [
            {"css": "img", "attr": "data-src"},
            {"css": "img", "attr": "src"},
        ]
    }) == "data-src"


def test_parse_items_without_lxml_is_empty_and_logs_error(monkeypatch, caplog):
    monkeypatch.setattr(parser_mod, "_WARNED_FALLBACK", False)
    p = Parser()
    p._lxml = False
    with caplog.at_level(logging.ERROR, logger="framework.parser"):
        items = p.parse_items(
            "<html><body><div class='item'><a class='title'>T</a></div></body></html>",
            {"css": ".item"},
            FIELDS,
            "https://example.test",
        )
    assert items == []  # 明确失败，不再返回「整页单条目」
    assert any("lxml" in r.getMessage() for r in caplog.records)


def test_parse_items_without_lxml_fails_visible_even_with_source_context(monkeypatch, caplog):
    monkeypatch.setattr(parser_mod, "_WARNED_FALLBACK", False)
    p = Parser()
    p._lxml = False
    with caplog.at_level(logging.ERROR, logger="framework.parser"):
        items = p.parse_items(
            "<html></html>",
            {"css": ".item"},
            FIELDS,
            "https://example.test",
            source_id="site_x",
            page_url="https://example.test/search",
        )
    assert items == []
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "site_x" in msg


def test_parse_failure_raises_and_logs(monkeypatch, caplog):
    p = Parser()
    if not p._lxml:
        return  # 无 lxml 环境下该分支不适用

    def _boom(*_a, **k):
        raise ValueError("bad html")

    monkeypatch.setattr(parser_mod.lxml.html, "fromstring", _boom)
    monkeypatch.setattr(parser_mod.lxml.html, "document_fromstring", _boom)
    with caplog.at_level(logging.ERROR, logger="framework.parser"):
        try:
            p.parse("<html>x</html>")
        except Exception:
            pass
        else:
            raise AssertionError("解析失败应向上抛出，而不是静默返回")
    assert any("解析失败" in r.getMessage() for r in caplog.records)


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
