# -*- coding: utf-8 -*-
"""详情页二级缓存 TTL 测试（test_page_cache_ttl.py）。

覆盖 D10：_get_detail_html 写 Redis 二级缓存时携带 TTL（_PAGE_HTML_TTL），
避免旧「永久」写导致详情页缓存永久驻留。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.config import SourceConfig
from framework.content import Content

SRC = {
    "$schema_version": 2,
    "$id": "site_a",
    "$type": "novel",
    "$name": "站点A",
    "transports": {"base_url": "https://example.test"},
    "endpoints": {"search": {"base_url": "/search", "method": "GET"}},
}


class _FakeCache:
    def __init__(self):
        self.store = {}
        self.set_calls = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        self.set_calls.append((key, ttl))
        self.store[key] = value


def test_detail_html_redis_write_carries_ttl():
    cache = _FakeCache()
    cont = Content(None, None, None, cache=cache)
    source = SourceConfig.from_dict(SRC, "<a>")
    cont._get = lambda *a, **k: "<html>detail</html>"

    html = cont._get_detail_html(source, "/book/1", "https://example.test/book/1")
    assert html == "<html>detail</html>"
    assert cache.set_calls, "应写入二级缓存"
    key, ttl = cache.set_calls[-1]
    assert key == "page:site_a:https://example.test/book/1"
    assert ttl == Content._PAGE_HTML_TTL
    assert ttl and ttl > 0  # 非永久


def test_detail_html_redis_hit_skips_fetch():
    cache = _FakeCache()
    cache.store["page:site_a:https://example.test/book/1"] = "<html>cached</html>"
    cont = Content(None, None, None, cache=cache)
    source = SourceConfig.from_dict(SRC, "<a>")

    def _boom(*a, **k):
        raise AssertionError("缓存命中不应再抓取")

    cont._get = _boom
    html = cont._get_detail_html(source, "/book/1", "https://example.test/book/1")
    assert html == "<html>cached</html>"
    assert cache.set_calls == []  # 命中不写


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
