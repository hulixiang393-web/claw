# -*- coding: utf-8 -*-
"""ACG 漫画源阅读加载性能回归测试（test_acgxmh_perf.py）。

覆盖本次性能优化的两条关键路径（离线、mock http，不触真实网络）：

1. HTML 并行翻页（acgxmh 分页阅读）：
   - 页数完整：并行 wave 抓取 + 429 限流退避重试补页，一页不缺；
   - 边抓边回调：每 wave 完成回调一次连续前缀（阅读器秒出首屏），
     而非等整话抓完才拿到 URL。
2. 阅读器预取顺序：读到 70% 触发的「下一话」预取插队到向前缓存之前，
   连看时下一话优先就绪。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.errors import RequestError  # noqa: E402
from framework.http import AntiScrapeError, NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402

BASE = "https://c.example"

COMIC_PARALLEL_RAW = {
    "$schema_version": 2,
    "$id": "acg-parallel",
    "$type": "comic",
    "$name": "并行分页漫画",
    "$enabled": True,
    "$weight": 1.0,
    "transports": {"base_url": BASE, "timeout": 5, "retries": 0, "interval_ms": 0},
    "endpoints": {
        "content": {
            "page": {
                "body": {
                    "root_selector": {"css": "p.manga-picture img"},
                    "fields": {"url": {"css": "img", "attr": "src"}},
                    "paginator": {
                        "next_link": {"selector": {"css": "a.next", "attr": "href"}},
                        "parallel": {
                            "url_template": "{base}-{page}.html",
                            "first_page": 2,
                            "window": 2,
                            "max_pages": 100,
                        },
                    },
                }
            }
        }
    },
}


class _FakeChecker:
    pass


# 并行翻页会为每页构造独立 HttpClient（self._http.__class__(sleeper, defaults)），
# 因此配置/计数放模块级共享状态，让副本实例也能看到同一份章节参数与调用记录。
_STATE = {"total": 5, "fail_once": set(), "failed": set(), "calls": []}


class _FakeHttp:
    """离线假 HttpClient：按分页 URL 生成一页一图 HTML + next 链接。

    - _STATE["total"] 指定章节总页数；最后一页 next 自环（并行停止条件）。
    - _STATE["fail_once"]：这些页首次请求抛 AntiScrapeError（模拟 429 限流），
      第二次起成功——验证重试补页不丢图。
    """

    cache = None
    _sleeper = staticmethod(lambda *_a: None)

    def __init__(self, total=None, fail_once_pages=None, sleeper=None, defaults=None):
        if total is not None:
            _STATE["total"] = total
            _STATE["calls"] = []  # 新测试：清空共享调用记录
        if fail_once_pages is not None:
            _STATE["fail_once"] = set(fail_once_pages)
            _STATE["failed"] = set()
        if sleeper is None:
            sleeper = lambda *_a: None
        self.defaults = defaults or NetworkDefaults(timeout=5, retries=0, interval_ms=0)
        self.calls = _STATE["calls"]
        self._sleeper = sleeper

    @staticmethod
    def _page_no(url: str) -> int:
        tail = url.split("/")[-1]
        if "-" not in tail:
            return 1
        return int(tail.rsplit("-", 1)[-1].split(".", 1)[0])

    def get_text(self, url, headers=None, timeout=None, retries=None,
                 interval_ms=None, encoding=None, proxy_pool=None, direct=False):
        p = self._page_no(url)
        self.calls.append(p)
        if p in _STATE["fail_once"] and p not in _STATE["failed"]:
            _STATE["failed"].add(p)
            raise AntiScrapeError(f"反爬响应 HTTP 429 {url}")
        if p > _STATE["total"]:
            raise RequestError(f"HTTP 404 {url}")
        base = url.rsplit(".", 1)[0]
        nxt = f"{base}.html" if p >= _STATE["total"] else f"{base.rsplit('-', 1)[0]}-{p + 1}.html"
        return (
            f'<p class="manga-picture"><img src="{BASE}/pic/p{p}.webp"/></p>'
            f'<a class="next" href="{nxt}">下一页</a>'
        )

    def close(self):
        pass


def _content(http):
    return Content(http, Parser(), _FakeChecker())


def _source(window=2):
    raw = COMIC_PARALLEL_RAW
    raw["endpoints"]["content"]["page"]["body"]["paginator"]["parallel"]["window"] = window
    return SourceConfig.from_dict(raw, "<mem>")


# ---------------------------------------------------------------------- #
def test_parallel_pagination_complete_and_incremental_callback():
    """并行翻页页数完整 + 每 wave 回调连续前缀（边抓边显示）。"""
    http = _FakeHttp(total=5)
    c = _content(http)
    src = _source(window=2)
    events = []

    imgs = c.fetch_comic_pages(src, f"{BASE}/h/100.html", on_page=events.append)

    assert imgs == [f"{BASE}/pic/p{i}.webp" for i in range(1, 6)]
    # 首图就绪即回调 + 每 wave 回调 → 至少 2 次，且首次早于最终总数
    assert len(events) >= 2, f"应有分批回调，实际 {events}"
    assert len(events[0]) < len(imgs), "首批回调应早于整话完成"
    assert len(events[-1]) == len(imgs)
    # 回调前缀单调递增（连续前缀语义）
    assert all(len(a) <= len(b) for a, b in zip(events, events[1:]))


def test_parallel_pagination_retries_429_without_missing_page():
    """某页首次 429 → 退避重试补抓，最终页数完整（不丢页）。"""
    http = _FakeHttp(total=5, fail_once_pages=(3,))
    c = _content(http)
    src = _source(window=2)

    imgs = c.fetch_comic_pages(src, f"{BASE}/h/100.html")

    assert len(imgs) == 5, f"429 补抓失败导致缺页：{imgs}"
    assert f"{BASE}/pic/p3.webp" in imgs
    # 第 3 页被请求了 2 次（首次 429 + 重试成功）
    assert http.calls.count(3) == 2


def test_parallel_pagination_callback_failure_does_not_break_fetch():
    """on_page 回调抛异常不影响抓取完整性（回调是旁路，不参与正确性）。"""
    http = _FakeHttp(total=4)
    c = _content(http)
    src = _source(window=2)

    def _boom(_prefix):
        raise RuntimeError("renderer crashed")

    imgs = c.fetch_comic_pages(src, f"{BASE}/h/100.html", on_page=_boom)
    assert imgs == [f"{BASE}/pic/p{i}.webp" for i in range(1, 5)]


# ---------------------------------------------------------------------- #
def _make_view():
    from types import SimpleNamespace

    from framework.content import Chapter, Detail
    from gui.pages.reader.comic_view import ComicView

    view = ComicView(object())
    detail = Detail(
        source_id="demo", content_type="comic", url="http://c.example/m/1",
        title="漫画",
        chapters=[Chapter(f"第{i + 1}话", f"http://c.example/m/1/{i}.html") for i in range(6)],
    )
    view._detail = detail
    view._chapters = detail.chapters
    view._source = SimpleNamespace(source_id="demo")
    return view


def test_future_prefetch_jumps_ahead_of_backward_cache(_qapp):
    """读到 70% 触发的后续话预取插队到向前缓存之前（连看下一话优先就绪）。"""
    view = _make_view()
    view._prefetch_busy = False
    view._prefetch_queue = []
    view._prefetched = {}
    started = []
    view._start_next_prefetch = lambda: started.append(view._prefetch_queue[0])

    # 先向前缓存（前 2 话）→ 队列 [ch1, ch0]
    view._prefetch_prev(2)
    assert view._prefetch_queue == [
        view._chapters[1].url, view._chapters[0].url
    ]
    started.clear()

    # 后续预取（后 2 话）应插到队首，优先于向前缓存
    view._prefetch_future(2, 2)
    assert view._prefetch_queue == [
        view._chapters[3].url, view._chapters[4].url,
        view._chapters[1].url, view._chapters[0].url,
    ]
    # 队列第一个（下一话）被启动
    assert started == [view._chapters[3].url]


def test_future_prefetch_does_not_duplicate_queued_urls(_qapp):
    """重复触发后续预取（多次滚动到 70%）不重复入队。"""
    view = _make_view()
    view._prefetch_busy = False
    view._prefetch_queue = []
    view._prefetched = {}
    view._start_next_prefetch = lambda: None

    view._prefetch_future(1, 2)
    first = list(view._prefetch_queue)
    view._prefetch_future(1, 2)
    assert view._prefetch_queue == first


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
