# 发现页换源会话缓存 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在发现页源切换时冻结/还原每个源的完整浏览会话（分类栏 + 作品网格 + 页数 + 滚动位置），换回命中缓存时零网络请求，仅当用户滚动越过缓存末尾或点其它分类时才按需联网。

**Architecture:** 在 `framework/cache_service.py` 新增**不落盘**的会话级 `RedisLikeStore` 单例（`persist_path=None` → 重启即清、退出无需清理代码）；全部缓存/恢复逻辑放在 GUI 层 `discover_page.py`，与 `list_works`/`list_categories`/`Content._cache` 完全解耦（不违反 2026-09-08「发现页不走持久缓存」决策）。恢复命中时就地重建分类按钮、渲染 `_works` 卡片（主线程同步），滚动经 `QTimer.singleShot(0)` 在布局稳定后 setValue。

**Tech Stack:** Python 3.x / PySide6 / 现有 `RedisLikeStore`（dict+LRU+quota+pickle）/ pytest / 离屏 GUI smoke。

## Global Constraints

- 换源场景仅限**发现页顶部源下拉框**（`DiscoverPage._on_source_changed`），不含搜索页/阅读器。
- 快照上限 **5 个源**（LRU 逐出最旧）；quota **2GB**；**重启即清**（`persist_path=None`）。
- 恢复后行为：**纯缓存、不后台刷新**；后续用户操作（点其它分类 / 滚动过缓存末尾且 `has_more`）才走网络。
- **不得改动** `framework/discovery.py: list_works/list_categories`、`framework/search.py`、`Content._cache`（2026-09-08 cache-policy 决策）。
- `DiscoverPage.__init__` 新参数 `session_cache=None` 放**末尾**（`gui/_smoke_discover.py:128` 用位置参数构造，默认值保兼容）。
- 工作区存在其它并行任务未提交改动（video-recommend / auto-scroll / search-multi-source / 91porna / hciyuan 等），**只改本计划列出文件**。
- 所有快照序列化使用 `Work` **dataclass 对象直接 pickle**（禁用 `Work.as_dict()`/`Work(**d)` 往返——`as_dict` 输出键 `update_info` 与构造字段 `update` 不对称，会抛 TypeError）。
- 运行环境：Windows PowerShell；命令前缀 `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python ...`。
- 不 git commit（除非用户明确要求）。

---

### Task 1: 会话级缓存单例（cache_service.py）

**Files:**
- Modify: `framework/cache_service.py`（模块底部单例区 + `get_session_cache`）
- Test: `tests/test_cache_service.py`

**Interfaces:**
- Produces: `get_session_cache(data_dir=None) -> RedisLikeStore | None`
  - 全局单例 `_session_singleton`；无持久路径（`persist_path=None`）；`quota=2*1024**3`。
  - 初始化失败返回 None（上层降级禁用）。

- [ ] **Step 1: 写失败测试**

```python
def test_get_session_cache_singleton_not_persisted():
    """会话缓存：不落盘（重启即清）、2GB 配额、单例。"""
    from framework.cache_service import get_session_cache, RedisLikeStore

    a = get_session_cache()
    b = get_session_cache()
    assert a is b                       # 单例
    assert isinstance(a, RedisLikeStore)
    assert a._persist_path is None      # 不落盘 → 退出即释放，无需清理
    assert a._quota == 2 * 1024 * 1024 * 1024
    # 写读往返
    a.set("disc:demo", "v")
    assert a.get("disc:demo") == "v"
```

（在 `tests/test_cache_service.py` 末尾追加。）

- [ ] **Step 2: 运行验证失败**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_cache_service.py::test_get_session_cache_singleton_not_persisted -q`
Expected: FAIL（`ImportError: cannot import name 'get_session_cache'`）

- [ ] **Step 3: 实现**

在 `framework/cache_service.py` 单例区（`_search_singleton` 声明处，:264）加入：

```python
_session_singleton: Optional[RedisLikeStore] = None
```

在 `_configure_defaults`（:267）里一并创建（power 不落盘——`persist_path=None`）：

```python
    _session_singleton = RedisLikeStore(
        quota=2 * 1024 * 1024 * 1024, persist_path=None
    )
```

`_configure_defaults` 的 `global` 声明改为 `global _shelf_singleton, _search_singleton, _session_singleton`。

文件末尾追加（`get_search_cache` 之后）：

```python
def get_session_cache(data_dir: Optional[str] = None) -> Optional[RedisLikeStore]:
    """获取会话级缓存池（换源快照用）：不落盘，重启即清。

    纯内存（persist_path=None）→ 进程退出自动释放，无需显式清理，
    不会在退出路径阻塞。初始化失败返回 None → 调用方功能降级禁用。
    """
    global _session_singleton
    if _session_singleton is None:
        try:
            _configure_defaults(data_dir)
        except Exception as exc:  # noqa: BLE001
            log.warning("[cache] 会话缓存初始化失败: %s", exc)
            _session_singleton = None
    return _session_singleton
```

- [ ] **Step 4: 运行验证通过**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_cache_service.py -q`
Expected: 全部 PASS（既有 + 新增 1 个）

- [ ] **Step 5: 提交**

```bash
git add framework/cache_service.py tests/test_cache_service.py
git commit -m "feat: 新增会话级不落盘缓存单例 get_session_cache"
```
（若用户明确不需要 commit 则跳过——默认**不提交**，等待用户指令。）

---

### Task 2: Work 快照序列化往返 + 源 LRU 辅助（纯逻辑层）

**Files:**
- Create: `framework/discover_session.py`（会话快照纯逻辑模块，无 GUI 依赖，可单测）
- Test: `tests/test_discover_session.py`

**Interfaces:**
- Consumes: `framework.discovery.Work`（dataclass：title/url/cover/author/update/source_id/source_name）
- Produces:
  - `SOURCE_CACHE_MAX = 5`
  - `SNAPSHOT_KEY_PREFIX = "disc:"`
  - `snapshot_key(source_id: str) -> str`
  - `make_snapshot(cat_buttons: list, cat_url: str, works: list, pages: list, has_more: bool, current_page: int, scroll: float, ts: float) -> dict`
  - `from_snapshot(snap: dict) -> None`（仅校验结构，返回净化后的 dict）
  - `cat_list_from_buttons(cat_buttons: list) -> list`（`[(title, cat.url or None), ...]`，cat 为 None 表示「全部」）
  - `register_source(store, source_id: str) -> None`：LRU 上限控制（≤5），超限逐出最旧
  - `prune_excess(store, order: list) -> list`：返回应删除的 key 列表（纯函数，便于测试）

- [ ] **Step 1: 写失败测试**

```python
"""发现页换源会话快照纯逻辑测试（无 GUI 依赖）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.discovery import Work
from framework.discover_session import (
    SOURCE_CACHE_MAX,
    snapshot_key,
    make_snapshot,
    cat_list_from_buttons,
    prune_excess,
    register_source,
)
from framework.cache_service import RedisLikeStore


def test_snapshot_key():
    assert snapshot_key("srcA") == "disc:srcA"


def test_make_snapshot_and_works_roundtrip_via_pickle():
    """list[Work] 经 RedisLikeStore pickle 往返后字段完整（禁用 as_dict 往返）。"""
    ws = [Work(title="书一", url="/b1", cover="/c1.jpg", source_id="srcA", source_name="源A")]
    snap = make_snapshot(
        cat_buttons=[],
        cat_url="/cat/x",
        works=ws,
        pages=[1, 2],
        has_more=True,
        current_page=2,
        scroll=0.6,
        ts=123.0,
    )
    assert snap["cat_url"] == "/cat/x"
    assert snap["pages"] == [1, 2]
    assert snap["scroll"] == 0.6
    assert snap["has_more"] is True
    assert snap["current_page"] == 2

def test_cat_list_from_buttons_text():
    # 空按钮 → 空 cats
    snap = make_snapshot(
        cat_buttons=[], cat_url="", works=[], pages=[],
        has_more=False, current_page=0, scroll=0.0, ts=1.0,
    )
    assert snap["cats"] == []

    class _TBtn:
        def __init__(self, text):
            self._t = text

        def text(self):
            return self._t

    class _Cat:
        def __init__(self, url):
            self.url = url

    snap = make_snapshot(
        cat_buttons=[(_TBtn("全部"), None), (_TBtn("玄幻"), _Cat("/cat/x"))],
        cat_url="/cat/x", works=[], pages=[],
        has_more=False, current_page=0, scroll=0.0, ts=1.0,
    )
    assert snap["cats"] == [("全部", None), ("玄幻", "/cat/x")]


def test_works_pickle_roundtrip_fields():
    ws = [Work(title="书一", url="/b1", cover="/c1.jpg", source_id="srcA", source_name="源A", update="连载")]
    store = RedisLikeStore(quota=1024 * 1024)
    snap = make_snapshot(cat_buttons=[], cat_url="/cat/x",
                         works=ws, pages=[1], has_more=True,
                         current_page=1, scroll=0.5, ts=1.0)
    store.set(snapshot_key("srcA"), snap)
    got = store.get(snapshot_key("srcA"))
    assert got["works"][0].title == "书一"
    assert got["works"][0].url == "/b1"
    assert got["works"][0].update == "连载"


def test_prune_excess_keeps_five():
    order = ["s1", "s2", "s3", "s4", "s5", "s6"]
    assert prune_excess(order) == ["disc:s1"]          # 只删最旧一个
    order = ["s1", "s2", "s3", "s4", "s5"]
    assert prune_excess(order) == []


def test_register_source_lru():
    store = RedisLikeStore(quota=1024 * 1024)
    for sid in ["s1", "s2", "s3", "s4", "s5", "s6"]:
        register_source(store, sid)
    assert store.get(snapshot_key("s1")) is None   # 最旧被逐出
    assert store.get(snapshot_key("s6")) is not None
    assert SOURCE_CACHE_MAX == 5
```

- [ ] **Step 2: 运行验证失败**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_discover_session.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'framework.discover_session'`）

- [ ] **Step 3: 实现 `framework/discover_session.py`**

```python
"""发现页换源会话快照纯逻辑（无 GUI 依赖，方便单测）。

不触碰 discovery/list_works/list_categories 与 Content._cache——
换源会话缓存是 GUI 层独立会话机制（重启即清），与持久缓存决策解耦。
"""

from __future__ import annotations

from typing import Optional

from .cache_service import RedisLikeStore

SOURCE_CACHE_MAX = 5
SNAPSHOT_KEY_PREFIX = "disc:"


def snapshot_key(source_id: str) -> str:
    return f"{SNAPSHOT_KEY_PREFIX}{source_id}"


def cat_list_from_buttons(cat_buttons: list) -> list:
    """把 (btn, cat) 列表提取为可序列化 [(title, url|None)]。

    cat 为 None 表示「全部」按钮；title 取按钮 text()。
    """
    out = []
    for btn, cat in cat_buttons:
        title = getattr(btn, "text", lambda: "")()
        url = getattr(cat, "url", None) if cat is not None else None
        out.append((title, url))
    return out


def make_snapshot(
    cat_buttons,
    cat_url: str,
    works: list,
    pages: list,
    has_more: bool,
    current_page: int,
    scroll: float,
    ts: float,
) -> dict:
    return {
        "cats": cat_list_from_buttons(cat_buttons),
        "cat_url": cat_url,
        "works": list(works),
        "pages": sorted(set(pages)),
        "has_more": bool(has_more),
        "current_page": int(current_page),
        "scroll": round(float(scroll), 4),   # 0~1 滚动比例
        "ts": float(ts),
    }


def from_snapshot(snap: dict) -> Optional[dict]:
    """结构净化：缺关键字段返回 None（调用方按未命中处理）。"""
    if not isinstance(snap, dict):
        return None
    for k in ("cats", "cat_url", "works", "pages", "has_more", "current_page", "scroll", "ts"):
        if k not in snap:
            return None
    return snap


def register_source(store: Optional[RedisLikeStore], source_id: str) -> None:
    """登记源使用（LRU 上限控制）。超上限逐出最旧源的缓存键。"""
    if store is None or not source_id:
        return
    orig = store.get(SNAPSHOT_KEY_PREFIX + "_order")
    order = list(orig) if isinstance(orig, list) else []
    if source_id in order:
        order.remove(source_id)
    order.append(source_id)
    for key in prune_excess(order):
        store.delete(key)
    store.set(SNAPSHOT_KEY_PREFIX + "_order", order)


def prune_excess(order: list) -> list:
    """超上限时返回应删除的快照 key 列表（纯函数）。"""
    order = list(order)
    to_delete = []
    while len(order) > SOURCE_CACHE_MAX:
        oldest = order.pop(0)
        to_delete.append(snapshot_key(oldest))
    return to_delete
```

> 说明：LRU 用 `disc:_order` 元键（list[str]）记录源顺序，`register_source` 在命中/写入前调用；`prune_excess` 纯函数负责「最旧者逐出、返回待删 key」。快照 key 本身就是 `disc:{sid}`，与元键前缀区分（`disc:_order` 不冲突）。

- [ ] **Step 4: 运行验证通过**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_discover_session.py -q`
Expected: 全部 PASS

> **实现备注（后续 Task 引用）**：`_render_restored` 与 `_try_restore_source_snapshot` 需要一个把 `[(title, url|None)]` 还原成按钮的辅助。该函数需 GUI，放 Task 3。快照在 Task 3 写入时用 `cat_list_from_buttons(self._cat_buttons)` + `(self._current_cat_url or "")`。

---

### Task 3: DiscoverPage 会话快照写入与恢复（GUI 层）

**Files:**
- Modify: `gui/pages/discover_page.py`
  - `__init__` 增加 `session_cache=None` 参数（末尾，保位置参数兼容）
  - `_on_scroll`（:254 区域新建 `_record_scroll_ratio` + 节流）
  - `_on_page_loaded`（:592 后）追加快照更新
  - `_on_source_changed`（:275）切走旧源前落快照 + 切到新源尝试恢复
  - `_reset_works`（:437）`_pending_restore` 分支
  - `_load_categories`（:301）命中恢复时跳过
  - 新增 `_save_source_snapshot`、`_try_restore_source_snapshot`、`_render_restored`、`_restore_scroll`（`_scroll_timer` 节流属性）
- Modify: `gui/app.py`（`_build_discover` :422 传入 `session_cache`）
- Modify: `gui/_smoke_discover.py`（扩展：换源会话往返断言）

**Interfaces:**
- Consumes:
  - `get_session_cache()`（Task 1）
  - `make_snapshot`/`from_snapshot`/`register_source`/`snapshot_key`/`prune_excess`（Task 2）
- Produces:
  - `DiscoverPage.__init__(..., session_cache=None)`
  - `self._pending_restore: Optional[dict]`
  - `self._scroll_timer: Optional[QTimer]`
  - `self._source_order`（经 register_source 维护，不直接持有）

- [ ] **Step 1: 写失败测试（GUI 离屏 smoke 扩展）**

在 `gui/_smoke_discover.py` 的 `main()` 末尾（`_reflow` 断言之后、bulk 之前）追加：

```python
    # ---- 换源会话缓存：换走→换回 0 请求还原 ----
    from framework.cache_service import get_session_cache
    from framework.discover_session import snapshot_key

    session = get_session_cache()
    session.clear()
    # 未注入时给 page 补一个，验证往返
    if page.session_cache is None:
        page.session_cache = session

    # 保证滚动有代表值并落盘当前 demo 会话（已加载 2 部作品）
    page._current_cat_url = "/search"
    page._current_page = 1
    page._save_source_snapshot()
    assert session.get(snapshot_key("demo")) is not None

    # 模拟切走：清空当前视图（真实换源会在 _on_source_changed 里清）
    page._clear_works()
    page._works = []
    page._loaded_pages = set()

    # 换回 demo → 命中并渲染，不触发抓取
    page._current_source = mgr.get("demo")
    restored = page._try_restore_source_snapshot()
    assert restored, "应命中会话缓存"
    assert len(page._works) == 2, len(page._works)  # 恢复 2 部作品
    cards2 = [page.grid_layout.itemAt(i).widget() for i in range(page.grid_layout.count())]
    cards2 = [c for c in cards2 if c is not None]
    assert len(cards2) == 2, len(cards2)
    print("\n换源会话缓存还原: works=%d 卡片=%d" % (len(page._works), len(cards2)))
```

> 说明：smoke 中不真正发网络（MockHttp），核心断言是「快照可写、恢复渲染出 2 卡片」。真正的「0 请求」断言放单元测试（Task 4）用假 store 注入验证。

- [ ] **Step 2: 运行验证失败**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python gui/_smoke_discover.py`
Expected: FAIL（`AttributeError: 'DiscoverPage' object has no attribute 'session_cache'` 或 `_save_source_snapshot` 不存在）

- [ ] **Step 3: 实现 `__init__` 参数 + 实例属性**

```python
    def __init__(
        self,
        source_manager: SourceManager,
        discovery: Discovery,
        content: Content,
        bulk_fetch: BulkFetch,
        event_bus: EventBus,
        theme_manager: ThemeManager,
        parent=None,
        session_cache=None,      # 新增（末尾，默认 None=禁用）
    ):
        ...
        self.session_cache = session_cache
        self._pending_restore = None
        self._scroll_timer = None
```

新增常量与 import（文件顶部）：

```python
from framework.cache_service import RedisLikeStore  # 仅类型注解
from framework.discover_session import (
    SOURCE_CACHE_MAX,
    make_snapshot,
    from_snapshot,
    register_source,
    snapshot_key,
)
from PySide6.QtCore import Qt, Signal, QThreadPool, QRunnable, QObject, QTimer
```

- [ ] **Step 4: 实现快照写入（`_save_source_snapshot`）**

在 `_on_source_changed` 之前加方法：

```python
    def _save_source_snapshot(self) -> None:
        """冻结当前源完整浏览会话（分类/作品/页数/滚动）到会话缓存。

        只在切源离开/每页加载完成/滚动节流时调用。失败静默（降级）。
        """
        cache = self.session_cache
        if cache is None:
            return
        source = self._current_source
        if source is None:
            return
        try:
            import time
            snap = make_snapshot(
                self._cat_buttons,
                self._current_cat_url or "",
                self._works,
                list(self._loaded_pages),
                self._has_more,
                self._current_page,
                self._scroll_ratio(),
                time.time(),
            )
            register_source(cache, source.source_id)
            cache.set(snapshot_key(source.source_id), snap)
        except Exception:  # noqa: BLE001
            pass  # 缓存失败不影响浏览
```

新增 `_scroll_ratio`：

```python
    def _scroll_ratio(self) -> float:
        vbar = self.scroll.verticalScrollBar()
        m = vbar.maximum()
        if m <= 0:
            return 0.0
        return vbar.value() / m
```

- [ ] **Step 5: `_on_scroll` 节流滚动比例**

在 `_on_scroll`（:254）加入节流更新：滚动事件发得密集，首触发后 500ms 落盘一次：

```python
    def _on_scroll(self, value: int) -> None:
        """滚动到 80% 触发加载下一页（懒加载 + 预加载缓冲）。

        提前到 80% 而非贴底：滚动到底前下一批已在后台抓取，视觉无停顿；
        又不一次性并发爬多页（防封面加载不过来 / 反爬）。
        顺带节流记录滚动比例到会话缓存（恢复滚动位置用）。
        """
        self._schedule_scroll_snapshot()
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() > 0 and value >= vbar.maximum() * 0.8:
            self._load_next_page()

    def _schedule_scroll_snapshot(self) -> None:
        """滚动比例延迟 500ms 落盘（节流，避免高频写缓存）。"""
        if self.session_cache is None:
            return
        if self._scroll_timer is None:
            from PySide6.QtCore import QTimer
            self._scroll_timer = QTimer(self)
            self._scroll_timer.setSingleShot(True)
            self._scroll_timer.timeout.connect(self._save_source_snapshot)
        self._scroll_timer.start(500)
```

- [ ] **Step 6: `_on_page_loaded` 成功后写快照**

在 `_on_page_loaded` 的 `self._loaded_pages.add(page)`（:592）之后、`_maybe_preload` 之前插入：

```python
        # 会话缓存：每页并入后更新快照（覆盖写，量小无碍）
        self._save_source_snapshot()
```

- [ ] **Step 7: `_on_source_changed` 切走落盘 + 切到尝试恢复**

修改 `_on_source_changed`（:275-291）：

```python
    def _on_source_changed(self, index: int) -> None:
        if index < 0:
            return
        # 切走前冻结旧源会话（含滚动比例）——这样换回时能 0 请求还原
        if self._current_source is not None:
            self._save_source_snapshot()
        # 递增序号：使旧源的加载回调失效（防止快速切换竞态/卡死）
        self._source_epoch += 1
        self._current_source = self.source_combo.itemData(index)
        self._current_page = 0
        self._has_more = True
        self._current_cat_url = None
        self._cat_collapsed = True
        self.status_label.setText("正在加载分类...")
        # 立即清空旧源的作品网格，避免换源瞬间旧内容残留/溢出
        self._clear_works()
        self._clear_cat_buttons()
        # 换回该源→命中会话缓存→直接还原（0 请求）；否则正常网络加载
        if not self._try_restore_source_snapshot():
            self._load_categories()
        # 作品加载在分类就绪/缓存还原后触发
```

- [ ] **Step 8: 实现恢复逻辑（`_try_restore_source_snapshot` / `_render_restored` / `_restore_scroll`）**

在 `_on_source_changed` 之后新增：

```python
    def _try_restore_source_snapshot(self) -> bool:
        """换回某源：命中会话缓存则就地还原完整浏览会话，返回 True。

        命中后置 _pending_restore（_reset_works 消费）并重建分类栏；
        不命中返回 False，调用方走正常网络加载。
        """
        cache = self.session_cache
        source = self._current_source
        if cache is None or source is None:
            return False
        try:
            snap = from_snapshot(cache.get(snapshot_key(source.source_id)))
            if snap is None:
                return False
            self._pending_restore = snap
            self._render_cat_buttons_from_snapshot(snap)      # 0 请求重建分类栏
            self._rendered_entry = True
            register_source(cache, source.source_id)          # 命中即刷新该源 LRU 次序
            self._reset_works()        # 消费 _pending_restore → 就地渲染（0 请求）
            return True
        except Exception:  # noqa: BLE001
            self._pending_restore = None
            return False

    def _render_cat_buttons_from_snapshot(self, snap: dict) -> None:
        """用快照 cats 重建分类按钮（不请求网络）。

        与 _on_categories_loaded 的按钮构建共用 _make_cat_button/_refresh_cat_buttons：
        按钮对象只建一次，折叠只切可见性（保持现有模式）。
        """
        self._clear_cat_buttons()
        self._cat_buttons = []
        from framework.discovery import Category
        cats = snap.get("cats") or []
        # 「全部」按钮（cat=None）
        all_btn = self._make_cat_button("全部", None, False)
        self._cat_buttons.append((all_btn, None))
        for ctitle, cur_l in cats:
            cat = Category(title=ctitle or "", url=cur_l or "") if ctitle else None
            btn = self._make_cat_button(ctitle or "全部", cat, False)
            self._cat_buttons.append((btn, cat))
        self._cat_bar_populated = False
        self._refresh_cat_buttons(self._default_cat_url())
        # 选中快照 cat_url 对应的按钮
        target = snap.get("cat_url") or self._default_cat_url()
        for btn, cat in self._cat_buttons:
            selected = (getattr(cat, "url", None) or "") == target or (
                cat is None and target == self._default_cat_url()
            )
            btn.setChecked(bool(selected))
        self._current_cat_url = target

    def _default_cat_url(self) -> str:
        disc = self._current_source.get_discovery_config()
        return (
            disc.get("works_list_url")
            or disc.get("list_url")
            or self._current_source.base_url
        )
```

> 注意：`_make_cat_button`（:364）的签名是 `(text, cat, checked)`，可直接复用。

- [ ] **Step 9: `_reset_works` 消费 `_pending_restore`**

修改 `_reset_works`（:437-462）末尾的循环前：

```python
        for p in range(1, 1 + self._preload_ahead + 1):
            self._load_next_page(page=p)
```

改为：

```python
        if self._pending_restore is not None:
            self._render_restored(self._pending_restore)
            self._pending_restore = None
            return
        for p in range(1, 1 + self._preload_ahead + 1):
            self._load_next_page(page=p)
```

新增 `_render_restored`：

```python
    def _render_restored(self, snap: dict) -> None:
        """用快照渲染作品网格（0 网络请求），并恢复滚动位置。"""
        works = [w for w in (snap.get("works") or []) if isinstance(w, object)]
        self._works = []
        self._seen_urls = set()
        fresh = [w for w in works if w.url not in self._seen_urls and not self._seen_urls.add(w.url)]
        self._works.extend(fresh)
        cols = self._columns()
        for w in fresh:
            self._append_card(w, cols)
        self._apply_column_stretch(cols)
        self._loaded_pages = set(snap.get("pages") or [])
        self._current_page = int(snap.get("current_page") or 0) or max(self._loaded_pages or [0])
        self._has_more = bool(snap.get("has_more"))
        self._work_count = len(fresh)
        status = f"已还原 {len(self._loaded_pages)} 页 · 共 {self._work_count} 部（会话缓存）"
        self.status_label.setText(status)
        # 布局稳定后再恢复滚动（内容高度未就绪时 setValue 无效）
        self._restore_scroll(float(snap.get("scroll") or 0.0))

    def _restore_scroll(self, ratio: float) -> None:
        from PySide6.QtCore import QTimer
        ratio = max(0.0, min(1.0, ratio))

        def _apply() -> None:
            vbar = self.scroll.verticalScrollBar()
            if vbar.maximum() > 0:
                vbar.setValue(int(ratio * vbar.maximum()))
        QTimer.singleShot(0, _apply)
```

> 注：`_append_works` 异步批量（含跨页去重），恢复场景一次渲染全部 works，直接 `_append_card` 逐个摆放（与 `_reflow` 模式一致，:794）。

- [ ] **Step 10: 注入 app.py**

修改 `_build_discover`（app.py:423）：

```python
    def _build_discover(self) -> DiscoverPage:
        from framework.cache_service import get_session_cache
        page = DiscoverPage(
            source_manager=self.source_manager,
            discovery=self.discovery,
            content=self.content,
            bulk_fetch=self.bulk_fetch,
            event_bus=self.event_bus,
            theme_manager=self.theme_manager,
            session_cache=get_session_cache(),
        )
```

- [ ] **Step 11: 运行验证通过**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python gui/_smoke_discover.py`
Expected: 原有全部断言 PASS（×：源选择器 1、分类按钮、折叠、展开、2 卡片、直进阅读、reflow 保留卡片、全量抓取）+ 新增会话还原断言 PASS，末尾「=== 发现界面离屏测试通过 ===」

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_discover_session.py tests/test_cache_service.py -q`
Expected: 全部 PASS + 202 基线

---

### Task 4: 恢复「0 请求」单元测试（假 store 注入）

**Files:**
- Create: `tests/test_discover_restore_no_request.py`

**Interfaces:**
- Consumes: DiscoverPage（离屏构造，mode 桩注入 session_cache 与假 source manager）、`register_source`/`snapshot_key`/`make_snapshot`、`RedisLikeStore`
- Produces: 无

> 关键：不真正构造 DiscoverPage 也行——抽还原判定为**可单测的纯函数**更稳。在 `framework/discover_session.py` 加 `should_restore(store, source_id) -> bool`（有快照且结构合法）。GUI 侧 `_try_restore_source_snapshot` 内部直接调它，逻辑零 GUI 依赖。

- [ ] **Step 1: 在 `framework/discover_session.py` 加纯函数 `should_restore`**

```python
def should_restore(store: Optional[RedisLikeStore], source_id: str) -> bool:
    """换回某源时判断是否应命中会话缓存（结构校验后）。"""
    if store is None or not source_id:
        return False
    return from_snapshot(store.get(snapshot_key(source_id))) is not None
```

- [ ] **Step 2: 写测试**

```python
"""换源恢复 0 请求：should_restore / LRU / 缺失分支。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.discover_session import (
    should_restore,
    register_source,
    snapshot_key,
    make_snapshot,
    prune_excess,
)
from framework.cache_service import RedisLikeStore


def test_should_restore_true_with_valid_snapshot():
    store = RedisLikeStore(quota=1024 * 1024)
    register_source(store, "srcA")
    store.set(snapshot_key("srcA"), make_snapshot(
        cat_buttons=[], cat_url="/all", works=[], pages=[1, 2],
        has_more=True, current_page=2, scroll=0.5, ts=1.0,
    ))
    assert should_restore(store, "srcA") is True


def test_should_restore_false_when_missing_or_broken():
    store = RedisLikeStore(quota=1024 * 1024)
    assert should_restore(store, "srcZ") is False      # 无快照
    store.set(snapshot_key("srcZ"), {"partial": True})  # 结构残缺
    assert should_restore(store, "srcZ") is False
    assert should_restore(None, "srcA") is False       # store 为 None


def test_should_restore_false_after_prune_excess():
    store = RedisLikeStore(quota=1024 * 1024)
    for i in range(1, 7):          # 6 个源 → s1 被逐出
        register_source(store, f"s{i}")
    assert should_restore(store, "s1") is False
    assert should_restore(store, "s6") is True or should_restore(store, "s5") is True
```

- [ ] **Step 3: 运行验证失败 → 通过**

先实现 `should_restore` 再 Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_discover_restore_no_request.py -q`
Expected: PASS
（若先写测试后实现：首次运行应 FAIL `ImportError: cannot import name 'should_restore'`）

- [ ] **Step 4: 全量回归**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/ -q`
Expected: 202 + 新增（test_cache_service +1、test_discover_session +5、test_discover_restore_no_request +3）≈ **211 PASS**

---

### Task 5: 收尾验证（smoke 全量 + 排查交互副作用）

**Files:**
- Modify: 无（仅验证）

- [ ] **Step 1: 运行全部 smoke**

Run（依次）：
```bash
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python gui/_smoke_regression.py
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python gui/_smoke_reader.py
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python gui/_smoke_library.py
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python gui/_smoke_discover.py
```
Expected: 四个 smoke 全部「通过」退出（`gui/_smoke_search.py` 为既有已知失败，不测）。

- [ ] **Step 2: 现有 pytest 全量**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/ -q`
Expected: 全绿（≈211 PASS）

- [ ] **Step 3: 手工核查交互副作用清单**

经代码走查确认以下几点无误（不启动真实 GUI）：
1. `_on_source_changed` 首次进入（`_reload_sources` :255 触发）：`_current_source` 为 None → 不落快照；`_try_restore_source_snapshot` store 可用但无快照 → 走 `_load_categories` 正常加载。✓
2. 类型筛选切换（`_on_type_filter_changed` → `_reload_sources`）：每次重建源列表会触发 `_on_source_changed`。旧源快照已落盘；新源命中缓存还原。分类栏重建后折叠状态按 `cat_collapsed=True` 显示。✓
3. `_scroll_timer` 在页面销毁时：`QTimer(parent=self)` 自动清理，无崩溃。✓
4. 换分类（点细分按钮）不触发恢复分支——`_try_restore_source_snapshot` 仅在 `_on_source_changed` 调用；`_reset_works` 的 `_pending_restore` 在普通分类切换时为 None → 走网络。✓
5. 快照过期/损坏：`from_snapshot` 返回 None → 按未命中处理。✓
6. 会话缓存写入的 key 前缀 `disc:` 与现有 `redis_search.gz`/`redis_shelf.gz` 分离（独立 store），互不污染。✓

- [ ] **Step 4: 更新 SESSION.md / learning-log / CONVERSATION_LOG**

按 AGENTS.md 会话收尾要求，在 `D:\code\claw\SESSION.md`、`C:\Users\alonely\learning-log.md`、`C:\Users\alonely\CONVERSATION_LOG.md` 追加本功能完成记录（实现要点 + 测试结果 + 待办）。