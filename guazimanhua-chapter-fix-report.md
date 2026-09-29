# Guazi Manga Chapter Fix Report

## Status

Corrected the previous source-specific exclusion fix. Start-reading navigation labels are now handled globally as first regular chapter entries, with same-URL merge behavior that preserves a real chapter title when available and retains navigation-only links as chapter 1. Prelude labels remain before regular chapters.

## Tests

- `python -m pytest tests/test_chapter_sort.py tests/test_guazimanhua.py -q` — 18 passed
- Chapter sort/content/Guazi/comic/reader regression selection — 114 passed
- `python -m compileall -q framework tests` — passed
- `git diff --check` — passed

## Files

- `framework/content.py`: removed exclusion handling; globally merges duplicate start-reading navigation URLs and preserves real titles.
- `framework/chapter_sort.py`: treats start-reading labels as chapter 1 and keeps preview/序章/楔子 entries before regular chapters.
- `sources/guazimanhua.json`: removed Guazi-only exclusion configuration.
- `tests/test_chapter_sort.py`: added navigation and prelude ordering coverage.
- `tests/test_guazimanhua.py`: replaced exclusion expectations with global same-URL merge, navigation-only fallback, and generic-source coverage.

## Concerns

- Existing unrelated worktree changes were left untouched.
- The requested regression selection passed; no full-repository test run was performed.
