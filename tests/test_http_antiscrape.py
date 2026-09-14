"""激进档反爬增强测试（test_http_antiscrape.py）。

覆盖（mock/桩，不联网）：
- 默认路径（impersonate=None）：仍走 requests 会话，行为与原来一致
- impersonate="chrome"：同一请求改走 curl_cffi 会话，且以 chrome 档位创建
- curl_cffi 不可用或初始化失败：优雅降级回 requests 会话
- UA 轮换：未显式传 headers 时按请求次数轮换；显式 UA 不被覆盖

运行：python -m pytest tests/test_http_antiscrape.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import framework.http as http_mod  # noqa: E402
from framework.http import HttpClient, NetworkDefaults  # noqa: E402


class FakeResponse:
    """最小可用的类 requests/curl_cffi 响应桩。"""

    def __init__(self, status_code=200, text="", content=b"",
                 content_type="text/html; charset=utf-8"):
        self.status_code = status_code
        self._text = text
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.encoding = None
        self.apparent_encoding = "utf-8"

    @property
    def text(self):
        return self._text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise http_mod.RequestError(f"HTTP {self.status_code}")


class FakeSession:
    """记录请求参数并返回 FakeResponse 的桩会话。"""

    def __init__(self, resp=None):
        self.calls = []
        self.resp = resp or FakeResponse()

    def get(self, url, headers=None, proxies=None, timeout=None, **kw):
        self.calls.append({
            "url": url, "headers": headers, "proxies": proxies,
            "timeout": timeout, **kw,
        })
        return self.resp


def _http(defaults=None):
    return HttpClient(sleeper=lambda s: None, defaults=defaults)


def test_default_path_uses_requests():
    """impersonate 未开启：即使存在伪装会话也不使用，仍走 requests 会话。"""
    client = _http()
    fake = FakeSession()
    client._session = fake
    client._impersonate_session = FakeSession()
    text = client._get_once_raw("http://x/", {"User-Agent": "u"}, None, 5)
    assert text == ""
    assert len(fake.calls) == 1
    assert client._impersonate_session.calls == []
    assert client.defaults.impersonate is None


def test_impersonate_text_uses_cffi_session(monkeypatch):
    """impersonate="chrome"：请求改走 curl_cffi 会话，并以 chrome 档位创建。"""
    monkeypatch.setattr(http_mod, "_CFFI_AVAILABLE", True)
    created = {}
    fake = FakeSession(resp=FakeResponse(text="<html>OK</html>"))

    def _factory(impersonate=None):
        created["impersonate"] = impersonate
        return fake

    monkeypatch.setattr(http_mod.crequests, "Session", _factory)
    client = _http(NetworkDefaults(impersonate="chrome"))
    text = client._get_once_raw("http://x/", None, None, 5)
    assert created.get("impersonate") == "chrome"
    assert fake.calls[0]["url"] == "http://x/"
    assert fake.calls[0]["headers"] is None
    assert client._impersonate_session is fake
    assert text == "<html>OK</html>"


def test_impersonate_get_bytes_uses_cffi_session(monkeypatch):
    """get_bytes 在 impersonate 开启时也走 curl_cffi 会话并返回字节。"""
    monkeypatch.setattr(http_mod, "_CFFI_AVAILABLE", True)
    fake = FakeSession(resp=FakeResponse(content=b"\x89PNG"))
    monkeypatch.setattr(http_mod.crequests, "Session", lambda impersonate=None: fake)
    client = _http(NetworkDefaults(impersonate="chrome"))
    data = client.get_bytes("http://x/img.png", retries=0)
    assert data == b"\x89PNG"
    assert len(fake.calls) == 1


def test_cffi_unavailable_degrades_to_requests(monkeypatch):
    """curl_cffi 未安装：impersonate 开启时优雅降级回 requests 会话。"""
    monkeypatch.setattr(http_mod, "_CFFI_AVAILABLE", False)
    client = _http(NetworkDefaults(impersonate="chrome"))
    fake = FakeSession()
    client._session = fake
    text = client._get_once_raw("http://x/", None, None, 5)
    assert text == ""
    assert len(fake.calls) == 1
    assert client._impersonate_session is None


def test_cffi_init_failure_degrades_to_requests(monkeypatch):
    """curl_cffi 会话初始化失败：降级回 requests，不抛异常。"""
    monkeypatch.setattr(http_mod, "_CFFI_AVAILABLE", True)

    def _boom(impersonate=None):
        raise RuntimeError("cffi init fail")

    monkeypatch.setattr(http_mod.crequests, "Session", _boom)
    client = _http(NetworkDefaults(impersonate="chrome"))
    fake = FakeSession()
    client._session = fake
    text = client._get_once_raw("http://x/", None, None, 5)
    assert text == ""
    assert len(fake.calls) == 1
    assert client._impersonate_session is None


def test_impersonate_proxy_passed(monkeypatch):
    """impersonate 开启时代理经 proxies 参数透传，语义与 requests 一致。"""
    monkeypatch.setattr(http_mod, "_CFFI_AVAILABLE", True)
    fake = FakeSession()
    monkeypatch.setattr(http_mod.crequests, "Session", lambda impersonate=None: fake)
    client = _http(NetworkDefaults(impersonate="chrome"))
    client._get_once_raw("http://x/", None, "http://127.0.0.1:8888", 5)
    assert fake.calls[0]["proxies"] == {
        "http": "http://127.0.0.1:8888", "https": "http://127.0.0.1:8888",
    }


def test_impersonate_antiscrape_status_raises(monkeypatch):
    """403 在 impersonate 后端同样识别为反爬并抛 AntiScrapeError。"""
    monkeypatch.setattr(http_mod, "_CFFI_AVAILABLE", True)
    fake = FakeSession(resp=FakeResponse(status_code=403))
    monkeypatch.setattr(http_mod.crequests, "Session", lambda impersonate=None: fake)
    client = _http(NetworkDefaults(impersonate="chrome"))
    with pytest.raises(http_mod.AntiScrapeError):
        client._get_once_raw("http://x/", None, None, 5)


def test_impersonate_explicit_encoding_sets_encoding(monkeypatch):
    """impersonate 后端显式 encoding 语义与 requests 一致（写入 resp.encoding）。"""
    monkeypatch.setattr(http_mod, "_CFFI_AVAILABLE", True)
    resp = FakeResponse(content_type="text/html", text="OK中文")
    fake = FakeSession(resp=resp)
    monkeypatch.setattr(http_mod.crequests, "Session", lambda impersonate=None: fake)
    client = _http(NetworkDefaults(impersonate="chrome"))
    text = client._get_once_raw("http://x/", None, None, 5, encoding="utf-8")
    assert resp.encoding == "utf-8"
    assert text == "OK中文"


def test_ua_rotation_no_explicit_headers():
    """配置 user_agents 且未显式传 headers：按请求次数轮换 UA。"""
    client = _http(NetworkDefaults(user_agents=["UA-A", "UA-B"]))
    assert client._headers_with_ua(None) == {"User-Agent": "UA-A"}
    assert client._headers_with_ua(None) == {"User-Agent": "UA-B"}
    assert client._headers_with_ua(None) == {"User-Agent": "UA-A"}


def test_ua_fallback_default_when_no_list():
    """未配置 user_agents：回退默认 UA。"""
    client = _http(NetworkDefaults(user_agent="DEF"))
    assert client._headers_with_ua(None) == {"User-Agent": "DEF"}


def test_explicit_ua_not_overridden():
    """调用方显式传 headers（含 UA）：轮换不覆盖。"""
    client = _http(NetworkDefaults(user_agents=["UA-A"], user_agent="DEF"))
    out = client._headers_with_ua({"User-Agent": "CUSTOM", "X": "1"})
    assert out == {"User-Agent": "CUSTOM", "X": "1"}


def test_default_no_charset_falls_back_utf8():
    """默认路径：Content-Type 无 charset 时用 apparent_encoding 兜底。"""
    client = _http()
    resp = FakeResponse(content_type="text/html")
    fake = FakeSession(resp=resp)
    client._session = fake
    client._get_once_raw("http://x/", None, None, 5)
    assert resp.encoding == "utf-8"