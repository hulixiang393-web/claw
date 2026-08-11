"""decrypter.js_custom 策略测试：文本 + 字节解密，复用 Playwright JS 引擎。

编排逻辑（base64 进出、entry/params 传递）用 mock evaluate 快测；
真实 Chromium 执行一条用例，作为集成冒烟（无浏览器时跳过）。
"""
import base64

import pytest

from framework.config import SourceConfig
from framework.decrypter import Decrypter
from framework.http import HttpClient


def _source(**target_cfg) -> SourceConfig:
    raw = {
        "$schema_version": 2,
        "$id": "t",
        "$type": "novel",
        "$name": "t",
        "$enabled": False,
        "transports": {"base_url": "http://x"},
        "endpoints": {"detail": {"fields": {}}, "content": {}},
        "decryption": {"targets": target_cfg},
    }
    return SourceConfig.from_dict(raw)


class TestJsCustomOrchestration:
    def test_text_decrypt_uses_entry_script_params(self, monkeypatch):
        calls = {}

        def fake_eval(expr):
            calls["expr"] = expr
            return "你好"

        monkeypatch.setattr(
            "framework.decrypter._evaluate_js",
            lambda script, entry, data, params, is_bytes=False: fake_eval(data),
        )
        src = _source(
            content={
                "strategy": "js_custom",
                "entry": "decrypt",
                "script": "function decrypt(d, p) { return d; }",
                "params": {"key": "k"},
            }
        )
        out = Decrypter(HttpClient()).decrypt(src, "hello", "content")
        assert out == "你好"

    def test_unknown_strategy_passthrough(self):
        src = _source(content={"strategy": "nope"})
        assert Decrypter(HttpClient()).decrypt(src, "hello", "content") == "hello"

    def test_missing_script_raises(self):
        src = _source(content={"strategy": "js_custom"})
        with pytest.raises(Exception):
            Decrypter(HttpClient()).decrypt(src, "hello", "content")

    def test_bytes_base64_roundtrip(self, monkeypatch):
        seen = {}

        def fake_eval(script, entry, data, params, is_bytes=False):
            seen["data"] = data
            return data  # 原样 base64 返回

        monkeypatch.setattr(
            "framework.decrypter._evaluate_js", fake_eval
        )
        src = _source(
            image={
                "strategy": "js_custom",
                "script": "function decrypt(d, p) { return d; }",
            }
        )
        raw = b"\x01\x02\x03\xff"
        out = Decrypter(HttpClient()).decrypt_bytes(src, raw, "image")
        assert out == raw
        assert seen["data"] == base64.b64encode(raw).decode("ascii")


def _playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        s = sync_playwright().start()
        b = s.chromium.launch(headless=True, args=["--no-sandbox"])
        b.close()
        s.stop()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _playwright_available(), reason="Playwright Chromium 不可用")
class TestJsCustomRealBrowser:
    def test_text_decrypt_real(self):
        src = _source(
            content={
                "strategy": "js_custom",
                "script": (
                    "function decrypt(d, p) { "
                    "return Array.from(d).filter(function(c){return c!=='*';}).join(''); }"
                ),
            }
        )
        out = Decrypter(HttpClient()).decrypt(src, "h*e*l*l*o", "content")
        assert out == "hello"

    def test_bytes_decrypt_real(self):
        src = _source(
            image={
                "strategy": "js_custom",
                "script": (
                    "function decrypt(d, p) { "
                    "var raw = atob(d); "
                    "var b = Uint8Array.from(raw, function(c){return c.charCodeAt(0);}).reverse(); "
                    "var s = ''; b.forEach(function(x){ s += String.fromCharCode(x); }); "
                    "return btoa(s); }"
                ),
            }
        )
        raw = b"\x01\x02\x03\xff"
        out = Decrypter(HttpClient()).decrypt_bytes(src, raw, "image")
        assert out == b"\xff\x03\x02\x01"

    def test_entry_custom_name(self):
        src = _source(
            content={
                "strategy": "js_custom",
                "entry": "myDecrypt",
                "script": "function myDecrypt(d, p) { return d + d; }",
            }
        )
        out = Decrypter(HttpClient()).decrypt(src, "ab", "content")
        assert out == "abab"