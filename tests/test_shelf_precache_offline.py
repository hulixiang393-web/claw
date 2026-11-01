"""书架离线预加载端到端：预加载写进仓库的正文，阅读器必须读得到。

回归的是两个键空间错位的问题：
  1. 正文缓存键是 `body:<源>:t2:<url>`（带文本抽取版本），仓库键必须是干净 URL；
  2. `ShelfPrecacheJob` 收尾用 `evict_book_content(book_key, kept=URL)` 淘汰区间外
     的章节，若写进去的键带 `t2:`，刚缓存的章节会被自己判成「区间外」立刻删掉。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.content import Content  # noqa: E402
from framework.shelf_cache_repository import ShelfCacheRepository  # noqa: E402

from test_cache_service import _FakeHttp, _fake_parser, _fake_source  # noqa: E402


def _make_content(repo):
    class Checker:
        def check(self, source, url):
            return True

    return Content(_FakeHttp(), _fake_parser(), Checker(), cache=None, repository=repo)


def _chapters(urls):
    return [type("Ch", (object,), {"url": url})() for url in urls]


def test_shelf_precache_survives_finalize_eviction(tmp_path):
    from framework.shelf_precache import ShelfPrecacheJob

    repo = ShelfCacheRepository(tmp_path / "shelf.db", tmp_path / "content")
    content = _make_content(repo)
    source = _fake_source()
    book_key = "https://x.com/book"
    urls = ["https://x.com/b/%d.html" % i for i in range(5)]
    job = ShelfPrecacheJob(
        content=content,
        source=source,
        chapters=_chapters(urls),
        start_idx=1,
        ahead=2,
        repository=repo,
        book_key=book_key,
    )
    job.run()
    cached = set(repo.cached_chapter_keys(book_key))
    # 区间内（1,2,3）三章留下，区间外（0,4）不写
    assert cached == set(urls[1:4]), cached
    # 阅读器按自己的缓存键读回来（URL 前缀不同也能命中）
    for url in urls[1:4]:
        key = content._chapter_cache_key(source, url)
        assert content._cache_get(key, book_key=book_key) == "<h1>%s</h1>" % url
    repo.close()


def test_shelf_precache_reuses_cached_chapter_without_refetch(tmp_path):
    from framework.shelf_precache import ShelfPrecacheJob

    repo = ShelfCacheRepository(tmp_path / "shelf.db", tmp_path / "content")
    http = _FakeHttp()
    content = Content(http, _fake_parser(), object(), cache=None, repository=repo)
    source = _fake_source()
    book_key = "https://x.com/book"
    urls = ["https://x.com/b/%d.html" % i for i in range(3)]
    ShelfPrecacheJob(content, source, _chapters(urls), 0, 2, repo, book_key).run()
    assert set(http.calls) == set(urls)
    # 第二次运行：全部命中仓库，不再下载
    http.calls.clear()
    ShelfPrecacheJob(content, source, _chapters(urls), 0, 2, repo, book_key).run()
    assert http.calls == {}, http.calls
    repo.close()
