# Task C4 Final Validation Report

## Status
PASS with known Qt teardown timeout.

## Exact commands and results

- `python -m pytest tests/test_adult_filter.py tests/test_reader_prefetch_config.py tests/test_source_editor_save.py -q -p no:randomly` — PASS, 66 passed in 2.78s.
- `python -m pytest tests/test_folder_lock.py -q -p no:randomly` — assertions completed (`16 passed` shown), but the process exceeded the 120000 ms command timeout during Qt teardown.
- `python -m pytest tests/test_library_import_export.py -q -p no:randomly` — PASS, 12 passed in 0.83s.
- `python -m pytest tests/test_library_clear_favorite.py -q -p no:randomly` — PASS, 14 passed in 1.58s.
- `python -m pytest tests/test_cache_service.py tests/test_content_detail.py tests/test_content_related.py tests/test_reader_scroll_prefetch.py tests/test_reader_detail_cache.py tests/test_shelf_cache_repository.py tests/test_shelf_offline_cache.py tests/test_shelf_precache.py tests/test_shelf_service.py -q -p no:randomly` — PASS, 92 passed in 6.91s.
- `python -m pytest tests/test_discover_preload_depth.py tests/test_discover_session.py tests/test_discover_session_persist.py tests/test_discover_restore_no_request.py tests/test_discover_column_stretch.py tests/test_discover_scroll_anchor.py -q -p no:randomly` — PASS, 32 passed in 3.70s.
- `python -m pytest tests/test_discover_preload_depth.py tests/test_discover_session.py tests/test_discover_session_persist.py tests/test_search_page_multisource.py tests/test_card_layout_qss.py -q -p no:randomly` — PASS, 32 passed in 2.77s; emitted known `HoverTitle._marquee_on` teardown warnings.
- `python -m compileall -q framework gui tests` — PASS.
- `git diff --check` — PASS; only existing LF/CRLF normalization warnings.

## Coverage

- C1-C3: discover settings injection, preload depth, preload concurrency, session persistence, restore, and related discover regressions.
- A1-A10: adult/filter, folder lock/gate/delete, import/export, clear/favorite, cache/content, reader prefetch/content/detail cache, shelf cache/offline/precache/service, source editor, and settings coverage.
- B1-B11: reader/discover/shelf settings, reader prefetch, shelf offline/cache/precache, discover session/depth/concurrency, detail cache, source editor, and bounded A-regressions.

## Files

- Added: `task-c4-report.md`
- No production or test fixes were required; no unrelated refactors, commit, or push.

## Concerns

- `tests/test_folder_lock.py` reaches 16 passing tests but the Qt process exceeds the 120-second command bound during teardown; this is the known baseline Qt teardown hang documented by B11.
- Existing `HoverTitle.hideEvent()` warnings for missing `_marquee_on` remain during teardown; tests pass.
- `tests/test_settings_manager.py` and `tests/test_settings_page.py` are absent from this worktree; settings behavior was covered through `test_reader_prefetch_config.py`, `test_adult_filter.py`, and the existing B reports.
