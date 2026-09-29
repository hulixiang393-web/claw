from __future__ import annotations

from framework.shelf_cache_repository import ShelfCacheRepository


def _repo(tmp_path, **kw):
    return ShelfCacheRepository(tmp_path / "shelf.sqlite3", tmp_path / "content", **kw)


def test_default_total_quota_is_8gb(tmp_path):
    r = _repo(tmp_path)
    assert r.max_total_bytes == 8 * 1024 * 1024 * 1024
    assert r.max_book_bytes == 256 * 1024 * 1024


def test_pinned_book_survives_eviction(tmp_path):
    r = _repo(tmp_path, max_total_bytes=300, max_book_bytes=300)
    r.upsert_book("pinned", "s1", "u1", "novel", {})
    r.upsert_book("victim", "s1", "u2", "novel", {})
    r.set_book_pinned("pinned", True)
    r.put_content("pinned", "c1", "text/plain", "x" * 200, {})
    r.put_content("victim", "c1", "text/plain", "y" * 200, {})
    r.put_content("victim", "c2", "text/plain", "z" * 400, {})
    assert r.get_content("pinned", "c1") is not None
    assert r.pinned_book_keys() == ["pinned"]


def test_unpinned_book_gets_evicted_first(tmp_path):
    r = _repo(tmp_path, max_total_bytes=250, max_book_bytes=250)
    r.upsert_book("keepme", "s1", "u1", "novel", {})
    r.upsert_book("dropme", "s1", "u2", "novel", {})
    r.set_book_pinned("keepme", True)
    r.put_content("keepme", "c1", "text/plain", "x" * 200, {})
    r.put_content("dropme", "c1", "text/plain", "y" * 200, {})
    r.put_content("dropme", "c2", "text/plain", "z" * 300, {})
    assert r.get_content("keepme", "c1") is not None
    assert r.get_content("dropme", "c1") is None


def test_book_level_eviction_ignores_pin(tmp_path):
    r = _repo(tmp_path, max_total_bytes=10**9, max_book_bytes=200)
    r.upsert_book("big", "s1", "u1", "novel", {})
    r.set_book_pinned("big", True)
    r.put_content("big", "c1", "text/plain", "x" * 200, {})
    r.put_content("big", "c2", "text/plain", "y" * 400, {})
    assert r.get_content("big", "c1") is None


def test_oversized_payload_is_evicted_without_looping(tmp_path):
    r = _repo(tmp_path, max_total_bytes=10, max_book_bytes=10)
    r.upsert_book("big", "s1", "u1", "novel", {})
    result = r.put_content("big", "c1", "text/plain", "x" * 100, {})
    assert result["size"] == 100
    assert r.get_content("big", "c1") is None
    assert r.content_size() == 0


def test_cached_chapter_keys_and_size(tmp_path):
    r = _repo(tmp_path)
    r.upsert_book("bk", "s1", "u1", "novel", {})
    r.put_content("bk", "c1", "text/plain", "hello", {})
    r.put_content("bk", "c2", "text/plain", "world", {})
    assert sorted(r.cached_chapter_keys("bk")) == ["c1", "c2"]
    assert r.content_size("bk") > 0
    assert r.content_size() >= r.content_size("bk")


def test_clear_single_book_cache_clears_only_that_pin(tmp_path):
    r = _repo(tmp_path)
    r.upsert_book("bk", "s1", "u1", "novel", {})
    r.upsert_book("other", "s1", "u2", "novel", {})
    r.set_book_pinned("bk", True)
    r.set_book_pinned("other", True)
    r.put_content("bk", "c1", "text/plain", "x", {})
    r.put_content("other", "c1", "text/plain", "y", {})
    r.clear_content_cache("bk")
    assert r.is_book_pinned("bk") is False
    assert r.is_book_pinned("other") is True


def test_clear_all_cache_clears_all_pins(tmp_path):
    r = _repo(tmp_path)
    r.upsert_book("bk", "s1", "u1", "novel", {})
    r.set_book_pinned("bk", True)
    r.put_content("bk", "c1", "text/plain", "x", {})
    r.clear_content_cache()
    assert r.pinned_book_keys() == []


def test_content_survives_reopen(tmp_path):
    r1 = _repo(tmp_path)
    r1.upsert_book("bk", "s1", "u1", "novel", {})
    r1.put_content("bk", "c1", "text/plain", "持久内容", {})
    r1.close()
    r2 = _repo(tmp_path)
    assert r2.get_content("bk", "c1") == "持久内容"


class _Src:
    source_id = "s1"
    base_url = "https://s1.example"


def _content(repository):
    from framework.content import Content

    content = Content.__new__(Content)
    content._cache = None
    content._repository = repository
    return content


def test_cache_set_without_book_key_keeps_legacy_granularity(tmp_path):
    r = _repo(tmp_path)
    c = _content(r)
    c._cache_set("body:s1:https://x/ch1", "正文")
    assert r.cached_chapter_keys("https://x/ch1") == ["body"]


def test_cache_set_with_book_key_groups_chapters_per_book(tmp_path):
    r = _repo(tmp_path)
    c = _content(r)
    c._cache_set("body:s1:https://x/ch1", "正文1", book_key="BOOK")
    c._cache_set("body:s1:https://x/ch2", "正文2", book_key="BOOK")
    assert sorted(r.cached_chapter_keys("BOOK")) == [
        "https://x/ch1",
        "https://x/ch2",
    ]


def test_cache_get_with_book_key_roundtrips(tmp_path):
    r = _repo(tmp_path)
    c = _content(r)
    c._cache_set("body:s1:https://x/ch1", "正文1", book_key="BOOK")
    assert c._cache_get("body:s1:https://x/ch1", book_key="BOOK") == "正文1"


def test_cache_get_misses_when_wrong_book_key(tmp_path):
    r = _repo(tmp_path)
    c = _content(r)
    c._cache_set("body:s1:https://x/ch1", "正文1", book_key="BOOK")
    assert c._cache_get("body:s1:https://x/ch1", book_key="OTHER") is None


def test_same_chapter_url_isolated_between_books(tmp_path):
    r = _repo(tmp_path)
    c = _content(r)
    key = "body:s1:https://x/shared-chapter"
    c._cache_set(key, "BOOK1正文", book_key="BOOK1")
    c._cache_set(key, "BOOK2正文", book_key="BOOK2")
    assert c._cache_get(key, book_key="BOOK1") == "BOOK1正文"
    assert c._cache_get(key, book_key="BOOK2") == "BOOK2正文"
