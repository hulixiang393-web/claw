# -*- coding: utf-8 -*-
"""字体混淆自动解码：源没写解码配置也要能解（多个笔趣阁系站点同款）。

真实坑：只有 timotxt 配了 obfuscated_font，用户实际读的是别的源（18mh 等），
正文里同样有「张起灵녈头」，于是「重启也没用」。机制改为通用：正文出现
谚文/私用区假码点就按页面 @font-face 还原，不依赖逐源配置。
"""
from types import SimpleNamespace

import framework.obfuscated_font as of
from framework.content.base import ContentBase
from framework.content.chapter import ChapterMixin


class _Obj(ChapterMixin, ContentBase):
    pass


def _obj():
    obj = _Obj.__new__(_Obj)
    obj._cache = None
    obj._repository = None
    obj._decrypter = object()
    obj._abs_url = lambda source, url: url
    return obj


def _source(source_id="unknown"):
    return SimpleNamespace(source_id=source_id, raw={}, request_headers=lambda: {})


def test_auto_decode_skips_clean_text_without_touching_font(monkeypatch):
    calls = []
    monkeypatch.setattr(of, "decode", lambda *a, **k: calls.append(1) or ("", {}))
    source = _source()
    text = "张起灵打头，五人依次进入墓室。"
    assert _obj()._auto_decode_font(source, "https://x/1.html", text, "<html/>") == text
    assert calls == [], "干净正文不该触发字体下载"


def test_auto_decode_runs_for_undeclared_source(monkeypatch):
    seen = {}

    def fake_decode(text, cfg, headers=None):
        seen["cfg"] = cfg
        seen["headers"] = headers
        return text.replace("녈", "打").replace("극", "入"), {"녈": "打", "극": "入"}

    monkeypatch.setattr(of, "decode", fake_decode)
    source = _source("18mh-novel")
    text = "张起灵녈头，五人依次进극墓室。"
    out = _obj()._auto_decode_font(source, "https://x/1.html", text, "<html/>")
    assert out == "张起灵打头，五人依次进入墓室。"
    assert seen["cfg"]["html"] == "<html/>", "要带页面 HTML 供自动找 @font-face"
    assert seen["cfg"]["hint_key"] == "18mh-novel", "要记住该源用的字体，缓存文本才好复用"
    assert [list(span) for span in seen["cfg"]["codepoint_ranges"]] == [
        [0xAC00, 0xD7A3], [0xE000, 0xF8FF]
    ]


def test_auto_decode_handles_pua_codepoints(monkeypatch):
    monkeypatch.setattr(of, "decode", lambda text, cfg, headers=None: ("解密后", {}))
    text = "这里是\uE123\uE456"
    assert _obj()._auto_decode_font(_source(), "https://x/1.html", text, "<html/>") == "解密后"


def test_auto_decode_returns_text_when_decode_fails(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("字体服务器挂了")

    monkeypatch.setattr(of, "decode", boom)
    text = "张起灵녈头。"
    assert _obj()._auto_decode_font(_source(), "https://x/1.html", text, "<html/>") == text


def test_auto_decode_needs_html_or_known_fonts(monkeypatch):
    calls = []
    monkeypatch.setattr(of, "decode", lambda *a, **k: calls.append(1) or ("x", {}))
    obj, source = _obj(), _source("nosource")
    text = "张起灵녈头。"
    # 没有 html、也没有历史字体地址 → 不能白跑一次网络请求
    assert obj._auto_decode_font(source, "https://x/1.html", text, "") == text
    assert calls == []
    # 有历史字体地址（同一进程里抓成功过一次）→ 可以解缓存里的旧文本
    of._FONT_HINTS["nosource"] = ["https://x/f24092.ttf"]
    try:
        assert obj._auto_decode_font(source, "https://x/1.html", text, "") == "x"
        assert calls
    finally:
        of._FONT_HINTS.pop("nosource", None)


def test_cached_text_of_undeclared_source_is_auto_decoded(monkeypatch):
    monkeypatch.setattr(of, "decode",
                        lambda text, cfg, headers=None: (text.replace("녈", "打"), {"녈": "打"}))
    of._FONT_HINTS["unknown"] = ["https://x/f24092.ttf"]
    try:
        obj = _obj()
        out = obj._redecode_if_obfuscated(_source(), "https://x/1.html", "张起灵녈头。")
    finally:
        of._FONT_HINTS.pop("unknown", None)
    assert out == "张起灵打头。"
