import json
import time

from framework.config import SourceConfig
from framework.content import Content, Detail


def _src():
    return SourceConfig.from_dict({
        "$schema_version": 2, "$id": "s1", "$type": "novel", "$name": "S1",
        "transports": {"base_url": "https://s1.example"},
        "$metadata": {"homepage": "https://s1.example"},
    }, "s1.json")


class _Cache:
    def __init__(self):
        self.d = {}
        self.ttls = {}

    def get(self, key):
        expires = self.ttls.get(key)
        if expires is not None and expires <= time.time():
            self.d.pop(key, None)
            return None
        return self.d.get(key)

    def set(self, key, value, ttl=None):
        self.d[key] = value
        self.ttls[key] = time.time() + ttl if ttl is not None else None


class _Repository:
    def __init__(self):
        self.values = {}

    def get_content(self, book_key, chapter_key):
        return self.values.get((book_key, chapter_key))

    def put_content(self, book_key, chapter_key, content_type, content, headers):
        self.values[(book_key, chapter_key)] = content

    def upsert_book(self, *args):
        pass


def _content(cache, repository=None):
    c = Content.__new__(Content)
    c._cache = cache
    c._repository = repository
    return c


def test_detail_cache_hit_skips_network():
    calls = []
    c = _content(_Cache())
    c._cache_set_detail(
        f"detail:s1:{c._abs_url(_src(), 'https://s1.example/book/1')}",
        Detail(title="书一", url="https://s1.example/book/1", source_id="s1",
               content_type="novel", chapters=[]),
    )

    def _record(*args, **kwargs):
        calls.append((args, kwargs))
        return Detail(title="网络返回的错内容", url="", source_id="", content_type="novel")

    c._fetch_detail_page = _record
    detail = c.fetch_detail(_src(), "https://s1.example/book/1")
    assert calls == []
    assert detail.title == "书一"


def test_detail_cache_set_get_roundtrip():
    c = _content(_Cache())
    key = f"detail:s1:{c._abs_url(_src(), 'https://s1.example/book/2')}"
    before = time.time() + c._DETAIL_TTL
    c._cache_set_detail(key, Detail(title="书二", url="u2", source_id="s1",
                                     content_type="novel", chapters=[]))
    assert abs(c._cache.ttls[key] - before) < 2
    got = c._cache_get_detail(key)
    assert got is not None
    assert got.title == "书二"


def test_detail_cache_roundtrip_preserves_chapters():
    c = _content(_Cache())
    key = "detail:s1:https://s1.example/book/3"
    detail = Detail(
        title="书三", url="u3", source_id="s1", content_type="novel",
        chapters=[{"title": "第一章", "url": "c1"}],
    )
    c._cache_set_detail(key, detail)
    got = c._cache_get_detail(key)
    assert got.chapters[0].title == "第一章"
    assert got.chapters[0].url == "c1"


def test_detail_cache_get_malformed_returns_none():
    cache = _Cache()
    cache.d["detail:s1:bad"] = "not-json"
    assert _content(cache)._cache_get_detail("detail:s1:bad") is None


def test_detail_cache_expired_returns_none():
    cache = _Cache()
    cache.d["detail:s1:expired"] = json.dumps({"title": "旧"})
    cache.ttls["detail:s1:expired"] = time.time() - 1
    assert _content(cache)._cache_get_detail("detail:s1:expired") is None


def test_detail_cache_does_not_use_repository_chapter_entries():
    repository = _Repository()
    repository.values[("https://s1.example/book/4", "detail")] = json.dumps({"title": "错误详情"})
    c = _content(_Cache(), repository)
    assert c._cache_get_detail("detail:s1:https://s1.example/book/4") is None
