# -*- coding: utf-8 -*-
"""漫画渲染源降级兜底不得当完整结果入缓存 + 阅读器多图/懒加载回归。

背景（comicbox 只出 2 张图、其余永不出现）：
`Content._fetch_comic_pages_impl` 在 Playwright 渲染失败时静默降级到 HTML 提取
（framework/content.py 的 `except Exception` → HTML 兜底）。comicbox 这类
canvas 源的页面是 JS 应用，静态 HTML 里只有极少数 `.cropped` 占位元素，兜底
只能提取到 2 个 URL；而 `fetch_comic_pages` 事后把这个**残缺**结果按
`pages:{source_id}:{abs_url}` 写进缓存（7 天，Redis + SQLite 双层）。此后每次
打开该章都命中缓存直接返回那 2 个 URL，渲染再也不会被触发——章节被永久钉死
在 2 张图。普通漫画源走的是正常 HTML 路径，取到的是完整列表，所以不受影响。

覆盖：
- 降级兜底结果不入 pages: 缓存，且下次打开会重新尝试渲染（本次修复点）
- 渲染成功 → 照旧入缓存（缓存命中/零网络行为不变）
- 非渲染源 → 照旧入缓存（普通漫画源行为不变）
- 阅读器：N>2 张图全部被请求/渲染；滚动懒加载分批补全仍然工作
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402

from PySide6.QtCore import QEventLoop  # noqa: E402
from PySide6.QtWidgets import QLabel as _QLabel  # noqa: E402

from framework.config import SourceConfig  # noqa: E402
from framework.content import Chapter, Detail  # noqa: E402
from framework.parser import Parser  # noqa: E402

CHAPTER_URL = "https://c.example/ch/1.html"
PAGE_URLS = [f"https://c.example/pic/{i}.jpg" for i in range(1, 7)]

# Playwright 源（comicbox 结构）：render 走 canvas，静态 HTML 只有 2 个占位元素
RENDER_RAW = {
    "$schema_version": 2,
    "$id": "render-comic",
    "$type": "comic",
    "$name": "渲染漫画源",
    "$enabled": True,
    "$weight": 1.0,
    "transports": {"base_url": "https://c.example"},
    "endpoints": {
        "content": {
            "page": {
                "body": {
                    "render": "playwright",
                    "render_config": {
                        "wait_for": "canvas",
                        "extract_mode": "canvas",
                        "scroll_to_bottom": True,
                    },
                    "root_selector": {"css": ".comicpage .cropped"},
                    "fields": {"url": {"css": "div.cropped", "attr": "data-src"}},
                }
            }
        }
    },
}

# 普通漫画源：HTML 里就是完整图片列表
PLAIN_RAW = {
    "$schema_version": 2,
    "$id": "plain-comic",
    "$type": "comic",
    "$name": "普通漫画源",
    "$enabled": True,
    "$weight": 1.0,
    "transports": {"base_url": "https://c.example"},
    "endpoints": {
        "content": {
            "page": {
                "list": {
                    "root_selector": {"css": "div.page"},
                    "fields": {"url": {"css": "img", "attr": "data-src"}},
                }
            }
        }
    },
}

# 降级兜底时静态 HTML 只有 2 张（JS 应用只放了首屏占位）
STATIC_HTML = (
    '<div class="comicpage">'
    '<div class="cropped" id="p1" data-src="https://c.example/static/1.jpg"></div>'
    '<div class="cropped" id="p2" data-src="https://c.example/static/2.jpg"></div>'
    "</div>"
)


class _FakeChecker:
    pass


class _FakeHttp:
    """计数式假 HttpClient：章节页返回静态 HTML。"""

    cache = None

    def __init__(self, html=STATIC_HTML):
        from framework.http import NetworkDefaults

        self.defaults = NetworkDefaults()
        self.html = html
        self.calls = {}

    def get_text(self, url, **kw):
        self.calls[url] = self.calls.get(url, 0) + 1
        return self.html

    def close(self):
        pass


def _make_content(http, store=None, repository=None):
    from framework.content import Content

    return Content(http, Parser(), _FakeChecker(), cache=store, repository=repository)


def _make_store():
    from framework.cache_service import RedisLikeStore

    return RedisLikeStore(quota=4 * 1024 * 1024, persist_path=None)


def _install_render(result=None, error=None, batches=()):
    """替换 Playwright 渲染入口，返回调用计数 dict。"""
    import framework.playwright_helper as ph

    state = {"calls": 0}

    def fake_render(url, on_batch=None, **kw):
        state["calls"] += 1
        if error is not None:
            raise error
        if on_batch:
            for n in batches:
                on_page = on_batch
                on_page(result[:n])
        return result

    ph.fetch_rendered_images_sync = fake_render
    return state


# --------------------------------------------------------------------- #
# 取图侧：降级兜底结果不得写成"完整章节"缓存
# --------------------------------------------------------------------- #
def test_failed_render_fallback_is_not_cached():
    """渲染失败 → HTML 兜底只提到 2 张：不得入缓存，下次须重新渲染。"""
    store = _make_store()
    http = _FakeHttp()
    c = _make_content(http, store=store)
    src = SourceConfig.from_dict(RENDER_RAW, "<mem>")
    state = _install_render(error=RuntimeError("render failed"))

    imgs = c.fetch_comic_pages(src, CHAPTER_URL)
    assert imgs == ["https://c.example/static/1.jpg", "https://c.example/static/2.jpg"]
    assert state["calls"] == 1
    key = f"pages:v3:{src.source_id}:{CHAPTER_URL}"
    assert store.get(key) is None, "降级兜底的残缺列表不得写入 pages: 缓存"

    # 再次打开同一章 → 必须重新尝试渲染，而不是复用那 2 张的残缺缓存
    c.fetch_comic_pages(src, CHAPTER_URL)
    assert state["calls"] == 2, "重新打开章节应重新尝试渲染"


def test_successful_render_is_still_cached():
    """渲染成功 → 照旧写 pages: 缓存，第二次零渲染命中。"""
    store = _make_store()
    http = _FakeHttp()
    c = _make_content(http, store=store)
    src = SourceConfig.from_dict(RENDER_RAW, "<mem>")
    state = _install_render(result=PAGE_URLS)

    imgs = c.fetch_comic_pages(src, CHAPTER_URL)
    assert imgs == PAGE_URLS
    assert store.get(f"pages:v3:{src.source_id}:{CHAPTER_URL}") == PAGE_URLS
    assert c.fetch_comic_pages(src, CHAPTER_URL) == PAGE_URLS
    assert state["calls"] == 1, "缓存命中不应再渲染"


def test_plain_html_source_still_cached():
    """非渲染源：完整列表照旧入缓存，第二次零网络。"""
    store = _make_store()
    http = _FakeHttp(
        html="".join(f'<div class="page"><img data-src="{u}"/></div>' for u in PAGE_URLS)
    )
    c = _make_content(http, store=store)
    src = SourceConfig.from_dict(PLAIN_RAW, "<mem>")

    imgs = c.fetch_comic_pages(src, CHAPTER_URL)
    assert imgs == PAGE_URLS
    assert store.get(f"pages:v3:{src.source_id}:{CHAPTER_URL}") == PAGE_URLS
    c.fetch_comic_pages(src, CHAPTER_URL)
    assert http.calls.get(CHAPTER_URL, 0) == 1


# --------------------------------------------------------------------- #
# 阅读器侧：N>2 张图全部请求/渲染，懒加载分批补全
# --------------------------------------------------------------------- #
class _Sig:
    def connect(self, *_a, **_k):
        pass


class _FakeLabel(_QLabel):
    def __init__(self, calls, url, referer="", parent=None, source=None):
        super().__init__(parent)
        self.calls = calls
        self.url = url
        self.referer = referer
        self.loaded = _Sig()
        self.setMinimumHeight(600)  # 与真实 _ComicImageLabel 一致，保证内容可滚动

    def load(self):
        self.calls.append(self.url)


def _make_view(detail, content):
    from gui.pages.reader.comic_view import ComicView

    view = ComicView(content)
    view.resize(820, 620)
    view.show()
    view._detail = detail
    view._chapters = detail.chapters
    return view


def _pump(app, ms=1500):
    for _ in range(int(ms / 10)):
        app.processEvents(QEventLoop.AllEvents, 10)


def test_reader_requests_and_renders_more_than_two_images(_qapp):
    """章节 6 张图 → 全部发出图片请求、全部创建 label（不是只 2 张）。"""
    from gui.components import cover_loader as cl
    from gui.pages.reader import comic_view as cv

    urls = [f"https://c.example/pic/{i}.jpg" for i in range(1, 7)]
    http = _FakeHttp()
    c = _make_content(http)
    src = SourceConfig.from_dict(RENDER_RAW, "<mem>")
    # 边滚边出前缀（comicbox 形态）+ 最终完整列表
    _install_render(result=urls, batches=(2, 4, 6))

    detail = Detail(
        source_id=src.source_id, content_type="comic",
        url="https://c.example/book/1", title="漫画",
        chapters=[Chapter("第1话", CHAPTER_URL)],
    )
    view = _make_view(detail, c)
    view._source = src

    requests = []

    def counting_load(self, url, callback, **kw):
        requests.append(url)
        callback(None)

    with patch.object(cl._CoverLoader, "load", counting_load), \
            patch.object(cv, "_ComicImageLabel") as klazz:
        klazz.side_effect = lambda *a, **k: _FakeLabel(requests, *a, **k)
        view.load(src, detail)
        _pump(_qapp)

    assert view._images == urls, f"取图应完整，实际 {len(view._images)} 张"
    assert view._rendered_count == 6, "6 张图应全部渲染"
    assert len(requests) == 6, f"应发出 6 次图片请求，实际 {len(requests)}"
    assert len(requests) > 2


def test_reader_lazy_batch_fills_remaining_on_scroll(_qapp):
    """首屏只渲染一批，滚动到底由懒加载补全剩余图片。"""
    from gui.pages.reader import comic_view as cv
    from gui.pages.reader.comic_view import LAZY_BATCH

    urls = [f"https://c.example/pic/{i}.jpg" for i in range(1, 2 * LAZY_BATCH + 5)]
    detail = Detail(
        source_id="render-comic", content_type="comic",
        url="https://c.example/book/1", title="漫画",
        chapters=[Chapter("第1话", CHAPTER_URL)],
    )
    view = _make_view(detail, object())
    view._source = SimpleNamespace(source_id="render-comic")
    view._current_idx = 0
    view._images = urls
    view._mode = "gallery"
    view._rendered_count = 0
    view._rendered_header = True
    calls = []

    with patch.object(cv, "_ComicImageLabel") as klazz:
        klazz.side_effect = lambda *a, **k: _FakeLabel(calls, *a, **k)
        view._render_incremental()
        assert view._rendered_count == LAZY_BATCH, "单次增量只渲染一批"

        _pump(_qapp, 300)  # 让布局收敛，滚动条范围生效
        vbar = view.scroll.verticalScrollBar()
        assert vbar.maximum() > 0, "样本应可滚动"
        view._on_scroll_lazy(vbar.maximum())

    assert view._rendered_count == len(urls), "滚动到底应补全剩余图片"
    assert len(calls) == len(urls)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
