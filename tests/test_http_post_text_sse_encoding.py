# -*- coding: utf-8 -*-
"""post_text SSE 编码解码测试（test_http_post_text_sse_encoding.py）。

背景：ikanpp 搜索走 POST + text/event-stream（SSE）。该响应 Content-Type 无
charset，requests 对 text/* 默认按 iso-8859-1 解码 → UTF-8 中文标题乱码（实测
resp.encoding=ISO-8859-1、apparent_encoding=utf-8）。修复：post_text 支持显式
encoding，并用 _response_text 统一解码语义（显式> headers charset > apparent）。

本测试用模拟 requests 的 fake 响应（text 属性按 resp.encoding 解码 content）
验证：显式 encoding 生效；无 charset 时 apparent_encoding（utf-8）兜底不乱码。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.http import HttpClient, NetworkDefaults  # noqa: E402


class _RawResponse:
    """模拟 requests.Response 的 text 解码语义：按 resp.encoding 解码 content。

    encoding 未显式设置且内容匹配 text/* 时，模拟 requests 默认 iso-8859-1。
    """

    def __init__(self, content, content_type="text/event-stream"):
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.status_code = 200
        self._encoding = None
        self.apparent_encoding = "utf-8"

    @property
    def encoding(self):
        if self._encoding is not None:
            return self._encoding
        ct = self.headers.get("Content-Type", "")
        if "charset=" in ct:
            return ct.split("charset=")[-1].strip().strip("\"'")
        if ct.split(";")[0].strip() in (
            "text/event-stream", "text/plain", "text/html",
        ):
            return "iso-8859-1"  # requests 对无 charset 的 text/* 默认值
        return None

    @encoding.setter
    def encoding(self, value):
        self._encoding = value

    @property
    def text(self):
        return self.content.decode(self.encoding or "utf-8", errors="replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise IOError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.resp


def _client(resp):
    c = HttpClient(defaults=NetworkDefaults(retries=0))
    c._system_proxy_enabled = True
    c._session = _FakeSession(resp)
    return c


def test_post_text_explicit_encoding_decodes_utf8():
    """SSE 正文为 UTF-8 中文，显式 encoding='utf-8' → 不回退 iso-8859-1。"""
    body = 'data: {"type":"videos","videos":[{"vod_name":"战狼2"}]}\n\n'
    client = _client(_RawResponse(body.encode("utf-8")))
    text = client.post_text("http://x/search", encoding="utf-8", retries=0)
    assert "战狼2" in text
    assert "锟斤拷" not in text


def test_post_text_no_charset_falls_back_apparent():
    """无 charset 的 text/event-stream：apparent_encoding(utf-8) 兜底不乱码。"""
    body = 'data: {"title":"流浪地球"}\n\n'
    client = _client(_RawResponse(body.encode("utf-8")))
    text = client.post_text("http://x/search", retries=0)
    assert "流浪地球" in text


def test_post_text_respects_headers_charset():
    """headers 带 charset 时按其解码（与 get_text 语义一致）。"""
    body = 'data: {"title":"名侦探柯南"}\n\n'
    resp = _RawResponse(body.encode("utf-8"), content_type="text/event-stream; charset=utf-8")
    client = _client(resp)
    text = client.post_text("http://x/search", retries=0)
    assert "名侦探柯南" in text


def test_post_text_urllib_fallback_uses_encoding(monkeypatch):
    """无 requests 会话（urllib 分支）时也按 encoding 解码而不是硬编码 utf-8。"""
    import framework.http as http_mod

    body = 'data: {"title":"康熙王朝"}\n\n'.encode("utf-8")

    class _FakeResp:
        def __init__(self):
            self._read = body

        def read(self):
            return self._read

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    opener_calls = []

    def _fake_opener(proxy):
        class _Opener:
            def open(self, req, timeout=None):
                opener_calls.append((req, timeout))
                return _FakeResp()

        return _Opener()

    monkeypatch.setattr(http_mod, "_urllib_opener", _fake_opener)
    client = HttpClient(defaults=NetworkDefaults(retries=0))
    client._system_proxy_enabled = True
    client._session = None
    text = client.post_text(
        "http://x/search", json_body={"q": "x"}, encoding="utf-8", retries=0
    )
    assert "康熙王朝" in text
    assert len(opener_calls) == 1


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))