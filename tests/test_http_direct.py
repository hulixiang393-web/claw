# -*- coding: utf-8 -*-
"""源级「强制直连」测试（test_http_direct.py）。

背景：本机加速器/VPN 开着时系统代理（注册表）存活，requests 自动系统代理让
国内直连源（dm5 等）每个请求都经代理转发，实测放大 5~50 倍（dm5 直连 0.1s /
走代理 1.2s+）。transports.direct=true 的源应屏蔽「全局 network.proxy 与自动
系统代理」强制直连（显式打开的 proxy 仍优先，尊重更具体意图）。

覆盖：_effective_proxy 三态、get_text/get_bytes 传 direct 时 session 请求
proxies=None、content/discovery/search 源级 direct 透传、CoverLoader 图片
下载 direct 透传。全部 mock，不联网。
"""
import os
import sys
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from framework.http import HttpClient, NetworkDefaults


def _make(defaults_proxy=None):
    h = HttpClient(defaults=NetworkDefaults(proxy=defaults_proxy))
    h._system_proxy_enabled = True
    return h


class _Src:
    """最小源：transports 可指定 direct / proxy。"""

    def __init__(self, direct=False, proxy=None):
        self._direct = direct
        self._proxy = proxy
        self.base_url = "https://t.example"

    def transports(self) -> dict:
        t = {"base_url": self.base_url}
        if self._proxy:
            t["proxy"] = self._proxy
        if self._direct:
            t["direct"] = True
        return t

    def request_headers(self) -> dict:
        return {"User-Agent": "Mozilla/5.0"}

    def proxy_pool(self):
        return None


# ---------------------------------------------------------------------- #
# _effective_proxy 三态
# ---------------------------------------------------------------------- #
def test_direct_blocks_global_and_system_proxy():
    with patch("framework.http._live_system_proxy", return_value="http://127.0.0.1:65532"):
        h = _make(defaults_proxy="http://global:9")
        assert h._effective_proxy(None, force_direct=True) is None
        assert h._effective_proxy(None, force_direct=False) == "http://global:9"


def test_direct_without_explicit_goes_none():
    with patch("framework.http._live_system_proxy", return_value="http://127.0.0.1:65532"):
        h = _make()
        assert h._effective_proxy(None, force_direct=True) is None


def test_direct_explicit_proxy_still_priority():
    with patch("framework.http._live_system_proxy", return_value="http://127.0.0.1:65532"):
        h = _make()
        assert h._effective_proxy("http://explicit:9", force_direct=True) == "http://explicit:9"


def test_non_direct_uses_system_when_alive():
    with patch("framework.http._live_system_proxy", return_value="http://127.0.0.1:65532"):
        h = _make()
        assert h._effective_proxy(None, force_direct=False) == "http://127.0.0.1:65532"


# ---------------------------------------------------------------------- #
# get_text / get_bytes 直连时 session 请求不带 proxies
# ---------------------------------------------------------------------- #
class _Resp:
    status_code = 200
    headers = {}
    content = b"img"

    def raise_for_status(self):
        pass


class _TextResp:
    status_code = 200
    headers = {}
    text = "<html>ok</html>"

    def raise_for_status(self):
        pass


def _session_get(http, rsp):
    m = __import__("unittest.mock", fromlist=["Mock"]).Mock(return_value=rsp)
    http._session.get = m
    return m


def test_get_bytes_direct_sends_no_proxy():
    with patch("framework.http._live_system_proxy", return_value="http://127.0.0.1:65532"):
        h = _make()
        m = _session_get(h, _Resp())
        h.get_bytes("https://x/y.jpg", retries=0, direct=True)
        assert m.call_args.kwargs["proxies"] is None


def test_get_bytes_direct_ignores_global_proxy():
    with patch("framework.http._live_system_proxy", return_value="http://127.0.0.1:65532"):
        h = _make(defaults_proxy="http://global:9")
        m = _session_get(h, _Resp())
        h.get_bytes("https://x/y.jpg", retries=0, direct=True)
        assert m.call_args.kwargs["proxies"] is None


def test_get_bytes_non_direct_uses_system_proxy():
    with patch("framework.http._live_system_proxy", return_value="http://127.0.0.1:65532"):
        h = _make()
        m = _session_get(h, _Resp())
        h.get_bytes("https://x/y.jpg", retries=0)
        assert m.call_args.kwargs["proxies"] == {
            "http": "http://127.0.0.1:65532",
            "https": "http://127.0.0.1:65532",
        }


def test_get_text_direct_sends_no_proxy():
    with patch("framework.http._live_system_proxy", return_value="http://127.0.0.1:65532"):
        h = _make()
        m = _session_get(h, _TextResp())
        h.get_text("https://x/", retries=0, direct=True)
        assert m.call_args.kwargs["proxies"] is None


# ---------------------------------------------------------------------- #
# content / discovery / search 源级 direct 透传
# ---------------------------------------------------------------------- #
class _Recorder:
    """记录 get_text 参数并返回固定 HTML 的 HttpClient 替身。"""

    def __init__(self):
        self.calls = []
        self.defaults = NetworkDefaults()

    def get_text(self, url, **kw):
        self.calls.append(kw)
        return "<html>ok</html>"

    def close(self):
        pass


def test_content_get_passes_direct():
    from framework.content import Content

    rec = _Recorder()
    checker = type("C", (), {"check": lambda *a, **k: None})()
    c = Content(rec, object(), checker)
    c._get(_Src(direct=True), "/x.html")
    c._get(_Src(direct=False), "/x.html")
    assert [call.get("direct") for call in rec.calls] == [True, False]


def test_discovery_get_passes_direct():
    from framework.discovery import Discovery

    rec = _Recorder()
    d = Discovery(rec, object(), type("C", (), {"check": lambda *a, **k: None})())
    d._get(_Src(direct=True, proxy="http://p:9"), "/x.html")
    d._get(_Src(direct=False), "/x.html")
    assert [call.get("direct") for call in rec.calls] == [True, False]


def test_search_http_get_passes_direct():
    from framework.search import Search

    rec = _Recorder()
    s = Search.__new__(Search)
    s._http = rec
    s._http_get(_Src(direct=True), "https://t.example/search?q=1")
    s._http_get(_Src(direct=False), "https://t.example/search?q=2")
    assert [call.get("direct") for call in rec.calls] == [True, False]


# ---------------------------------------------------------------------- #
# CoverLoader 图片下载 direct 透传
# ---------------------------------------------------------------------- #
def _wait_until(cond, timeout=4000):
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()

    def _poll():
        if cond():
            loop.quit()
        else:
            QTimer.singleShot(25, _poll)

    QTimer.singleShot(25, _poll)
    QTimer.singleShot(timeout, loop.quit)
    loop.exec()


def _png_bytes():
    from PySide6.QtCore import QBuffer
    from PySide6.QtGui import QImage

    img = QImage(8, 8, QImage.Format_RGB32)
    img.fill(0xFF3366AA)
    buf = QBuffer()
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


class _FakeHttp:
    def __init__(self, data=b""):
        self.data = data
        self.calls = []

    def get_bytes(self, url, headers=None, proxy=None, timeout=None,
                  retries=None, proxy_pool=None, direct=False):
        self.calls.append(dict(url=url, proxy=proxy, direct=direct))
        return self.data


def _from_dict(raw):
    from framework.config import SourceConfig

    return SourceConfig.from_dict(raw, "<mem>")


@pytest.mark.usefixtures("_qapp")
def test_cover_loader_direct_from_source():
    """源配置 direct=true → 正文图/封面 get_bytes(direct=True, proxy=None)。"""
    from gui.components import cover_loader as cl

    loader = cl._CoverLoader()
    http = _FakeHttp(_png_bytes())
    loader.use_http(http)
    src = _from_dict({
        "$id": "d", "$type": "comic", "$name": "D", "$schema_version": 2,
        "transports": {"base_url": "https://d.example", "direct": True},
    })
    got = {}
    loader.load(
        "http://img.cdn/1.jpg",
        lambda pm: got.setdefault("pm", pm),
        cache=False,
        source=src,
    )
    _wait_until(lambda: "pm" in got)
    assert got["pm"] is not None and not got["pm"].isNull()
    assert http.calls, "应走 HttpClient.get_bytes"
    call = http.calls[0]
    assert call["direct"] is True
    assert call["proxy"] is None


@pytest.mark.usefixtures("_qapp")
def test_cover_loader_no_source_keeps_proxy():
    """无 direct 配置/无 source → 沿用既有代理策略（不强制直连）。"""
    from gui.components import cover_loader as cl

    loader = cl._CoverLoader()
    http = _FakeHttp(_png_bytes())
    loader.use_http(http)
    got = {}
    loader.load(
        "http://img.cdn/2.jpg",
        lambda pm: got.setdefault("pm", pm),
        cache=False,
    )
    _wait_until(lambda: "pm" in got)
    assert http.calls
    assert http.calls[0]["direct"] is False