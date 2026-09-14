# -*- coding: utf-8 -*-
"""请求头非 latin-1 编码测试（test_http_header_encoding.py）。

背景：requests/urllib 用 latin-1 编码请求头值。含中文的 Referer（章节 URL
未百分号编码，如 ho5ho 的 /中字h漫/...）会抛 UnicodeEncodeError，导致整个
请求失败（实测 ho5ho 全彩成人漫正文图 48/48「加载失败」）。HttpClient 应在
发请求前把非 latin-1 头值按 URL 规则百分号编码，保证可发送且语义等价。

覆盖：_latin1_header_value 纯函数三态 + _headers_with_ua 实际接线。全部离线。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.http import HttpClient, NetworkDefaults, _latin1_header_value


def _make():
    h = HttpClient(defaults=NetworkDefaults())
    h._system_proxy_enabled = True
    return h


def test_ascii_header_unchanged():
    assert _latin1_header_value("Mozilla/5.0") == "Mozilla/5.0"
    assert _latin1_header_value("https://www.ho5ho.com/") == "https://www.ho5ho.com/"


def test_non_ascii_referer_percent_encoded():
    raw = "https://www.ho5ho.com/中字h漫/abc/server-1/"
    out = _latin1_header_value(raw)
    # 可被 latin-1 编码（不再抛 UnicodeEncodeError）
    out.encode("latin-1")
    assert "中" not in out
    assert "%E4%B8%AD%E5%AD%97h%E6%BC%AB" in out
    # 结构字符与已编码部分保留
    assert out.startswith("https://www.ho5ho.com/")


def test_existing_percent_encoding_preserved():
    # 已百分号编码的 Referer 不应被二次编码（% 属 safe）
    raw = "https://www.ho5ho.com/%E4%B8%AD%E5%AD%97h%E6%BC%AB/x/"
    assert _latin1_header_value(raw) == raw


def test_non_str_passthrough():
    assert _latin1_header_value(123) == 123
    assert _latin1_header_value(None) is None


def test_headers_with_ua_sanitizes_referer():
    h = _make()
    out = h._headers_with_ua(
        {"User-Agent": "UA", "Referer": "https://www.ho5ho.com/中字h漫/x/"}
    )
    assert "中" not in out["Referer"]
    out["Referer"].encode("latin-1")  # 不抛异常即通过
