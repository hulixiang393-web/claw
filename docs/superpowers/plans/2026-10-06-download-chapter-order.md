# Local Download and Reader Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve chapter order in downloaded EPUBs, display online and local novel images, and make local EPUB directories jump to the selected chapter.

**Architecture:** Downloaded text/image payloads are stored in chapter-indexed runtime slots and ordered before EPUB construction. Online novel content gains a separate text-and-image extraction API while preserving the existing text-only `fetch_chapter()` contract. The local EPUB reader parses ordered text/image fragments, uses its existing image gallery for image-only chapters, and fixes directory item indices.

**Tech Stack:** Python 3.10, PySide6, lxml, pytest, EbookLib, existing `CoverLoader` and `FastEpub`.

## Global Constraints

- Base all changes on `385de40 perf(ui): reduce discovery preload and fanqie wait`.
- Restore only `bookstack`, `shuyous`, and `mopbook` plus the chapter-pagination support they require; do not restore other post-baseline commits.
- Do not change download-history JSON to persist `epub_chapters` or image bytes.
- Preserve `Content.fetch_chapter()` as a plain-text API.
- Preserve existing `build_novel_epub()` and `build_comic_epub()` input contracts.
- Image failures must not discard readable text or other successfully loaded images.
- All asynchronous image/chapter callbacks must reject stale chapter generations.
- Run commands with `C:\Users\alonely\AppData\Local\Programs\Python\Python310\python.exe`.

---

## File Map

- Modify `framework/downloader.py`: index-addressed payload slots and ordered EPUB finalization.
- Modify `framework/download_queue.py`: record successful comic batch indices and avoid repeating completed chapters after resume.
- Create `tests/test_download_chapter_order.py`: reverse completion, retry overwrite, comic batch and finalizer ordering tests.
- Modify `framework/content.py`: add image-aware chapter page extraction while preserving text-only API.
- Modify `framework/settings_manager.py` and `gui/pages/settings_page.py`: default-on image setting control and persistence.
- Modify `gui/pages/reader_page.py` and `gui/pages/reader/novel_view.py`: settings propagation, async image loading, generation safety and re-render/toggle behavior.
- Create `tests/test_online_novel_images.py`: extraction, setting and NovelView async callback tests.
- Modify `framework/epub_reader.py`: parse ordered text/image fragments while retaining the existing `split_single_document()` default return shape.
- Modify `gui/pages/reader/epub_view.py`: preserve mixed chapter parts, render text+image order, and set EPUB directory row indices.
- Create `tests/test_epub_mixed_content.py`: fragment order, mixed rendering and local directory navigation tests.
- Update `SESSION.md` and add `docs/superpowers/reports/2026-10-06-local-reader-download-report.md` after validation.

## Task 1: Store Downloaded Chapters by Original Index

**Interfaces:**
- `Downloader._store_epub_chapter(task, index: int, title: str, payload) -> None` stores `(title, payload)` at the original index, extending slots with `None` as needed.
- `Downloader._ordered_epub_chapters(task) -> list[tuple[str, object]]` returns successful selected materials by original index.
- Builders still receive `(title, text)` novel tuples or `(title, image_bytes)` comic tuples.

**Files:** `framework/downloader.py`, `framework/download_queue.py`, `tests/test_download_chapter_order.py`.

- [ ] **Step 1: Add offline downloader test helpers and RED test**

Create `_FakeContent.fetch_chapter()` using a URL-to-text dictionary, `_FakeContent.fetch_comic_pages()` returning deterministic `data:` images, `_FakeHttp.get_bytes()` returning fixed bytes, `_Settings.get()` returning its default, plus simple task/chapter helpers. Add:

```python
def test_novel_materials_are_ordered_by_original_index(monkeypatch):
    task = _task("novel", ["c1", "c2", "c3"])
    downloader = _downloader(monkeypatch, {"c1": "one", "c2": "two", "c3": "three"})
    downloader._download_novel(None, task, task.chapters[2], 2)
    downloader._download_novel(None, task, task.chapters[0], 0)
    downloader._download_novel(None, task, task.chapters[1], 1)
    task.done_chapters = [2, 0, 1]

    assert downloader._ordered_epub_chapters(task) == [
        ("第一章", "one"), ("第二章", "two"), ("第三章", "three")
    ]
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `python -m pytest tests/test_download_chapter_order.py::test_novel_materials_are_ordered_by_original_index -q`
Expected: FAIL because indexed helper does not exist and current storage appends in completion order.

- [ ] **Step 3: Implement indexed slots and novel/comic writes**

In `Downloader`, add `_store_epub_chapter()`. Extend `task.epub_chapters` with `None` to `index + 1`, then assign `(title, payload)` at that index. Replace `_download_novel()` and `_download_comic()` append operations with this helper. Repeated writes to the same index overwrite the previous result.

Add `_ordered_epub_chapters()`: enumerate slots in ascending index, skip `None`, skip unselected indices, and when `done_chapters` has entries include only those marked complete. For legacy test/task objects whose materials are an already-packed list of tuples but have no completion indices, preserve tuple order.

- [ ] **Step 4: Add and run RED/GREEN retry overwrite test**

Write a test that stores payload A then payload B at index 1 and asserts index 1 contains only B, with no duplicate tuple. Run before implementation to observe failure; run again after to confirm pass.

- [ ] **Step 5: Add comic batch out-of-order test**

Call `download_comic_batch()` for chapters with original indices `[2, 0, 1]`, assert return byte counts remain keyed by those indices, and assert material slots are `[chapter0, chapter1, chapter2]`. Run and confirm RED before replacing batch append, then GREEN.

- [ ] **Step 6: Track comic-batch completion and resume correctly**

In `DownloadQueue._download_comic_batch()`, successful result indices are appended once to `task.done_chapters` under `_lock`. Build `pending` by selecting indices not in `done_chapters`, respecting `selected`. Do not add failed or skipped-batch entries to `done_chapters`.

- [ ] **Step 7: Verify download ordering regressions**

Run: `python -m pytest tests/test_download_chapter_order.py tests/test_download_retry_restart.py tests/test_downloader_referer.py tests/test_download_history.py -q`
Expected: PASS, and the history tests continue to prove EPUB bytes are not serialized.

## Task 2: Finalize EPUBs in Chapter Order

**Interfaces:**
- `Downloader.finalize_epub(task) -> Path` remains unchanged externally and passes ordered chapter tuples to existing builder functions.

**Files:** `framework/downloader.py`, `tests/test_download_chapter_order.py`.

- [ ] **Step 1: Add RED test for builder input ordering**

Monkeypatch `framework.epub_builder.build_novel_epub` to capture its `chapters` argument; construct a task with materials stored in indexed slots in completion order `2, 0, 1`, set `done_chapters=[2, 0, 1]`, and call `finalize_epub()`. Assert builder receives chapter titles in order `第一章, 第二章, 第三章` and a `Path` is returned.

- [ ] **Step 2: Run test and confirm RED**

Run: `python -m pytest tests/test_download_chapter_order.py::test_finalize_epub_passes_chapters_in_source_order -q`
Expected: FAIL because finalizer currently forwards `task.epub_chapters` directly.

- [ ] **Step 3: Finalize only ordered completed materials**

In `finalize_epub()`, compute `chapters = self._ordered_epub_chapters(task)`, reject an empty ordered result with the existing `RuntimeError("无章节内容，无法合成 epub")`, and pass `chapters` to the selected builder. Do not change builder or destination path behavior.

- [ ] **Step 4: Verify ordering and retry tests**

Run: `python -m pytest tests/test_download_chapter_order.py tests/test_download_retry_restart.py tests/test_downloader_referer.py tests/test_download_history.py -q`
Expected: PASS.

## Task 3: Extract and Display Online Novel Images

**Interfaces:**
- `Content.fetch_chapter_with_images(source, url) -> tuple[str, list[str]]`; old `fetch_chapter(source, url, use_cache=True) -> str` remains unchanged.
- `_fetch_chapter_page(source, url, pag_enabled=True, include_images=False)` keeps its existing two-value return when `include_images=False`, and returns `(text, next_url, image_urls)` when true.
- `NovelView.set_show_images(enabled: bool) -> None` controls image requests/rendering.

**Files:** `framework/content.py`, `framework/settings_manager.py`, `gui/pages/settings_page.py`, `gui/pages/reader_page.py`, `gui/pages/reader/novel_view.py`, `tests/test_online_novel_images.py`.

- [ ] **Step 1: Write RED extraction contract tests**

Use a fixture HTML body with a duplicate image, relative `src`, lazy `data-src`, and an advertisement image outside the configured body. Configure a lightweight fake source with a CSS body selector and a fake HTTP client returning the fixture. Assert `fetch_chapter_with_images()` returns text plus de-duplicated absolute body image URLs, and assert `fetch_chapter()` still returns `str` without changing its public shape.

- [ ] **Step 2: Run extraction test and confirm RED**

Run: `python -m pytest tests/test_online_novel_images.py::test_fetch_chapter_with_images_extracts_body_urls -q`
Expected: FAIL because the image-aware API and extractor are missing.

- [ ] **Step 3: Implement body-scoped image extraction**

Add a helper that normalizes CSS/XPath/fallback selector config, queries matching body roots via `Parser._query`, traverses descendant `img` nodes in document order, checks `src`, `data-src`, `data-original`, `data-lazy-src` in that order, skips empty/data URIs/advertisement URLs, resolves against current page URL with `urljoin`, and de-duplicates while preserving order. Extraction exceptions return `[]`.

Add `include_images=False` to `_fetch_chapter_page`; keep its legacy `(text, next_url)` result exactly when false. When true, return the image URLs as a third result. Playwright-rendered text returns an empty image URL list without changing its text behavior.

Add `fetch_chapter_with_images()` that follows the same pagination max and same-chapter checks as `fetch_chapter`, joins page text with newline, and combines page image URLs in encounter order. It must not write image metadata into the existing body text cache.

- [ ] **Step 4: Run extraction tests and confirm GREEN**

Run: `python -m pytest tests/test_online_novel_images.py::test_fetch_chapter_with_images_extracts_body_urls -q`
Expected: PASS, and the legacy `fetch_chapter()` contract test passes.

- [ ] **Step 5: Add RED tests for setting persistence and task image extraction**

Assert `DEFAULTS["reader"]["show_images"] is True`; instantiate `SettingsPage`, verify checkbox default, toggle false, call `_on_apply()`, and assert the settings store has `False`. Directly run `_LoadChapterTask` with a fake content whose `fetch_chapter_with_images()` returns a text and URL list; assert the chapter keeps the URL list and emitted body text remains unchanged.

- [ ] **Step 6: Implement the settings and task wiring**

Add `show_images: True` under reader defaults. Add a checkbox in the Reader settings tab, load/save it under `reader.show_images`. Pass the setting from `ReaderPage` to `NovelView` on initialization and after settings are applied. Make `_LoadChapterTask` accept `show_images=False`; call `fetch_chapter_with_images()` only when enabled and the method exists, otherwise call legacy `fetch_chapter()`. Store URLs in a chapter-local `_cached_image_urls` attribute and mark whether image extraction has been attempted.

- [ ] **Step 7: Add RED async rendering and stale-callback tests**

Construct a `NovelView` with fake chapter image URLs and monkeypatch `CoverLoader.instance()` to a fake loader that captures URL, callback, and options. Assert enabled scroll mode issues loads with `referer=chapter.url`, `cache=False`, `persist=False`; invoke a success callback and assert a pixmap label is added. Change chapter generation before invoking an old callback and assert it adds nothing. Disable images and assert callback results cannot add labels.

- [ ] **Step 8: Implement async rendering and toggle behavior**

Use `CoverLoader.instance().load(url, callback, referer=chapter.url, cache=False, persist=False, source_id=source.source_id, source=source)`. Preserve the body text QLabel. Add loaded pixmaps in order to the scroll content layout, scaled proportionally to the viewport width. Increment an image generation on chapter changes and setting changes. On enabling images for a chapter that was previously loaded text-only, invalidate only that chapter's cached-text marker and reload through the image-aware task. On disabling, remove image labels but preserve text and cached URL metadata.

- [ ] **Step 9: Run online image and reader regressions**

Run: `python -m pytest tests/test_online_novel_images.py tests/test_reader_zzz.py tests/test_reader_scroll_prefetch.py tests/test_reading_progress_shelf.py -q`
Expected: PASS.

## Task 4: Preserve Mixed Text and Images in Local EPUB Chapters

**Interfaces:**
- `split_single_document(html, include_parts=False)` preserves its current triple return by default. With `include_parts=True`, it returns `(title, text, img_srcs, content_parts)` for each chapter; `content_parts` is an ordered list of `("text", text)` and `("image", href)`.
- `_Chapter` gains `content_parts: list`, default empty; existing `title`, `src`, `text`, and `img_srcs` remain available.
- `_LoadSignals.done` becomes `Signal(object, object, object, object, object)` with `(idx, kind, text, imgs, parts)`. `kind` is `"novel"`, `"comic"`, or `"mixed"`; `imgs` remains the raw byte list for pure comic chapters; `parts` is an ordered list of `("text", text)` and `("image", raw_bytes)` for mixed chapters. `EpubView._on_chapter_loaded()` consumes all five values.

**Files:** `framework/epub_reader.py`, `gui/pages/reader/epub_view.py`, `tests/test_epub_mixed_content.py`.

- [ ] **Step 1: Write RED test for split-document mixed order**

Call `split_single_document()` with one chapter containing a paragraph before an image and another paragraph after it. With `include_parts=True`, assert the parts are exactly text, image reference, text in source order. Call without the flag and assert the legacy three-value shape remains.

- [ ] **Step 2: Run test and confirm RED**

Run: `python -m pytest tests/test_epub_mixed_content.py::test_split_single_document_preserves_mixed_order -q`
Expected: FAIL because `include_parts` and content parts are not present.

- [ ] **Step 3: Implement ordered XHTML fragment extraction**

Add a parser helper in `framework/epub_reader.py` that walks an lxml XHTML tree in DOM order. Emit non-empty text runs; emit paragraph/newline boundaries as text separators; emit image `src` references as image parts. Normalize adjacent text parts. For image-only fragments retain `img_srcs` and existing behavior. Extend `split_single_document()` with `include_parts=False`; default returns the old triples, true returns a fourth part list.

- [ ] **Step 4: Run parser tests and confirm GREEN**

Run: `python -m pytest tests/test_epub_mixed_content.py::test_split_single_document_preserves_mixed_order -q`
Expected: PASS.

- [ ] **Step 5: Add RED test for normal XHTML mixed chapter load data**

Use a fake EPUB exposing one XHTML item and two image resources. Run `_LoadChapterTask` against markup containing text, image, text. Assert the chapter data keeps the ordered text/image/text parts and image bytes are read via `_read_bytes()`; an image-only XHTML remains classified `comic`.

- [ ] **Step 6: Implement task-side mixed chapter parsing**

In `_Chapter`, add `content_parts`. In `_build_chapters()`, request split parts for large merged documents and place parts on created chapters. In `_LoadChapterTask`, parse normal XHTML to ordered parts, resolve image references to bytes under `_READ_LOCK`, and classify `novel`, `comic`, or `mixed` based on actual part kinds. Keep existing `text` and `img_srcs` for their existing pure-content paths.

- [ ] **Step 7: Add RED mixed renderer test**

Create an EpubView with a mixed chapter whose parts alternate text, image bytes, text. Monkeypatch the background decoder to immediately return a deterministic QImage through the normal callback path. Assert the mixed scroll layout item order follows the parts and image labels fit within viewport width.

- [ ] **Step 8: Implement ordered mixed renderer**

Add a mixed-content container/layout in EpubView. Insert selectable QLabel text blocks and image placeholder labels in source order. Decode image bytes in the existing Qt thread pool using `_ComicDecodeTask` with a separate mixed-generation callback; scale decoded images to the current viewport width, preserving aspect ratio. Increment generation when changing chapter or rendering a new document so late results are ignored. Keep pure-text labels and pure-comic gallery paths unchanged.

- [ ] **Step 9: Run EPUB mixed-content and reader smoke tests**

Run: `python -m pytest tests/test_epub_mixed_content.py tests/test_reader_zzz.py -q`
Run: `python -X utf8 gui/_smoke_reader.py` with `QT_QPA_PLATFORM=offscreen`.
Expected: PASS; pure text/comic behavior remains unchanged.

## Task 5: Fix Local EPUB Directory Navigation

**Interfaces:**
- `_populate_toc()` sets each directory item's `Qt.UserRole` to its zero-based spine/chapter index.
- `_on_toc_clicked(item)` validates index and invokes `_load_chapter(index)`.

**Files:** `gui/pages/reader/epub_view.py`, `tests/test_epub_mixed_content.py`.

- [ ] **Step 1: Write RED directory click tests**

For both novel and comic chapter fixtures, call `_populate_toc()`, assert row `i` has `item.data(Qt.UserRole) == i`, replace `_load_chapter` with a list append, emit a click for row 1, and assert index 1 was selected. Add an invalid/missing role item and assert it causes no exception or load.

- [ ] **Step 2: Run test and confirm RED**

Run: `python -m pytest tests/test_epub_mixed_content.py::test_epub_toc_click_selects_correct_chapter -q`
Expected: FAIL because `_populate_toc()` does not set `Qt.UserRole`.

- [ ] **Step 3: Store and validate local chapter indices**

When creating each `QListWidgetItem`, call `item.setData(Qt.UserRole, index)`. In `_on_toc_clicked`, parse role data as an integer, return if missing or outside `0 <= index < len(self._chapters)`, and call `_load_chapter(index)` otherwise. Keep `_load_chapter()` responsible for current-row/progress update.

- [ ] **Step 4: Verify local navigation tests pass**

Run: `python -m pytest tests/test_epub_mixed_content.py -q`
Expected: PASS for novel, comic, mixed content and invalid indices.

## Task 6: Full Regression, Report, and Handoff

**Files:** `SESSION.md`, create `docs/superpowers/reports/2026-10-06-local-reader-download-report.md`.

- [ ] **Step 1: Run combined targeted tests**

Run: `python -m pytest tests/test_download_chapter_order.py tests/test_download_retry_restart.py tests/test_downloader_referer.py tests/test_download_history.py tests/test_online_novel_images.py tests/test_epub_mixed_content.py tests/test_reader_zzz.py tests/test_reader_scroll_prefetch.py tests/test_reading_progress_shelf.py tests/test_app_startup.py -q`
Expected: PASS.

- [ ] **Step 2: Run GUI smokes**

Set `QT_QPA_PLATFORM=offscreen`, then run `python -X utf8 gui/_smoke_reader.py` and the project's settings/app smoke scripts if present. Expected: PASS without live source requirements.

- [ ] **Step 3: Run compile and whitespace checks**

Run: `python -m compileall -q framework gui tests` and `git diff --check`.
Expected: both succeed; no changes to source configuration or startup optimization behavior.

- [ ] **Step 4: Review diff against the approved specification**

Inspect `git status --short`, `git diff --stat`, and `git diff`. Confirm only download ordering, reader/settings, local EPUB parsing/rendering/navigation, regression tests, `SESSION.md`, and the report are changed. Confirm no source JSON, startup scripts, or unrelated cast files changed.

- [ ] **Step 5: Record final validation**

Write the report with exact test commands/results and known limitations. Append progress and decisions to `SESSION.md` without replacing prior entries. Do not push remote. Commit only if the user requests a commit.
