# -*- coding: utf-8 -*-
"""搜索「需登录」引导测试（test_search_login_required.py）。

番茄等站搜索页未登录渲染恒为空（服务端空 body / 登录墙）。render_config
配置 login_required 后，搜索 0 结果 + 未保存 Cookie → 抛 SourceError 明确
引导（GUI 源状态 chip 会显示），避免用户误以为搜索坏了反复重试。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from framework.errors import SourceError
from framework.search import Search

RAW = {
    "$schema_version": 2,
    "$id": "login-src",
    "$type": "novel",
    "$name": "需登录源",
    "$enabled": True,
    "$weight": 1.0,
    "transports": {"base_url": "https://l.example"},
    "endpoints": {
        "search": {
            "base_url": "/search/{keyword}",
            "method": "GET",
            "render": "playwright",
            "item": {
                "root_selector": {"css": "a[href*='/page/']"},
                "render_config": {
                    "wait_for": "a[href*='/page/']",
                    "wait_until": "networkidle",
                    "timeout_ms": 30000,
                    "extra_delay_ms": 3000,
                    "login_required": True,
                },
            },
        }
    },
}


class _CM:
    def __init__(self, logged: bool):
        self._logged = logged

    def is_logged_in(self, source_id: str) -> bool:
        return self._logged

    def to_playwright_cookies(self, source_id: str) -> list:
        return []


def _rendered(
    monkeypatch, items=None, interact=False, home="https://l.example"
):
    from framework import playwright_helper

    def _fake_items(*a, **kw):
        return items or []

    def _fake_search(*a, **kw):
        return items or []

    if interact:
        monkeypatch.setattr(playwright_helper, "fetch_rendered_search_sync", _fake_search)
    else:
        monkeypatch.setattr(playwright_helper, "fetch_rendered_items_sync", _fake_items)


def _search(logged=None):
    s = Search.__new__(Search)
    s._cookie_manager = None if logged is None else _CM(logged)
    return s


def _src():
    from framework.config import SourceConfig

    return SourceConfig.from_dict(RAW, "<mem>")


# ------------------------------------------------------------------------- #
def test_login_required_not_logged_raises(monkeypatch):
    """login_required + 未登录 + 0 结果 → 抛 SourceError（含引导文案）。"""
    _rendered(monkeypatch, items=[])
    with pytest.raises(SourceError) as ei:
        _search(None)._search_html_rendered(
            _src(), "https://l.example/search/x", _src().get_search_config()["item"], "x"
        )
    assert "需登录" in str(ei.value)


def test_login_required_logged_in_no_raise(monkeypatch):
    """已保存 Cookie → 0 结果也正常返回（可能是真无匹配）。"""
    _rendered(monkeypatch, items=[])
    out = _search(True)._search_html_rendered(
        _src(), "https://l.example/search/x", _src().get_search_config()["item"], "x"
    )
    assert out == []


def test_login_required_empty_keyword_no_raise(monkeypatch):
    """关键词为空（非搜索场景调用）→ 不触发登录提示。"""
    _rendered(monkeypatch, items=[])
    out = _search(None)._search_html_rendered(
        _src(), "https://l.example/search/", _src().get_search_config()["item"], ""
    )
    assert out == []


def test_no_login_required_flag_behaves_as_before(monkeypatch):
    """未配置 login_required 的渲染源 → 0 结果正常返回，不误报。"""
    raw = {k: v for k, v in RAW.items()}
    import copy

    raw = copy.deepcopy(RAW)
    rc = raw["endpoints"]["search"]["item"]["render_config"]
    rc.pop("login_required")
    from framework.config import SourceConfig

    src = SourceConfig.from_dict(raw, "<mem>")
    _rendered(monkeypatch, items=[])
    out = _search(None)._search_html_rendered(
        src, "https://l.example/search/x", src.get_search_config()["item"], "x"
    )
    assert out == []


def test_login_required_with_results_no_raise(monkeypatch):
    """有结果 → 即便未登录也不报错（防止登录标记误伤能正常搜索的站）。"""
    _rendered(monkeypatch, items=[{"title": "剑来", "href": "https://l.example/page/1"}])
    out = _search(None)._search_html_rendered(
        _src(), "https://l.example/search/x", _src().get_search_config()["item"], "剑来"
    )
    assert out and out[0].title == "剑来"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))