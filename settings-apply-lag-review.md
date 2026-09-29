# Settings Apply Lag Review

Date: 2026-09-29

## Verdict

**Changes requested**

The production fix matches the reported root cause and is narrowly scoped, but the regression test is not strong enough to verify the full concurrency/reentrancy requirement: it proves deferral and one-shot coalescing, but not that the deferred callback observes the latest persisted setting or that reentrant applies remain bounded.

## Findings

### Minor — test does not prove latest-settings-wins semantics

**Location:** `tests/test_adult_filter.py:674-713`

`test_settings_apply_defers_visibility_refresh_and_coalesces` calls `_on_settings_applied()` twice with a `_Settings` stub whose `get()` always returns the default, and replaces `_sync_source_visibility` with a recorder. Consequently, the test can pass even if the implementation captured the first setting value, applied stale state, or never read the setting from the deferred callback. It verifies only that no callback runs before `processEvents()` and that exactly one callback is queued.

**Suggestion:** Make the stub mutable, invoke the first apply with one visibility value, change the stub to the second value, invoke the second apply, then process events and assert that the real `_sync_source_visibility` path applies the second value exactly once. This should assert the resulting `SourceManager.is_adult_visible()` value or record the value read by the sync helper.

### Minor — reentrancy behavior is not covered

**Location:** `gui/app.py:1402-1414`; `tests/test_adult_filter.py:674-713`

The callback clears `_source_visibility_sync_scheduled` before calling `_sync_source_visibility()`. That is a reasonable ordering because EventBus handlers may synchronously run during the visibility change, but the test does not exercise an event handler that calls `_on_settings_applied()` again. Without that case, the review cannot verify that a reentrant apply schedules at most one follow-up callback and does not create an unbounded timer chain.

**Suggestion:** Add a test where the visibility/event path triggers another apply once, then drain the event loop and assert bounded callback count and the final setting. Keep the assertion finite and explicit rather than relying only on completion.

## Verified points

- Settings persistence remains synchronous: `SettingsPage._on_apply()` writes all values, calls `s.save()`, and only then emits `settings_applied` (`gui/pages/settings_page.py:441-480`).
- Only source-visibility synchronization and its EventBus-driven page refresh/search work are deferred from `MainWindow._on_settings_applied()` (`gui/app.py:1402-1421`). Theme, reader style/font, and cache configuration remain on the synchronous path.
- Startup synchronization remains synchronous and occurs before page construction (`gui/app.py:347-350`), preserving the startup invariant tested at `tests/test_adult_filter.py:534-610`.
- `SourceManager` and `EventBus` behavior is not changed by this fix; the new helper delegates to `set_event_bus()` and `set_adult_visible()` without altering their direct semantics (`gui/app.py:84-91`).
- The scheduler guard coalesces repeated applies before the next event-loop turn and clears its state before executing the callback, so the normal path does not accumulate unbounded timers (`gui/app.py:1402-1414`).
- The focused regression suite passes: `python -m pytest tests/test_adult_filter.py -q` → `30 passed`.
- The requested production/test diff is limited to `gui/app.py` and `tests/test_adult_filter.py`; the worktree also contains unrelated pre-existing changes documented in the supplied report, which were not attributed to this fix.

## Summary

The fix is directionally correct and preserves synchronous persistence, startup ordering, direct source/event semantics, and unrelated settings behavior. Strengthen the regression test to prove latest-state application and one bounded reentrant apply before treating the change as fully verified.
