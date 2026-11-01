# -*- coding: utf-8 -*-
"""阅读器渲染兜底：正文进渲染前必须已解码（缓存/离线文本也一样）。

用户侧的真实坑：正文缓存、书架缓存仓库、离线预缓存里存的是**解密后**文本
（TTL 7 天），解码上线前写入的条目会原样带「张起灵녈头」这类假字，重开应用
也从缓存里读出来。渲染前统一过一遍 Content 的自愈逻辑即可兜住所有来源。
"""
from types import SimpleNamespace

from gui.pages.reader.novel_view import NovelView


class _Content:
    def __init__(self, mapping=None):
        self.calls = []
        self.mapping = mapping or {}

    def _redecode_if_obfuscated(self, source, url, text):
        self.calls.append((url, text))
        return self.mapping.get(text, text)


def _view(content):
    view = NovelView.__new__(NovelView)  # 不初始化 Qt，只测方法语义
    view._content = content
    view._source = SimpleNamespace(source_id="timotxt")
    return view


def test_ensure_decoded_rewrites_cached_text_and_chapter_cache():
    dirty = "张起灵녈头，五人依次进극墓室。"
    clean = "张起灵打头，五人依次进入墓室。"
    content = _Content({dirty: clean})
    chapter = SimpleNamespace(url="https://example.test/1.html", _cached_text=dirty)
    out = _view(content)._ensure_decoded(chapter, dirty)
    assert out == clean
    assert chapter._cached_text == clean, "渲染兜底后要同步章节缓存，行距/段距测量才用解码文本"
    assert content.calls == [(chapter.url, dirty)]


def test_ensure_decoded_passes_clean_text_through_once():
    content = _Content()
    chapter = SimpleNamespace(url="https://example.test/1.html")
    text = "张起灵打头，五人依次进入墓室。"
    assert _view(content)._ensure_decoded(chapter, text) == text
    assert content.calls == [(chapter.url, text)], "正常文本只做一次区间判断，不改写"


def test_ensure_decoded_handles_empty_and_broken_content():
    content = _Content()
    assert _view(content)._ensure_decoded(SimpleNamespace(url="u"), "") == ""
    assert content.calls == []

    broken = SimpleNamespace(_redecode_if_obfuscated=None)
    assert _view(broken)._ensure_decoded(SimpleNamespace(url="u"), "녈") == "녈"

    def boom(*_args, **_kwargs):
        raise RuntimeError("network down")

    assert _view(SimpleNamespace(_redecode_if_obfuscated=boom))._ensure_decoded(
        SimpleNamespace(url="u"), "녈") == "녈"
