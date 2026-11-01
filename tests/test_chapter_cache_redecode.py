# -*- coding: utf-8 -*-
"""正文缓存自愈：解码上线前缓存的章节仍带假字时，读缓存也要就地重解。

提莫書屋的字体混淆是后来才接上的，而正文缓存（shelf_cache / 书架缓存仓库）
存的是**解密后**文本、TTL 7 天。旧缓存命中时若不重解，用户重开同一章仍然
是「张起灵녈头」——表现就是「解密没成功」。
"""
from types import SimpleNamespace

import pytest

from framework.content.base import ContentBase
from framework.content.chapter import ChapterMixin


class _Cache:
    """最小缓存替身：get/set 语义与 RedisLikeStore 一致。"""

    def __init__(self, data=None):
        self.data = dict(data or {})
        self.sets = []

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ttl=None):
        self.data[key] = value
        self.sets.append((key, value, ttl))


class _Decrypter:
    """只做假字替换，避免测试联网下字体。"""

    def __init__(self):
        self.calls = []

    def decrypt(self, source, content, target="content"):
        self.calls.append(target)
        return (content or "").replace("녈", "打").replace("극", "入")


class _Obj(ChapterMixin, ContentBase):
    """按 Content 的 MRO 组合，_cache_set/_cache_get 来自 ContentBase。"""


def _mixin(cache, decrypter):
    obj = _Obj.__new__(_Obj)
    obj._cache = cache
    obj._repository = None
    obj._decrypter = decrypter
    obj._abs_url = lambda source, url: url
    return obj


def _source(steps=None):
    targets = {
        "content": ({"steps": steps} if steps is not None
                    else {"strategy": "zh_convert", "to": "zh-cn"}),
    }
    return SimpleNamespace(
        source_id="demo",
        raw={"decryption": {"targets": targets}},
        base_url="https://example.test",
        request_headers=lambda: {},
    )


def _obf_steps():
    return [
        {"strategy": "obfuscated_font", "codepoint_ranges": [[0xAC00, 0xD7A3]]},
        {"strategy": "zh_convert", "to": "zh-cn"},
    ]


def test_obfuscated_font_steps_detects_declaration():
    found = ChapterMixin._obfuscated_font_steps(_source(_obf_steps()))
    assert found and found[0][0] == "content"
    assert found[0][1]["strategy"] == "obfuscated_font"
    assert ChapterMixin._obfuscated_font_steps(_source()) == []


def test_cached_text_with_fake_glyphs_is_redecoded():
    source = _source(_obf_steps())
    url = "https://example.test/1.html"
    cache = _Cache()
    decrypter = _Decrypter()
    obj = _mixin(cache, decrypter)
    key = obj._chapter_cache_key(source, url)
    cache.data[key] = "张起灵녈头，五人依次进극墓室。"
    fixed = obj._redecode_if_obfuscated(source, url, cache.data[key])
    assert fixed == "张起灵打头，五人依次进入墓室。"
    assert decrypter.calls == ["content"]
    assert cache.sets and cache.sets[0][0] == key, "解完要回写缓存，避免每次读都重解"
    assert "녈" not in cache.sets[0][1]


def test_clean_cached_text_is_untouched():
    source = _source(_obf_steps())
    decrypter = _Decrypter()
    obj = _mixin(_Cache(), decrypter)
    text = "张起灵打头，五人依次进入墓室。"
    out = obj._redecode_if_obfuscated(source, "https://example.test/1.html", text)
    assert out == text
    assert decrypter.calls == [], "没有假字就不该走解密"


def test_source_without_font_step_is_untouched():
    source = _source()  # 只声明 zh_convert
    decrypter = _Decrypter()
    obj = _mixin(_Cache(), decrypter)
    text = "任意文本 녈"
    assert obj._redecode_if_obfuscated(source, "https://example.test/1.html", text) == text
    assert decrypter.calls == []


def test_empty_text_and_no_decrypter_are_safe():
    source = _source(_obf_steps())
    assert _mixin(_Cache(), _Decrypter())._redecode_if_obfuscated(
        source, "https://example.test/1.html", "") == ""
    obj = _mixin(_Cache(), None)
    assert obj._redecode_if_obfuscated(
        source, "https://example.test/1.html", "녈") == "녈"


def test_range_bounds_are_respected():
    source = _source([{"strategy": "obfuscated_font",
                      "codepoint_ranges": [[0xE000, 0xF8FF]]}])
    decrypter = _Decrypter()
    obj = _mixin(_Cache(), decrypter)
    # 谚文不在声明的私用区区间内 → 不动
    text = "녈"
    assert obj._redecode_if_obfuscated(source, "https://example.test/1.html", text) == text
    assert decrypter.calls == []
