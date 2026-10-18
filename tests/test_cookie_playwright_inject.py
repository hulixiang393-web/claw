# -*- coding: utf-8 -*-
"""CookieManager → Playwright add_cookies 注入测试（test_cookie_playwright_inject.py）。

回归：番茄搜索页需登录态渲染（服务端对未登录返回空 body）。GUI 登录弹窗保存的
cookie 需能原样转成 Playwright add_cookies 供 fetch_rendered_* 注入。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.cookie_manager import CookieManager


def _make_manager() -> CookieManager:
    return CookieManager(tempfile.mkdtemp())


def test_to_playwright_cookies_basic():
    mgr = _make_manager()
    mgr.save(
        "fanqie",
        [
            {"name": "sessionid", "value": "abc123", "domain": ".fanqienovel.com", "path": "/", "secure": True},
            {"name": "ttwid", "value": "xyz", "domain": "fanqienovel.com", "path": "/", "secure": False},
        ],
        logged_at=1710000000,
    )
    out = mgr.to_playwright_cookies("fanqie")
    by_name = {c["name"]: c for c in out}
    assert set(by_name) == {"sessionid", "ttwid"}
    assert by_name["sessionid"]["domain"] == "fanqienovel.com"  # 前导点已去除
    assert by_name["sessionid"]["value"] == "abc123"
    assert by_name["sessionid"]["secure"] is True
    assert by_name["ttwid"]["path"] == "/"


def test_to_playwright_cookies_dedup_same_name_same_domain():
    mgr = _make_manager()
    mgr.save(
        "s1",
        [
            {"name": "a", "value": "v1", "domain": ".x.com"},
            {"name": "a", "value": "v2", "domain": ".x.com"},
            {"name": "b", "value": "u", "domain": ".y.com"},
        ],
    )
    out = mgr.to_playwright_cookies("s1")
    names = [(c["name"], c["domain"]) for c in out]
    assert len(names) == len(set(names))
    # 同名同域只留一个
    assert sum(1 for n, d in names if n == "a") == 1


def test_to_playwright_cookies_skips_missing_domain():
    mgr = _make_manager()
    mgr.save(
        "s2",
        [{"name": "nodomain", "value": "x", "domain": ""}, {"name": "ok", "value": "y", "domain": ".z.com"}],
    )
    out = mgr.to_playwright_cookies("s2")
    assert [c["name"] for c in out] == ["ok"]


def test_to_playwright_cookies_missing_file():
    mgr = _make_manager()
    assert mgr.to_playwright_cookies("ghost") == []


def test_search_accepts_cookie_manager():
    """Search 构造可接收 cookie_manager（构造链兼容回归）。"""
    from framework.parser import Parser

    from framework.search import Search

    mgr = _make_manager()
    s = Search.__new__(Search)
    # 用真实构造走一遍（默认无 cookie_manager 也应可构造）
    from framework.http import HttpClient

    s2 = Search(http=HttpClient(), parser=Parser())
    assert s2._cookie_manager is None
    s3 = Search(http=HttpClient(), parser=Parser(), cookie_manager=mgr)
    assert s3._cookie_manager is mgr