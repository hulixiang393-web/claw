# 书架卡片懒加载详情 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 启动不加载书架内容，点击书架 Tab 只加载卡片，点击具体卡片才加载对应缓存或在线详情，本地内容继续直接打开。

**Architecture:** 延迟构造书架、发现和搜索页面，禁止启动网络预加载/全源自检。书架 Tab 触发轻量卡片扫描；LibraryPage 对在线卡片使用后台详情任务、请求键去重和 generation 守卫，卡片点击只加载当前作品。

**Tech Stack:** Python 3.10、PySide6、pytest、Qt `QThreadPool`/`QRunnable`/`Signal`。

## Global Constraints

- 启动阶段不构造书架、发现、搜索页面，不启动发现预加载和全源自检。
- 点击书架 Tab 只读取卡片元数据、封面和阅读进度。
- 点击具体卡片才读取对应本地缓存或在线详情/目录；不读取其它卡片内容。
- 本地 EPUB、漫画和视频点击保持本地直开。
- 封面保留异步加载；封面补全只能使用 `fetch_cover()`，不能升级为完整详情请求。
- 在线详情加载必须在后台线程，不阻塞 Qt 主线程。
- 快速切换卡片时，旧响应不能打开旧作品；同一作品在途请求不得重复。
- 不修改 `app_config.json`、三个已恢复源、下载顺序和阅读器图片功能。
- 不提交 git commit。

---

## File Map

- Modify `gui/pages/library_page.py`: online detail worker、请求状态、卡片点击懒加载、本地分支保持不变。
- Modify `gui/app.py`: 延迟构造书架/发现/搜索/源管理/设置页面，取消启动发现预加载和全源自检，首次点击 Tab 时构造。
- Modify `framework/shelf_service.py` only if tests prove records carry eager detail data; otherwise no change.
- Create `tests/test_library_lazy_detail.py`: no-detail-on-build、click-load、local-direct、stale-response、duplicate-inflight tests.
- Update `SESSION.md` and add `docs/superpowers/reports/2026-10-06-shelf-card-lazy-detail-report.md` after validation.

## Task 1: Prove and Implement Lazy Online Detail Loading

**Interfaces:**
- Add `_DetailLoadSignals.finished = Signal(object, object, object, object)` carrying `(request_key, detail, error, generation)`.
- Add `_DetailLoadTask(content, source, request_key, generation)`; `run()` calls `content.fetch_detail(source, url)` and emits exactly one result.
- `LibraryPage._open_rec(rec)` remains the click entry point; local records emit existing local signals immediately, online records call `_load_online_detail(rec)`.
- `_load_online_detail(rec)` deduplicates by `(source_id, url)`, starts a `QThreadPool` task, and only emits `open_online_requested((source_id, url, content_type))` for the current generation/key after success.

- [ ] **Step 1: Write failing tests**

Create `tests/test_library_lazy_detail.py` with lightweight fake shelf/content/source objects and Qt event processing. Cover:

```python
def test_library_build_does_not_fetch_online_detail(_qapp):
    content = RecordingContent()
    page = LibraryPage(shelf_service=FakeShelf([online_record()]), content=content)
    page._scan_task = None
    page._render([online_record()])
    assert content.detail_calls == []


def test_online_card_click_fetches_detail_before_open_signal(_qapp, monkeypatch):
    content = RecordingContent(detail=FakeDetail())
    page = LibraryPage(shelf_service=FakeShelf([]), content=content,
                       source_manager=FakeSources())
    opened = []
    page.open_online_requested.connect(opened.append)
    page._open_rec(online_record())
    wait_for_qt_tasks(_qapp)
    assert content.detail_calls == [("source-1", "https://example.test/book")]
    assert opened == [("source-1", "https://example.test/book", "novel")]
```

Also add tests for local EPUB direct signal, local video direct signal, stale A response after B click, and repeated click on same online key producing one detail call. Use synchronization events in the fake content rather than sleeps.

- [ ] **Step 2: Run tests and verify RED**

Run: `C:\Users\alonely\AppData\Local\Programs\Python\Python310\python.exe -m pytest tests/test_library_lazy_detail.py -q`
Expected: FAIL because online clicks currently emit immediately and no detail worker/deduplication exists.

- [ ] **Step 3: Implement worker and request state**

In `gui/pages/library_page.py`, add `_DetailLoadSignals` and `_DetailLoadTask` beside `_ScanTask`. Add `self._detail_tasks: dict[tuple[str, str], _DetailLoadTask]`, `self._detail_generation = 0`, and `self._detail_loading_keys: set`. In `_load_online_detail`, resolve the source by `source_id`; if unavailable, show a retryable warning and return. For an existing key, return without starting another task. Increment generation, record the key, start the task, and connect to `_on_detail_loaded`.

In `_on_detail_loaded`, remove the key from in-flight state. Ignore results whose generation is not current or whose key is not current. On error/detail missing, update the card status if available or show a non-blocking `QMessageBox.warning`, then allow retry. On success emit the existing tuple exactly once. Do not pass a fetched full `Detail` through the shelf record or trigger prefetch here; `ReaderPage.open()` owns detail loading after the card-open signal.

- [ ] **Step 4: Verify focused tests pass**

Run: `python -m pytest tests/test_library_lazy_detail.py -q`
Expected: PASS, including local direct-open and stale/duplicate cases.

## Task 2: Remove Any Startup Eager Detail/Precache Path

**Interfaces:**
- `LibraryPage._render()` may render records containing a historical `detail` field but must ignore its chapters and never call `fetch_detail` or pre-cache automatically.
- `_maybe_backfill_covers()` may call only the injected lightweight cover callback.
- `_precache_book()` remains an explicit user action only.

- [ ] **Step 1: Add regression tests for forbidden startup work**

Extend `tests/test_library_lazy_detail.py` with a fake content recording `fetch_detail`, `fetch_chapter`, `precache_chapters`, and `fetch_cover`. Render records containing a synthetic full `detail` object and assert all detail/chapter/precache counters remain zero. Invoke `_precache_book()` only through its explicit method test boundary if existing setup supports it; do not treat explicit caching as startup work.

- [ ] **Step 2: Run test and verify RED if current code violates it**

Run: `python -m pytest tests/test_library_lazy_detail.py::test_render_ignores_historical_detail_without_prefetch -q`
Expected: FAIL only if an eager path is present; if it passes before implementation, retain it as a regression proving the current boundary.

- [ ] **Step 3: Remove only confirmed eager calls**

Trace any failing call from `LibraryPage.__init__` → `_rebuild` → `_ScanTask` → `_render` and from `MainWindow._build_library`. Remove or defer only complete-detail/chapters/precache calls. Do not remove `_load_cover()` or lightweight `fetch_cover()` backfill. Do not change shelf filtering, grouping, sorting, or card fields.

- [ ] **Step 4: Run shelf and startup regressions**

Run: `python -m pytest tests/test_library_lazy_detail.py tests/test_shelf_service.py tests/test_shelf_cache.py tests/test_app_startup.py -q` for files that exist. Expected: PASS.

## Task 3: GUI Smoke and Final Validation

- [ ] **Step 1: Run shelf smoke and startup smoke**

Set `QT_QPA_PLATFORM=offscreen` and run existing `gui/_smoke_library.py`, `gui/_smoke_app.py`, and `gui/_smoke_reader.py`. The library smoke must not require live network detail requests.

- [ ] **Step 2: Run relevant full regression**

Run: `python -m pytest tests/test_library_lazy_detail.py tests/test_shelf_service.py tests/test_shelf_cache.py tests/test_reading_progress_shelf.py tests/test_app_startup.py tests/test_public_novel_sources.py tests/test_chapter_pagination.py -q` for existing files.

- [ ] **Step 3: Compile and inspect diff**

Run `python -m compileall -q framework gui tests` and `git diff --check`. Inspect `git status --short`, `git diff --stat`, and confirm no changes to `app_config.json`, source JSON files, download-order files, or reader image files.

- [ ] **Step 4: Record handoff**

Write `docs/superpowers/reports/2026-10-06-shelf-card-lazy-detail-report.md` with exact test results, known network limitations, and the fact that no git commit was created. Append the decision and verification to `SESSION.md` without replacing existing entries.
