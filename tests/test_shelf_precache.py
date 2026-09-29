from framework.shelf_precache import ShelfPrecacheJob
from framework.shelf_cache_repository import ShelfCacheRepository


class _Ch:
    def __init__(self, url):
        self.url = url
        self.title = url


class _Src:
    source_id = "s1"
    base_url = "https://s1.example"


class _StubContent:
    def __init__(self, fail_at=None):
        self.fetched = []
        self.fail_at = fail_at
        self.book_keys = []

    def _abs_url(self, src, url):
        return url

    def fetch_chapter(self, src, url):
        if self.fail_at == url:
            raise RuntimeError("boom")
        self.fetched.append(url)
        return f"正文 {url}"

    def _cache_get(self, key, book_key=None):
        return None

    def _cache_set(self, key, value, ttl=None, book_key=None):
        self.book_keys.append(book_key)


def _mk(tmp_path, content=None, ahead=3, n=10):
    repo = ShelfCacheRepository(tmp_path / "shelf.sqlite3", tmp_path / "content")
    content = content or _StubContent()
    job = ShelfPrecacheJob(
        content=content,
        source=_Src(),
        chapters=[_Ch(f"u{i}") for i in range(n)],
        start_idx=2,
        ahead=ahead,
        repository=repo,
        book_key="BOOK",
        on_progress=None,
    )
    return job, content, repo


def test_job_caches_current_plus_ahead(_qapp, tmp_path):
    job, content, repo = _mk(tmp_path, ahead=3)
    job.run()
    assert content.fetched == ["u2", "u3", "u4", "u5"]


def test_job_writes_through_repository_with_book_key(_qapp, tmp_path):
    job, content, repo = _mk(tmp_path, ahead=2)
    job.run()
    assert content.book_keys == ["BOOK", "BOOK", "BOOK"]
    assert sorted(repo.cached_chapter_keys("BOOK")) == ["u2", "u3", "u4"]
    assert repo.is_book_pinned("BOOK") is True


def test_job_does_not_cache_earlier_chapters(_qapp, tmp_path):
    job, content, repo = _mk(tmp_path, ahead=2)
    job.run()
    assert "u0" not in content.fetched and "u1" not in content.fetched


def test_job_continues_after_single_failure(_qapp, tmp_path):
    job, content, repo = _mk(tmp_path, content=_StubContent(fail_at="u3"), ahead=3)
    job.run()
    assert "u3" not in content.fetched
    assert "u4" in content.fetched


def test_job_cancel_stops_early(_qapp, tmp_path):
    job, content, repo = _mk(tmp_path, ahead=5)
    job.cancel()
    job.run()
    assert content.fetched == []


def test_job_ahead_zero_caches_current_only(_qapp, tmp_path):
    job, content, repo = _mk(tmp_path, ahead=0)
    job.run()
    assert content.fetched == ["u2"]


def test_job_clamps_at_end(_qapp, tmp_path):
    job, content, repo = _mk(tmp_path, ahead=5, n=3)
    job.run()
    assert content.fetched == ["u2"]


def test_job_evicts_outside_window(_qapp, tmp_path):
    repo = ShelfCacheRepository(tmp_path / "shelf.sqlite3", tmp_path / "content")
    repo.upsert_book("BOOK", "s1", "BOOK", "novel", {})
    repo.put_content("BOOK", "u0", "text/plain", "旧", {})
    repo.put_content("BOOK", "u1", "text/plain", "旧", {})
    job = ShelfPrecacheJob(
        _StubContent(), _Src(), [_Ch(f"u{i}") for i in range(10)],
        2, 1, repo, "BOOK", None,
    )
    job.run()
    assert sorted(repo.cached_chapter_keys("BOOK")) == ["u2", "u3"]
