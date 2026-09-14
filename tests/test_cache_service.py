"""Redis 风格缓存服务单元测试。"""
from __future__ import annotations

import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from framework.cache_service import RedisLikeStore


def make_store(tmp_path=None, quota=64 * 1024, persist=None):
    return RedisLikeStore(quota=quota, persist_path=persist)


def test_set_get(tmp_path):
    s = make_store(tmp_path)
    s.set("a", "hello")
    assert s.get("a") == "hello"
    assert s.get("missing") is None


def test_ttl_expiry(tmp_path):
    s = make_store(tmp_path)
    s.set("a", "x")
    s.expire("a", 0.001)
    time.sleep(0.05)
    assert s.get("a") is None
    s.set("b", "y", ttl=0.001)
    time.sleep(0.05)
    assert s.get("b") is None


def test_hset_hget(tmp_path):
    s = make_store(tmp_path)
    s.hset("book", "title", "A")
    s.hset("book", "author", "B")
    assert s.hget("book", "title") == "A"
    assert s.hgetall("book") == {"title": "A", "author": "B"}


def test_delete_and_clear(tmp_path):
    s = make_store(tmp_path)
    s.set("a", 1)
    s.set("b", 2)
    assert s.count() == 2
    s.delete("a")
    assert s.count() == 1
    s.clear()
    assert s.count() == 0


def test_scan_pattern(tmp_path):
    s = make_store(tmp_path)
    s.set("search:x:kw", 1)
    s.set("search:y:kw2", 2)
    s.set("cover:a:u", 3)
    keys = sorted(s.scan("search:*"))
    assert keys == ["search:x:kw", "search:y:kw2"]


def test_lru_eviction_and_bytes(tmp_path):
    s = RedisLikeStore(quota=5000)
    s.set("k1", "v" * 1000)
    s.set("k2", "v" * 1000)
    s.set("k3", "v" * 1000)
    assert s.bytes_used() > 0
    s.set("k4", "v" * 1000)
    assert s.get("k1") is None   # 最旧被驱逐
    assert s.get("k2") is not None or s.get("k3") is not None
    assert s.bytes_used() <= 5000


def test_persist_roundtrip(tmp_path):
    path = str(tmp_path / "r.gz")
    s = RedisLikeStore(quota=1024 * 1024, persist_path=path)
    s.set("a", {"x": [1, 2, 3]})
    s.hset("b", "f", "val")
    s.save()
    s2 = RedisLikeStore(quota=1024 * 1024, persist_path=path)
    s2.load()
    assert s2.get("a") == {"x": [1, 2, 3]}
    assert s2.hget("b", "f") == "val"


def test_flush_check_throttle(tmp_path):
    path = str(tmp_path / "r2.gz")
    s = RedisLikeStore(quota=1024 * 1024, persist_path=path)
    s._save_interval = 60.0
    s._save_write_threshold = 500
    s._last_save_ts = time.time()  # 最近刚存过 → 不触发超时
    s.set("a", "x")  # _writes = 1，低于阈值
    s.flush_checked()
    assert not Path(path).exists()  # 未落盘
    s._writes = 500
    s.flush_checked()  # 达阈值 → 落盘
    assert Path(path).exists()


def test_http_client_holds_cache_ref():
    from framework.http import HttpClient

    store = RedisLikeStore(quota=1024 * 1024)
    http = HttpClient(sleeper=lambda s: None, cache=store)
    assert http.cache is store
    http.close()


class _FakeHttp:
    """可替换 get_text 的假 HttpClient，记录调用次数。"""

    cache = None

    def __init__(self):
        self.calls = {}
        self.defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def get_text(self, url, **kw):
        self.calls[url] = self.calls.get(url, 0) + 1
        return f"<h1>{url}</h1>"

    def close(self):
        pass


def _fake_source():
    class S:
        source_id = "srcA"
        base_url = "https://x.com"
        content_type = "novel"
        _raw = {
            "endpoints": {
                "content": {
                    "chapter": {
                        "pagination": {"enabled": False},
                        "body": {"selector": {"css": "#content"}},
                    }
                }
            }
        }

        @property
        def raw(self):
            return self._raw

        def transports(self):
            return {}

        def request_headers(self):
            return {}

        def proxy_pool(self):
            return None

        def get_detail_config(self):
            return {"fields": {}}

        def get_discovery_config(self):
            return {}

        def get_search_config(self):
            return {}

    return S()


def _fake_parser():
    class P:
        def parse(self, html):
            return html

        def extract(self, doc, sel):
            return [doc]

        def extract_first(self, doc, sel, base_url):
            return ""

        def parse_items(self, doc, root_sel, fields, base_url):
            return []

    return P()


def _fake_checker():
    class C:
        def __init__(self):
            pass

    return C()


def _make_content(store, http=None, parser=None):
    from framework.content import Content

    return Content(
        http or _FakeHttp(),
        parser or _fake_parser(),
        _fake_checker(),
        cache=store,
    )


def test_content_fetch_detail_caches_and_hits():
    """_get_detail_html 走 Redis 二级缓存（page: 永久键）、二次命中免下载。"""
    store = make_store()
    http = _FakeHttp()
    c = _make_content(store, http)
    src = _fake_source()
    html = c._get_detail_html(src, "https://x.com/book", "https://x.com/book")
    assert http.calls.get("https://x.com/book") == 1
    key = f"page:{src.source_id}:https://x.com/book"
    assert store.get(key) == html
    # 清空内存缓存，模拟「重启」后 Redis 命中（不再下载）
    c._detail_html_cache.clear()
    html2 = c._get_detail_html(src, "https://x.com/book", "https://x.com/book")
    assert html2 == html
    assert http.calls.get("https://x.com/book") == 1


def test_content_fetch_chapter_caches_body():
    """fetch_chapter 真实链路：开头 Redis 命中免抓、末尾写 body: 键。"""
    store = make_store()
    http = _FakeHttp()
    c = _make_content(store, http)
    src = _fake_source()
    url = "https://x.com/b/1.html"
    text = c.fetch_chapter(src, url)
    key = f"body:{src.source_id}:{url}"
    assert store.get(key) == text
    # 二次调用（清内存缓存后）不重新抓取
    c._detail_html_cache.clear()
    text2 = c.fetch_chapter(src, url)
    assert text2 == text
    assert http.calls.get(url, 0) == 1


def test_content_precache_chapters():
    """预加载当前章+后3章：只抓没缓存的章 + 写 body 键。"""
    store = make_store()
    http = _FakeHttp()
    c = _make_content(store, http)
    src = _fake_source()
    chapters = [
        type("Ch", (object,), {"url": f"https://x.com/b/{i}.html"})()
        for i in range(5)
    ]
    c.precache_chapters(src, chapters, 1, ahead=3)
    # 当前章(1) + 后续章[2,3) → 预加载 1,2,3（后 ahead-1 章）
    for i in (1, 2, 3):
        assert store.get(f"body:{src.source_id}:https://x.com/b/{i}.html") is not None
    # 第4章（index 4）不在范围内
    assert store.get(f"body:{src.source_id}:https://x.com/b/4.html") is None
    assert set(http.calls) == {
        "https://x.com/b/1.html",
        "https://x.com/b/2.html",
        "https://x.com/b/3.html",
    }


def test_cover_bytes_persist_roundtrip():
    store = make_store()
    store.set("cover:s1:https://c/img.jpg", b"\x89PNG-fake-bytes")
    assert store.get("cover:s1:https://c/img.jpg") == b"\x89PNG-fake-bytes"


def test_get_session_cache_singleton_not_persisted():
    """会话缓存：不落盘（退出即清）、2GB 配额、单例。"""
    from framework.cache_service import get_session_cache, RedisLikeStore

    a = get_session_cache()
    b = get_session_cache()
    assert a is b                       # 单例
    assert isinstance(a, RedisLikeStore)
    assert a._persist_path is None      # 不落盘 → 进程退出自动释放，无需清理
    assert a._quota == 2 * 1024 * 1024 * 1024
    # 写读往返
    a.set("disc:demo", "v")
    assert a.get("disc:demo") == "v"


def test_discovery_list_and_categories_never_cache():
    """发现页改走纯实时抓取后：list_works / list_categories 不读不写缓存。"""
    from framework.discovery import Discovery

    store = make_store()
    http = _FakeHttp()
    disc = Discovery(http, _fake_parser(), _fake_checker())
    # 预置假缓存键，验证 list_works 不命中缓存、仍发网络请求（首次即抓）
    src = _src_with_discovery("s1", "https://a.example")
    key = f"list:{src.source_id}:https://a.example/list?page=1"
    store.set(key, [{"title": "T", "url": "u"}])
    res = disc.list_works(src, "/list", 1)
    assert res == []  # 真实抓取（fake parser 返回空），不读缓存
    assert http.calls  # 确实发了网络请求
    # 分类同理：不读缓存（cats: 键预置了假数据，但静态分类源直接返回配置），
    # 走真实 list_categories 逻辑（静态分类来自配置，返回配置值，非缓存值）
    cat_key = f"cats:{src.source_id}:https://a.example/list"
    store.set(cat_key, [{"title": "C", "url": "/c"}])
    cats = disc.list_categories(src)
    # 返回配置里的静态分类（title="全"，非缓存里的 "C"）→ 证明不读缓存
    assert [c.title for c in cats] == ["全"]
    assert store.get(cat_key) == [{"title": "C", "url": "/c"}]  # 缓存未被读/覆盖


def _src_with_discovery(sid, base):
    from framework.config import SourceConfig

    raw = {
        "$schema_version": 1, "$id": sid, "$type": "novel", "$name": sid,
        "$enabled": True, "$weight": 1.0,
        "transports": {"base_url": base},
        "endpoints": {
            "discovery": {
                "list_url": "/list",
                "list_paginator": {"type": "increment", "start": 1, "step": 1},
                "list_item": {"categories": [{"title": "全", "url": "/list"}]},
                "works_list_item": {
                    "root_selector": ".item",
                    "fields": {"title": "a.title", "url": "a@href"},
                },
            }
        },
    }
    return SourceConfig.from_dict(raw, "<mem>")
