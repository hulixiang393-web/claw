# 书架 SQLite 缓存、卡片稳定渲染与章节预加载 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 SQLite 元数据 + 文件内容缓存，消除搜索/发现/书架卡片抖动，并实现可配置的小说/漫画当前章节后 N 章预加载、18+ 搜索/发现过滤和加密收藏夹。

**Architecture:** SQLite 保存书架、目录、阅读位置、活动会话和缓存索引；正文/漫画图片使用原子写入的文件缓存。所有缓存调用统一经过 `ShelfCacheRepository`，卡片页面使用固定尺寸、批量建卡和渲染代次，阅读器以当前章节维护活动缓存窗口。

**Tech Stack:** Python 3.10+, stdlib `sqlite3`, PySide6, existing `RedisLikeStore`, `QThreadPool`, `ThreadPoolExecutor`, pytest.

## Global Constraints

- SQLite 使用 Python 标准库 `sqlite3`，不新增第三方数据库依赖。
- 不把大正文或漫画图片作为 SQLite BLOB；数据库只保存索引、元数据和状态，正文/图片保存为文件。
- 书架中的已收藏内容始终显示，不受全局 18+ 隐藏开关影响；书架是用户主动收藏的内容空间。
- 关闭 18+ 设置时，仅搜索页和发现页过滤确认的 18+ 源；不删除源配置、历史数据、书架记录、阅读位置或内容缓存。
- 阅读位置、章节目录、书架元数据永久保留；正文和漫画图片属于可清理内容缓存。
- 默认自动清理周期为 30 天；只清除内容窗口，不清除最后阅读位置和目录。
- 不记录视频内容缓存或视频播放进度。
- 迁移公共缓存接口时必须盘点并修改全部调用方，不能只替换底层实现。
- 保持 `Content.fetch_chapter`、`Content.fetch_comic_pages`、`ReadingProgress` 和 ReaderPage 现有兼容调用。
- 不提交 Git，除非用户明确要求；不得回滚无关已有工作区改动。

---

### Task 1: 建立 SQLite 书架缓存仓库与文件内容存储

**Files:**
- Create: `framework/shelf_cache_repository.py`
- Modify: `framework/cache_service.py`
- Modify: `framework/settings_manager.py`
- Test: `tests/test_shelf_cache_repository.py`

**Interfaces:**
- `ShelfCacheRepository(path, content_root, clock=None)`
- `upsert_book(book_key, source_id, book_url, content_type, metadata)`
- `replace_chapters(book_key, chapters)`
- `get_book_snapshot(book_key) -> dict | None`
- `get_content(book_key, chapter_key) -> bytes | str | None`
- `put_content(book_key, chapter_key, content_type, payload, metadata) -> dict`
- `update_location(book_key, location)`, `update_session(book_key, session)`
- `evict_book_content(book_key, keep_chapters)`, `clear_content_cache(book_key=None)`, `cleanup_inactive(days, now=None)`

- [ ] **Step 1: Write failing repository tests**

Cover schema creation, book/chapter upsert, atomic text/image writes, restart persistence, duplicate `put_content` idempotency, missing-file repair, per-book and global byte limits, and clear-content preserving book/chapter/location rows.

```python
def test_content_round_trips_after_repository_restart(tmp_path):
    repo = ShelfCacheRepository(tmp_path / "shelf.db", tmp_path / "content")
    repo.upsert_book("book-1", "demo", "https://book/1", "novel", {"title": "书"})
    repo.put_content("book-1", "chapter-1", "novel", "正文" * 100)
    repo2 = ShelfCacheRepository(tmp_path / "shelf.db", tmp_path / "content")
    assert repo2.get_content("book-1", "chapter-1") == "正文" * 100
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_shelf_cache_repository.py -q`

Expected: FAIL because the repository module and schema do not exist.

- [ ] **Step 3: Implement schema, transactions, and atomic file writes**

Use `sqlite3.connect(..., check_same_thread=False)` with an `RLock`, foreign keys, schema version, and explicit transactions. Store JSON metadata in text columns only for small metadata. Write payloads to a same-directory temporary file, flush/close, atomically replace, then commit `cache_entries`. On database/file mismatch, remove the stale row or re-index a valid manifest entry. Use gzip when no existing zstd utility is available.

- [ ] **Step 4: Add cleanup and quota tests, then pass them**

Run: `python -m pytest tests/test_shelf_cache_repository.py -q`

Expected: all repository tests pass, including automatic deletion of entries older than 30 days while retaining book, chapters, location, and session rows.

- [ ] **Step 5: Run compile and whitespace checks**

Run: `python -m compileall -q framework tests; git diff --check`

---

### Task 2: Migrate existing progress/shelf/cache data into the repository

**Files:**
- Modify: `framework/reading_progress.py`
- Modify: `framework/shelf_service.py`
- Modify: `framework/cache_service.py`
- Modify: `gui/app.py`
- Create: `framework/shelf_cache_migration.py`
- Test: `tests/test_shelf_cache_migration.py`
- Test: `tests/test_reading_progress_shelf.py`

**Interfaces:**
- `migrate_legacy_data(repository, reading_progress_path, shelf_service, legacy_cache) -> MigrationReport`
- Migration is idempotent and never overwrites a newer SQLite location with an older JSON record.

- [ ] **Step 1: Write failing migration tests**

Create legacy JSON and fake RedisLikeStore fixtures. Assert first startup imports books, chapters, and locations; second startup does not duplicate or regress newer SQLite records; a failed migration leaves old files usable.

- [ ] **Step 2: Run migration tests to confirm failure**

Run: `python -m pytest tests/test_shelf_cache_migration.py tests/test_reading_progress_shelf.py -q`

- [ ] **Step 3: Implement idempotent migration and startup wiring**

Run migration after repository creation and before pages are built. Preserve `reading_progress.json` as a backup. Keep existing JSON fallback if SQLite creation/migration fails. Route shelf add/remove and progress writes through the repository while retaining current public methods for callers not yet migrated.

- [ ] **Step 4: Audit all old cache/progress call sites**

Search and record every use of `reading_progress`, `get_shelf_cache`, `precache_chapters`, `fetch_chapter`, and comic page cache. Update each caller or place it behind the repository adapter; no direct production call may bypass the chosen compatibility boundary without an explicit test.

- [ ] **Step 5: Run migration and existing progress tests**

Run: `python -m pytest tests/test_shelf_cache_migration.py tests/test_reading_progress_shelf.py tests/test_read_restore_cross_book.py -q`

---

### Task 3: Add global 18+ source policy and encrypted collections

**Files:**
- Create: `framework/content_policy.py`
- Modify: `framework/shelf_service.py`
- Modify: `framework/settings_manager.py`
- Modify: `gui/pages/settings_page.py`
- Modify: `gui/pages/search_page.py`
- Modify: `gui/pages/discover_page.py`
- Modify: `gui/pages/library_page.py`
- Test: `tests/test_content_policy.py`
- Test: `tests/test_collection_security.py`

**Interfaces:**
- `is_adult_source(source_id) -> bool`
- `filter_visible_results(items, show_adult_sources, source_id_getter) -> list`
- `CollectionSecurity.set_password(collection_id, password)`, `verify(collection_id, password)`, `lock(collection_id)`, `is_unlocked(collection_id)`
- `ShelfService` collection methods must require an unlocked state for encrypted collections.

- [ ] **Step 1: Write failing policy/security tests**

Assert the exact 25 confirmed IDs are adult, unknown IDs are not adult, search/discovery filtering hides them when disabled, library filtering does not hide already-shelved cards, and password hashes never contain plaintext. Assert unlock is valid within one process and invalid in a new process/after lock.

- [ ] **Step 2: Run tests to confirm failure**

Run: `python -m pytest tests/test_content_policy.py tests/test_collection_security.py -q`

- [ ] **Step 3: Implement centralized policy and password derivation**

Use `hashlib.scrypt` when available, otherwise PBKDF2-HMAC-SHA256 with a random salt and versioned parameters. Store only salt, derived digest, algorithm, and parameters in SQLite. Keep unlocked collection IDs in memory only. Apply policy before card creation in SearchPage/DiscoverPage, and explicitly bypass it in LibraryPage.

- [ ] **Step 4: Add settings and collection UI behavior**

Add `content.show_adult_sources` default false, a settings checkbox, collection encryption toggle, password creation/change dialog, unlock dialog, lock action, and locked-state rendering that does not load collection contents. Incorrect passwords must not reveal item metadata or content.

- [ ] **Step 5: Run policy/security tests and compile**

Run: `python -m pytest tests/test_content_policy.py tests/test_collection_security.py -q; python -m compileall -q framework gui tests`

---

### Task 4: Eliminate card layout jitter across SearchPage, DiscoverPage, and LibraryPage

**Files:**
- Modify: `gui/components/work_card.py`
- Modify: `gui/components/cover_loader.py`
- Modify: `gui/pages/search_page.py`
- Modify: `gui/pages/discover_page.py`
- Modify: `gui/pages/library_page.py`
- Test: `tests/test_card_batch_render.py`
- Test: existing search/discover/library GUI tests

**Interfaces:**
- Add a shared batch renderer or helper with `append_batch(items, batch_size, render_one)` and generation cancellation.
- Existing page signals and card click payloads remain unchanged.

- [ ] **Step 1: Write failing rendering tests**

Use offscreen Qt fixtures to append hundreds of cards and assert: fixed card geometry, batch count is bounded, old generation callbacks do not append, non-bottom scroll value is unchanged, and cover completion updates only its card.

- [ ] **Step 2: Run tests to confirm current jitter-prone behavior**

Run: `python -m pytest tests/test_card_batch_render.py tests/test_search_page_lazyload.py tests/test_search_page_scroll_guard.py tests/test_library_open_jump.py -q`

- [ ] **Step 3: Implement batch construction and one-layout updates**

Create cards in bounded batches from the existing result list. During each batch block target layout updates, append all cards, update column stretch once, then re-enable updates and schedule the next batch with `QTimer.singleShot(0 or 16, ...)`. Keep a render epoch per page and discard stale callbacks. Preserve bottom anchoring only when the user was already at bottom.

- [ ] **Step 4: Move cover decode/scale off the GUI hot path**

Keep network work in CoverLoader workers and move byte decoding/scaling into worker-safe `QImage` processing. Main-thread callbacks only create/update the final pixmap in the fixed cover label. Remove per-card fade animations for bulk loads or coalesce them per batch. Do not change card height when author/source/cover changes.

- [ ] **Step 5: Run GUI rendering regressions**

Run the focused rendering tests and existing card/search/discovery/library tests; record any known PySide6 process-level crash separately rather than changing unrelated behavior.

---

### Task 5: Integrate reader cache window and configurable preloading

**Files:**
- Modify: `framework/content.py`
- Modify: `gui/pages/reader_page.py`
- Modify: `gui/pages/reader/novel_view.py`
- Modify: `gui/pages/reader/comic_view.py`
- Modify: `framework/settings_manager.py`
- Modify: `gui/pages/settings_page.py`
- Test: `tests/test_reader_cache_window.py`
- Test: `tests/test_reading_progress_shelf.py`
- Test: existing novel/comic reader tests

**Interfaces:**
- `Content.precache_chapters(..., ahead: int | None = None)` reads the configured default when `ahead` is omitted.
- `ShelfCacheRepository` is the source of truth for cached body/pages and session window.
- `ReaderPage._start_precache` schedules current chapter plus configured `N` following chapters, with current content priority.

- [ ] **Step 1: Write failing cache-window tests**

Assert default N=10, configurable N, current chapter plus N following chapters are scheduled, old chapters are evicted after advancing, duplicate tasks are not submitted, and current content is not blocked by background preloading.

- [ ] **Step 2: Run tests to confirm failure**

Run: `python -m pytest tests/test_reader_cache_window.py tests/test_reading_progress_shelf.py -q`

- [ ] **Step 3: Implement repository-backed novel cache**

On `fetch_chapter`, check repository first for shelf books, then existing compatibility cache, then network. On success write compressed body and update `cache_entries`. At chapter change update `sessions`, submit a bounded worker for the current+N window, and evict only content outside the window and quota policy.

- [ ] **Step 4: Implement repository-backed comic image cache**

Persist actual image bytes and stable image keys; reuse cached images before network. Preloading must be cancellable on book switch, dedupe by book/chapter/image key, and avoid writing results from stale reader generations.

- [ ] **Step 5: Add settings and clear-cache actions**

Expose N, per-book limit, total limit, auto-clean toggle, and 30-day inactive days in SettingsPage. Add a clear content-cache action that preserves books, chapters, locations, and sessions. Use a background cleanup task and report completion without freezing the UI.

- [ ] **Step 6: Run reader/cache regressions**

Run: `python -m pytest tests/test_reader_cache_window.py tests/test_reading_progress_shelf.py tests/test_read_restore_cross_book.py tests/test_reader_zzz.py tests/test_comic_scroll_anchor.py tests/test_comic_pages_cache.py -q`

---

### Task 6: Wire offline shelf cards and repository lifecycle

**Files:**
- Modify: `gui/pages/library_page.py`
- Modify: `framework/shelf_service.py`
- Modify: `gui/app.py`
- Modify: `framework/shelf_cache_repository.py`
- Test: `tests/test_library_offline_cache.py`
- Test: `tests/test_shelf_cache_repository.py`

**Interfaces:**
- Library card construction uses repository snapshots and never requires network for metadata.
- Adding/removing a shelf item updates repository book/chapter rows and removes content files only on explicit removal.

- [ ] **Step 1: Write failing offline shelf tests**

Populate SQLite/files, disable network, build LibraryPage, and assert cards show immediately with cached metadata/cover. Assert removing a book deletes its repository rows/files; clear-content preserves the book and location.

- [ ] **Step 2: Run tests to confirm failure**

Run: `python -m pytest tests/test_library_offline_cache.py -q`

- [ ] **Step 3: Implement offline-first library rendering**

Use repository snapshots in the background scan, render cards in batches, and pass cached cover paths/bytes to fixed cover widgets. Do not trigger `fetch_detail` or network when metadata exists locally. Keep local-video behavior unchanged.

- [ ] **Step 4: Verify lifecycle and restart behavior**

Run repository, migration, and offline library tests across two repository instances to simulate app restart; assert reading positions remain after content clear and inactive cleanup.

---

### Task 7: Full call-site audit, integration tests, and quality gates

**Files:**
- Modify: all files identified by repository/cache call-site audit
- Test: `tests/test_shelf_integration.py`
- Test: all affected existing test files
- Modify: `SESSION.md` and `learning-log.md` only after verified completion

**Interfaces:**
- No old direct cache call remains unreviewed; compatibility paths have explicit tests.
- Search/discover adult filtering and library bypass share the same policy function.
- Collection unlock is process-scoped and expires on application restart.

- [ ] **Step 1: Run repository-wide call-site searches**

Search for `reading_progress`, `get_shelf_cache`, `RedisLikeStore`, `precache_chapters`, `fetch_chapter`, `fetch_comic_pages`, card construction, and collection access. Create/update a migration checklist and resolve every production call site.

- [ ] **Step 2: Add end-to-end integration tests**

Cover: add book to shelf → restart → offline card → open cached current chapter → prefetch N → advance chapter → evict old content → clear cache → reopen and reload content; include 18+ search/discovery filtering, always-visible shelved 18+ card, encrypted collection unlock/restart, and card batch loading.

- [ ] **Step 3: Run relevant regression tests**

Run the repository’s affected suites, including repository/migration/security/card/reader/search/discover/library tests and all existing media/reader regression tests.

- [ ] **Step 4: Run final quality gates**

Run:

```powershell
python -m compileall -q framework gui tests
python -m pytest --ignore=tests/test_comic_scroll_anchor.py --ignore=tests/test_comic_view_referer.py -p no:randomly -q
git diff --check
git status --short
```

Run configured lint/typecheck if present; if none exists, record that compileall and pytest are the available gates. Review `git diff --stat`, `git diff`, and all changed call sites before any requested commit.
