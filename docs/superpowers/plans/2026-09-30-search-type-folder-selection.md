# Search Type and Folder Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Limit the search source menu and source selection to the selected content type, and let batch search results be added to a chosen shelf folder.

**Architecture:** Keep source filtering in `SearchPage` using `SourceManager.enabled_sources()` and each source's `content_type`. Keep folder choice in the existing App/SearchPage signal flow by giving `SearchPage` a folder-name provider backed by the App-owned `LibraryStore`, then extending the batch-add signal payload with the selected folder. The App handler passes the folder to the existing `LibraryStore.add` path. The default folder is the uncategorized folder (`""`), never the display-only `"全部"` value.

**Tech Stack:** Python 3, PySide6, existing `SearchPage`, `SourceManager`, `ShelfService`, and pytest conventions.

## Global Constraints

- Modify the actual project at `D:\code\claw`.
- Do not add dependencies.
- Do not use `qtbot`.
- `"全部"` is a display/filter label, not a writable folder name.
- Preserve existing source visibility filtering, search cancellation, result filtering, and shelf lock behavior.
- Run focused tests, `python -m compileall -q framework gui tests`, and `git diff --check`.

---

### Task 1: Type-Scoped Search Sources

**Files:**
- Modify: `gui/pages/search_page.py:275-297, 418-470`, source menu handlers near `_rebuild_sources_menu`
- Test: existing `tests/test_search_page_multisource.py` and `tests/test_search_page_source_menu.py`

**Interfaces:**
- Consumes: `self.type_combo.currentData()`, `self._manager.enabled_sources()`, `SourceConfig.content_type`.
- Produces: source menu rows and source selection state scoped to the current type; `_on_search()` submits only sources matching the selected type.

- [ ] **Step 1: Write failing tests**

Add coverage to the existing search tests for:

```python
def test_source_menu_only_contains_selected_type(app, manager):
    page = SearchPage(manager, search)
    page.type_combo.setCurrentIndex(page.type_combo.findData("comic"))
    page._rebuild_sources_menu()
    assert page._visible_source_ids() == {"comic-a", "comic-b"}
    assert "novel-a" not in page._src_rows


def test_type_change_removes_incompatible_selected_sources(app, manager):
    page = SearchPage(manager, search)
    page._selected_sources = {"novel-a", "comic-a"}
    page.type_combo.setCurrentIndex(page.type_combo.findData("comic"))
    page._on_type_changed("comic")
    assert page._selected_sources == {"comic-a"}
    assert page._all_selected is False


def test_search_type_all_uses_all_enabled_sources(app, manager):
    page = SearchPage(manager, search)
    page.type_combo.setCurrentIndex(page.type_combo.findData(""))
    page._on_search()
    assert submitted_source_ids(search) == {"novel-a", "comic-a", "video-a"}
```

Use the existing fake manager/search fixtures and preserve the current test style. The tests must assert both visible menu rows and submitted search sources.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```bash
python -m pytest tests/test_search_page_multisource.py tests/test_search_page_source_menu.py -q
```

Expected failure: the current source menu still contains all enabled source types and the current selection state does not have a type-change normalization hook.

- [ ] **Step 3: Implement the minimal source-type filter**

Add a helper with one responsibility:

```python
def _sources_for_current_type(self):
    selected_type = self.type_combo.currentData() or ""
    sources = self._manager.enabled_sources()
    if selected_type:
        sources = [s for s in sources if s.content_type == selected_type]
    return sources
```

Use it in:

- source-menu rebuilding;
- the “全部” action so it selects only the currently visible type-scoped sources;
- selected-source row construction;
- `_on_search()` before creating `_SearchTask` instances.

Connect the type combo change signal to a handler that:

1. removes selected IDs not in the current type-scoped source set;
2. changes `_all_selected` to `False` when a previous all-type selection is no longer valid;
3. updates the button text and menu check states;
4. does not start a search until the user presses Search.

For the “全部类型” option, return all enabled sources and preserve current behavior.

- [ ] **Step 4: Run focused tests and regressions**

Run:

```bash
python -m pytest tests/test_search_page_multisource.py tests/test_search_page_source_menu.py tests/test_search_filter.py -q
python -m compileall -q framework gui tests
git diff --check
```

Expected: all focused tests pass and search tasks contain only the selected content type.

---

### Task 2: Choose Folder for Batch Add-to-Shelf

**Files:**
- Modify: `gui/pages/search_page.py:226-250, 1108-1115`
- Modify: `gui/app.py:571-599, 690-700` to provide folder names and accept the folder payload
- Modify: existing search-page and shelf integration only; do not create a second store
- Test: existing search-page tests and existing shelf-service/favorite-flow tests

**Interfaces:**
- Consumes: selected `SearchResult` items and an App-provided folder-name callback backed by `LibraryStore.list_folders()`.
- Produces: `add_to_shelf_requested` carrying `(items, folder_name)` where `folder_name == ""` means uncategorized; `"全部"` is never emitted as a writable folder.

- [ ] **Step 1: Write failing tests**

Add tests for:

```python
def test_batch_add_shelf_emits_selected_folder(app, page):
    page._selected = {"u1": result("u1"), "u2": result("u2")}
    page._choose_shelf_folder = lambda: "漫画收藏"
    captured = []
    page.add_to_shelf_requested.connect(lambda items, folder: captured.append((items, folder)))
    page._on_batch_add_shelf()
    assert captured[0][1] == "漫画收藏"
    assert [item.url for item in captured[0][0]] == ["u1", "u2"]


def test_batch_add_shelf_cancel_keeps_selection(app, page):
    page._selected = {"u1": result("u1")}
    page._choose_shelf_folder = lambda: None
    page._on_batch_add_shelf()
    assert set(page._selected) == {"u1"}


def test_all_is_not_a_writable_folder(app, page):
    page._shelf_folder_provider = lambda: ["全部", "漫画收藏"]
    assert "全部" not in page._writable_folder_names()
```

The dialog test may use the existing `QInputDialog`/`QMessageBox` stubbing pattern. No `qtbot` or new test dependency.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```bash
python -m pytest tests/test_search_page_multisource.py tests/test_search_page_source_menu.py tests/test_favorite_flow.py -q
```

Expected failure: the current signal emits only `items`, and no folder choice occurs.

- [ ] **Step 3: Implement folder selection**

Change the signal to:

```python
add_to_shelf_requested = Signal(object, str)
```

Add these helpers in `SearchPage`; the provider is injected by `App` and returns `LibraryStore.list_folders()`:

```python
def _writable_folder_names(self):
    return [name for name in self._shelf_folder_provider() if name != "全部"]


def _choose_shelf_folder(self):
    folders = ["未分类"] + [name for name in self._writable_folder_names()]
    choice, accepted = QInputDialog.getItem(
        self,
        "选择收藏夹",
        "加入到：",
        folders,
        0,
        False,
    )
    if not accepted:
        return None
    return "" if choice == "未分类" else choice
```

Add an optional constructor callback such as `shelf_folder_provider=None`, defaulting to a provider that returns `[]` for isolated tests. In `App._build_search()`, pass a callback that calls the existing App-owned store's `list_folders()`. Do not make `SearchPage` construct a second store. The App handler receives `(items, folder)` and calls the existing `store.add(..., folder=folder)` path.

Update `_on_batch_add_shelf()`:

```python
folder = self._choose_shelf_folder()
if folder is None:
    return
self.add_to_shelf_requested.emit(items, folder)
self._clear_selection()
```

Update the App handler to pass `folder=folder` into the existing favorite-add operation. Preserve password protection: adding to a locked target folder remains allowed according to the current project behavior; deleting/moving out of a locked folder remains protected.

- [ ] **Step 4: Run focused tests and regressions**

Run:

```bash
python -m pytest tests/test_search_page_multisource.py tests/test_search_page_source_menu.py tests/test_search_filter.py tests/test_favorite_flow.py tests/test_shelf_service.py -q
python -m compileall -q framework gui tests
git diff --check
```

Expected: type-scoped source filtering and folder-selected batch add pass without regressions.

---

## Final Verification

- [ ] Search menu shows only sources matching the selected type.
- [ ] Search “全部类型” shows all enabled source types.
- [ ] Type changes clear incompatible source selections.
- [ ] Search results still support source chips and result filtering.
- [ ] Batch add opens a folder selector.
- [ ] Cancelling folder selection leaves result selection intact.
- [ ] “全部” cannot be written as a folder.
- [ ] Uncategorized maps to `folder=""`.
- [ ] Existing favorite and locked-folder behavior remains unchanged.
- [ ] Focused tests pass.
- [ ] Compileall passes.
- [ ] `git diff --check` passes.
