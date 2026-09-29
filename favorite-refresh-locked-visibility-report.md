# Favorite Refresh and Locked Visibility Review

## Status

Fixed `Content.refresh_detail()` so repository-backed chapter bodies are evicted with `ShelfCacheRepository.evict_book_content()` using the existing per-book cache conventions. Redis/in-memory body entries are still invalidated, and the current chapter is excluded from the keep set so a favorite refresh refetches it.

## Covered behavior

- Repository body cache is removed for the refreshed book, without clearing unrelated books.
- Chapters absent from the freshly fetched detail list are evicted.
- Latest listed chapters remain cached unless they are the explicitly refreshed current chapter.
- Favorite/current chapter refresh refetches fresh content instead of returning stale repository data.
- Nonfavorite cache policy and locked-folder visibility behavior remain unchanged.

## Verification

- `tests/test_shelf_privacy_cache_regressions.py tests/test_shelf_offline_cache.py`: 30 passed.
- `tests/test_cache_service.py`: 20 passed.
- The combined folder/reader run reached passing tests but did not exit within the five-minute command timeout; no failure traceback was produced.
- `compileall` and `git diff --check`: run after the code change.
