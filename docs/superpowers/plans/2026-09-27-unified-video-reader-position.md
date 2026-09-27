# 通用视频播放与精准阅读位置记忆 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 优化所有视频源共享的播放/代理/缓存代码，并让小说与漫画在重启、改字号、改窗口和懒加载后恢复到稳定内容位置。

**Architecture:** 视频侧保持源配置只负责生产媒体地址，统一在 `MediaProxy`、缓存和外部播放器层做测量、去重、流式转发和有界异步。阅读侧在现有章节级进度上增加可选内容锚点：小说按正文文本，漫画按章节内图片身份和图片内偏移；比例和页码只作为兼容回退。

**Tech Stack:** Python 3.10+, PySide6, requests/urllib3, ThreadPoolExecutor, pytest, JSON 持久化。

## Global Constraints

- 不逐个修改 `sources/*.json` 来配置视频加速。
- 不主动批量读取每个源的 m3u8、分片或测速地址。
- 不默认开启无界多线程预取、不轮换代理出口、不增加激进重试。
- 不改变视频源解析、章节排序、VLC 全集播放列表和源选择契约。
- 不记录视频播放进度；视频位置按现有需求忽略。
- 保持已有 `chapter_url`、`position`、`page` 记录兼容。
- 不提交 Git，除非用户另行明确要求。
- 每个任务完成后运行对应专项测试；最终运行 lint/typecheck/compile 和稳定全量测试。

---

### Task 1: 建立公共视频性能基线与诊断契约

**Files:**
- Modify: `framework/media_proxy.py`
- Modify: `framework/external_player.py`
- Test: `tests/test_media_proxy_force.py`
- Test: `tests/test_media_proxy_stream_tuning.py`
- Test: `tests/test_external_player_hls.py`

**Interfaces:**
- Produces a bounded, thread-safe diagnostic record for existing requests without issuing extra upstream requests.
- Keeps existing `MediaProxy.build_url`, stream routes, and `external_player.open_with_player` signatures backward compatible.

- [ ] **Step 1: Write failing tests**

Add tests that invoke existing fake upstreams and assert diagnostics distinguish `manifest`, `segment`, `key`, `media`, and `range`; assert cache hit/miss and upstream wait are present where observable; assert no extra upstream call is caused by diagnostics.

```python
def test_diagnostics_are_bounded_and_do_not_probe_upstream(proxy, upstream):
    before = upstream.request_count
    response = request_manifest(proxy)
    assert response.status_code == 200
    assert upstream.request_count == before + 1
    records = proxy.diagnostics()
    assert len(records) <= 128
    assert records[-1]["request_kind"] == "manifest"
```

- [ ] **Step 2: Run tests to verify the new assertions fail**

Run:

```powershell
python -m pytest tests/test_media_proxy_force.py tests/test_media_proxy_stream_tuning.py -q
```

Expected: the new diagnostic field assertions fail while existing forwarding tests remain informative.

- [ ] **Step 3: Implement the smallest shared instrumentation**

Add a stable internal record factory and update the existing request lifecycle at request start, first non-empty upstream chunk, response completion, cache decision, and failure. Store only bounded metadata: protocol, request kind, host, route, status, elapsed, first-byte time, throughput, cache state, wait time, and failure category. Never store full URLs, authorization, cookies, tokens, or body bytes.

Use the already existing diagnostic storage and locking patterns in `media_proxy.py`; do not add a second global telemetry system.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
python -m pytest tests/test_media_proxy_force.py tests/test_media_proxy_stream_tuning.py tests/test_external_player_hls.py -q
```

Expected: PASS, with no increase in upstream request counts beyond the explicitly requested media requests.

---

### Task 2: Harden shared media cache, single-flight, and cancellation

**Files:**
- Modify: `framework/media_proxy.py`
- Modify: `framework/media_cache.py`
- Test: `tests/test_media_cache.py`
- Test: `tests/test_media_proxy_stream_tuning.py`
- Test: `tests/test_media_proxy_force.py`

**Interfaces:**
- Existing cache APIs remain callable by `MediaProxy` and tests.
- Introduce an internal per-resource in-flight state keyed by canonical media URL, with completion/error signaling and bounded cleanup.

- [ ] **Step 1: Write failing concurrency tests**

Add tests with a fake upstream that blocks the first request. Start two requests for the same complete media resource and assert only one upstream request occurs, both clients receive the same bytes, and a failed first request does not leave a permanent in-flight entry.

```python
def test_same_resource_requests_share_one_upstream_fetch(proxy, upstream):
    first, second = start_concurrent_requests(proxy, media_url)
    upstream.release()
    assert first.body == second.body == PAYLOAD
    assert upstream.request_count == 1
```

Add a Range test proving a partial response never becomes a complete cache entry.

- [ ] **Step 2: Run the new tests and confirm failure**

Run:

```powershell
python -m pytest tests/test_media_cache.py tests/test_media_proxy_stream_tuning.py -q
```

Expected: duplicate upstream calls or incorrect cache state before implementation.

- [ ] **Step 3: Implement single-flight and cache correctness**

Use a per-key condition/event or equivalent existing synchronization primitive. The first request owns the upstream fetch; followers wait only for that key, then read the completed cache or receive the same failure. Ensure cleanup runs on success, exception, client disconnect, proxy stop, and timeout.

Keep Range responses out of the complete-object cache unless the requested range is a full zero-offset response with a verified complete length. Preserve 206/416 headers and never serve a partial file as 200.

- [ ] **Step 4: Verify cache and cancellation behavior**

Run:

```powershell
python -m pytest tests/test_media_cache.py tests/test_media_proxy_stream_tuning.py tests/test_media_proxy_force.py -q
```

Expected: PASS; no stale `.part`, in-flight, or task entries after failure/stop.

---

### Task 3: Make bounded asynchronous prefetch a protocol-level fallback

**Files:**
- Modify: `framework/media_proxy.py`
- Modify: `framework/settings_manager.py` only if current defaults cannot supply runtime limits
- Test: `tests/test_hls_throughput_tuning.py`
- Test: `tests/test_media_proxy_series.py`

**Interfaces:**
- Keep `hls_prefetch.enabled` default false.
- Keep existing `_maybe_prefetch`, `_prefetch_drain`, and `stop` compatibility where present.
- Add cancellation and circuit-breaker state scoped to the current proxy/session, not to a source ID.

- [ ] **Step 1: Write failing tests**

Cover: cancellation on `stop`, one task per canonical URL, player request winning over a queued prefetch, hard depth/worker caps, and disabling after 403/429 or repeated connection failures.

```python
def test_prefetch_circuit_breaker_stops_after_rate_limit(proxy, upstream):
    proxy.enable_prefetch_for_test(depth=4, workers=2)
    proxy.schedule_prefetch(order)
    proxy.drain_prefetch()
    assert proxy.prefetch_enabled() is False
    assert upstream.request_count <= 2
```

- [ ] **Step 2: Run tests to confirm failure**

Run:

```powershell
python -m pytest tests/test_hls_throughput_tuning.py tests/test_media_proxy_series.py -q
```

- [ ] **Step 3: Implement only bounded common behavior**

Reuse the existing `ThreadPoolExecutor`, direct-session behavior, shared path lock, and tee cache. Add a session-level cancellation event, hard caps, per-task timeout handling, and a small failure circuit breaker. Keep hooks outside the path lock. Do not add source-specific branches or automatic manifest probing.

- [ ] **Step 4: Run focused regression tests**

Run:

```powershell
python -m pytest tests/test_hls_throughput_tuning.py tests/test_media_proxy_series.py tests/test_media_cache.py -q
```

Expected: PASS and default-disabled mode performs zero extra upstream requests.

---

### Task 4: Extend reading progress with versioned content locations

**Files:**
- Modify: `framework/reading_progress.py`
- Test: `tests/test_reading_progress_shelf.py`
- Test: `tests/test_read_restore_cross_book.py`

**Interfaces:**
- Extend `ReadingProgress.save(..., location: dict | None = None)` without breaking current callers.
- `resume()` returns a defensive copy including optional `location`.
- Add atomic JSON persistence using a temporary sibling file and replace, with the existing failure-safe behavior.

- [ ] **Step 1: Write failing persistence tests**

```python
def test_location_round_trips_and_old_record_still_loads(tmp_path):
    rp = ReadingProgress(tmp_path / "progress.json")
    location = {"version": 1, "kind": "novel-anchor", "anchor_text": "目标段落"}
    rp.save("demo", BOOK, "novel", CH2, "第二章", location=location)
    assert ReadingProgress(rp.path).resume(BOOK)["location"] == location
```

Add a fixture containing only legacy `position` and `page` and assert it resumes unchanged.

- [ ] **Step 2: Run tests and confirm failure**

Run:

```powershell
python -m pytest tests/test_reading_progress_shelf.py tests/test_read_restore_cross_book.py -q
```

- [ ] **Step 3: Implement versioned optional location and atomic writes**

Validate location is a JSON object, copy it before storing, preserve old fields, and write via a temporary file in the same directory followed by `os.replace`. On write failure, retain the previous valid file and do not break the reader.

- [ ] **Step 4: Verify compatibility**

Run the same two test files and assert all existing chapter/position tests pass.

---

### Task 5: Implement novel text-anchor capture and restore

**Files:**
- Modify: `gui/pages/reader/novel_view.py`
- Modify: `gui/pages/reader_page.py`
- Test: `tests/test_reading_progress_shelf.py`
- Test: `tests/test_read_restore_cross_book.py`
- Test: `tests/test_reader_zzz.py`

**Interfaces:**
- Add internal helpers with stable signatures: `_normalize_anchor_text`, `_build_location_snapshot`, `_resolve_location_scroll`, and `_restore_location_if_book`.
- Extend `position_changed` payload handling in `ReaderPage` without removing legacy tuple support.

- [ ] **Step 1: Write failing Qt tests**

Test that a saved paragraph anchor restores after a changed viewport/font layout; test that a changed chapter body finds the normalized anchor text; test that a missing anchor falls back to legacy ratio/page; test that switching books invalidates pending restore.

```python
def test_novel_restores_text_anchor_after_layout_change(app, tmp_path):
    location = make_novel_location("稳定段落", offset=6)
    view.load(source, detail, restore_location=location)
    resize_and_process_events(view, width=1100, height=700)
    assert visible_text_at_scroll(view).startswith("稳定段落")
```

- [ ] **Step 2: Run the new tests to confirm failure**

Run:

```powershell
python -m pytest tests/test_reading_progress_shelf.py tests/test_read_restore_cross_book.py tests/test_reader_zzz.py -q
```

- [ ] **Step 3: Implement anchor capture and restoration**

Normalize whitespace and exclude the generated chapter heading. Capture a bounded text window around the visible cursor/scroll-derived character position, its normalized hash, character offset, intra-anchor offset, content hash, current ratio, page, and layout metadata. On restore, require chapter URL match, prefer exact content hash plus offset, then unique normalized anchor search, then page, then ratio. Apply the resulting scroll only after layout completion and guard delayed callbacks by book identity and a bounded retry counter.

Keep the existing 1.5-second write throttle and force a final snapshot through `ReaderPage.flush_progress`.

- [ ] **Step 4: Run focused Qt tests**

Run:

```powershell
python -m pytest tests/test_reading_progress_shelf.py tests/test_read_restore_cross_book.py tests/test_reader_zzz.py -q
```

Expected: PASS; existing chapter navigation, pager mode, and cross-book guards remain green.

---

### Task 6: Implement comic image-anchor capture and restore

**Files:**
- Modify: `gui/pages/reader/comic_view.py`
- Modify: `gui/pages/reader_page.py`
- Test: `tests/test_reading_progress_shelf.py`
- Test: `tests/test_read_restore_cross_book.py`
- Test: `tests/test_comic_scroll_anchor.py`
- Test: `tests/test_comic_pages_cache.py`

**Interfaces:**
- Add internal helpers `_visible_image_anchor`, `_build_location_snapshot`, `_resolve_image_anchor`, and `_restore_location_with_retry` while preserving current position-only callers.
- Use the existing comic generation and scroll epoch guards.

- [ ] **Step 1: Write failing Qt tests**

Test restoration to image index plus image URL/hash after image insertion, restoration of image-internal ratio after zoom/window change, delayed lazy layout retry, and fallback to legacy scroll ratio.

```python
def test_comic_restores_same_image_after_inserted_page(app):
    view.load(source, detail, restore_location=location_for_image("page-12", 0.37))
    insert_image_before_target(view)
    finish_layout(view)
    assert current_anchor_image(view) == "page-12"
    assert image_internal_ratio(view) == pytest.approx(0.37, abs=0.03)
```

- [ ] **Step 2: Run tests to confirm failure**

Run:

```powershell
python -m pytest tests/test_reading_progress_shelf.py tests/test_read_restore_cross_book.py tests/test_comic_scroll_anchor.py tests/test_comic_pages_cache.py -q
```

- [ ] **Step 3: Implement image anchor capture and restore**

Determine the topmost visible rendered image and its URL/key, compute the fraction of the viewport position within that image, and store image index as a fast path. On restore, match URL/key first and index second, wait until the target and preceding layout are present, scroll to target top plus saved internal fraction, and stop after bounded retries. Keep all delayed callbacks guarded by book generation and scroll epoch.

- [ ] **Step 4: Run focused comic tests**

Run the same four test files and verify no cross-book or lazy-loading regressions.

---

### Task 7: Integrate persistence, flush, and final quality gates

**Files:**
- Modify: `gui/pages/reader_page.py`
- Modify: `gui/app.py` only if the existing tab/quit hooks do not flush the richer snapshot
- Test: `tests/test_reading_progress_shelf.py`
- Test: `tests/test_read_restore_cross_book.py`
- Test: relevant video proxy/player tests

**Interfaces:**
- `ReaderPage.flush_progress()` remains the single public flush entry point.
- App tab switching and application shutdown continue calling it.

- [ ] **Step 1: Add end-to-end persistence tests**

Exercise: open book, scroll/change font/resize, flush, construct a new reader with the same progress file, and assert novel and comic location objects survive and restore. Verify video views do not write a playback position.

- [ ] **Step 2: Run the complete relevant regression set**

Run:

```powershell
python -m pytest tests/test_reading_progress_shelf.py tests/test_read_restore_cross_book.py tests/test_reader_zzz.py tests/test_comic_scroll_anchor.py tests/test_comic_pages_cache.py tests/test_media_cache.py tests/test_media_proxy_force.py tests/test_media_proxy_stream_tuning.py tests/test_hls_throughput_tuning.py tests/test_external_player_hls.py -q
```

- [ ] **Step 3: Run repository quality gates**

Detect and run the repository's configured lint/typecheck commands. At minimum run:

```powershell
python -m compileall framework gui tests
python -m pytest --ignore=tests/test_comic_scroll_anchor.py -q
```

Also run `git diff --check` and inspect `git diff --stat`, `git diff`, and `git status --short`; do not claim completion if unrelated existing worktree changes are mixed into the verification result.

- [ ] **Step 4: Record verification and known limits**

Update `SESSION.md` and `learning-log.md` only after implementation and verification, documenting measured public-chain improvements, anchor fallback behavior, and any pre-existing PySide6 process-level crashes. Do not commit unless explicitly requested.
