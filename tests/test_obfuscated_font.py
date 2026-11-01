# -*- coding: utf-8 -*-
"""反爬子集字体解码（obfuscated_font）：cmap 解析、字体 URL、字形映射、解密链路。

真实样本：timotxt（提莫書屋）把正文里的「是/不/我/的/了」换成谚文码点下发，
再用 /fonts/f24092.ttf 这张 150 字子集字体画成正确字形。fixture 就是那只字体
（22KB），所以这些用例离线可跑、不依赖网络。
"""
import json
import os
from types import SimpleNamespace

import pytest

import framework.obfuscated_font as of
from framework.decrypter import Decrypter

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "timo_f24092.ttf")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CSS = """
@font-face{font-family:iconfont;src:url(//at.alicdn.com/t/c/icon_1.woff2) format("woff2")}
@font-face{font-family:'bqg';src:url(/fonts/f24092.woff2) format('woff2'),
  url(/fonts/f24092.ttf) format('truetype')}
"""


@pytest.fixture()
def font_raw():
    with open(FIXTURE, "rb") as handle:
        return handle.read()


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(of, "_cache_dir", lambda: str(tmp_path))
    of._MAP_CACHE.clear()
    yield
    of._MAP_CACHE.clear()


def test_codepoints_in_respects_ranges():
    text = "正常놆文字\uac00\uae00\ue000"
    codes = of.codepoints_in(text, [[0xAC00, 0xD7A3]])
    assert codes == {0xAC00, 0xAE00, 0xB186}
    assert of.codepoints_in(text, []) == set()
    # 两个区间都要覆盖
    both = of.codepoints_in(text, [[0xAC00, 0xD7A3], [0xE000, 0xF8FF]])
    assert 0xE000 in both


def test_font_urls_from_css_absolutizes_and_skips_icons():
    urls = of.font_urls_from_css(CSS, base_url="https://i1.timotxt.com/min/read.38.css")
    assert "https://i1.timotxt.com/fonts/f24092.woff2" in urls
    assert "https://i1.timotxt.com/fonts/f24092.ttf" in urls
    assert not any("icon" in url for url in urls), urls


def test_stylesheet_urls_from_html():
    html = ('<link rel="stylesheet" href="https://i1.timotxt.com/min/read.38.css">'
            '<link rel="icon" href="/favicon.ico">')
    assert of.stylesheet_urls_from_html(html) == [
        "https://i1.timotxt.com/min/read.38.css"
    ]


def test_cmap_codes_parses_subset_font(font_raw):
    codes = of.cmap_codes(font_raw)
    assert len(codes) == 150, "提莫字体是 150 字子集"
    assert 0xB186 in codes and 0xAE4A in codes
    assert 0x4E00 not in codes, "子集字体里不该有常规汉字码位"


@pytest.mark.skipif(not of._sys_font(), reason="需要系统字体做字形匹配")
def test_build_map_matches_known_glyphs(font_raw):
    """这 6 个码点的答案由真实正文上下文核对过（如「大家是不是」）。"""
    expect = {0xAED7: "上", 0xB196: "我", 0xB18A: "不",
              0xADF9: "入", 0xB213: "地", 0xB180: "布"}
    mapping = of.build_map(font_raw, list(expect), confidence=0.55)
    for code, char in expect.items():
        assert mapping.get(chr(code)) == char, (hex(code), mapping.get(chr(code)), char)


@pytest.mark.skipif(not of._sys_font(), reason="需要系统字体做字形匹配")
def test_decode_end_to_end_with_fixture(font_raw, tmp_path):
    text = "周圍全놆殘垣斷壁，他놅心裡很뀪往一樣平靜。"
    cfg = {"codepoint_ranges": [[0xAC00, 0xD7A3]], "confidence": 0.55,
           "font_urls": ["https://example.test/f24092.ttf"]}
    original = of.font_bytes
    of.font_bytes = lambda url, headers=None: font_raw
    try:
        out, mapping = of.decode(text, cfg)
    finally:
        of.font_bytes = original
    assert not [c for c in out if 0xAC00 <= ord(c) <= 0xD7A3], out
    assert "是" in out and "的" in out
    assert len(mapping) >= 140, "整只字体应一次解完（一一映射）"
    assert list(tmp_path.glob("map_*.json")), "映射应落盘缓存"


def test_decrypter_runs_steps_in_order():
    # 不依赖 load_source 的全局缓存：这里只关心 steps 的顺序语义
    source = SimpleNamespace(
        raw={"decryption": {"targets": {}}}, request_headers=lambda: {}
    )
    calls = []

    class _Recorder(Decrypter):
        def _base64_decode(self, content):
            calls.append(content)
            return content[:-1] if content else content

    decrypter = _Recorder(http=None)
    source.raw["decryption"]["targets"]["__test__"] = {
        "steps": [{"strategy": "base64_decode"}, {"strategy": "base64_decode"}]
    }
    try:
        out = decrypter.decrypt(source, "abcde", target="__test__")
    finally:
        source.raw["decryption"]["targets"].pop("__test__", None)
    assert calls == ["abcde", "abcd"], calls
    assert out == "abc"


def test_timotxt_source_declares_font_decoding():
    with open(os.path.join(ROOT, "sources", "timotxt.json"), encoding="utf-8") as handle:
        raw = json.load(handle)
    targets = raw["decryption"]["targets"]
    for name in ("content", "chapter"):
        steps = targets[name]["steps"]
        assert steps[0]["strategy"] == "obfuscated_font"
        assert steps[0]["codepoint_ranges"] == [[44032, 55203]]
        assert steps[0]["font_css"], "必须声明到字体所在 CSS"
        assert steps[-1]["strategy"] == "zh_convert"
