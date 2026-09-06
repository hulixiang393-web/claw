# Redis 风格缓存机制实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为书架（3G）/发现搜索（10G）实现 Redis 风格磁盘持久化缓存（含章节预加载、搜索页数放开、手动清除）。

**Architecture:** 零依赖自研 `cache_service.py`（dict + LRU + TTL + 字节配额 + pickle-gz 落盘），两个池实例（ShelfCache 3G / SearchCache 10G），注入 HttpClient → Content → CoverLoader → Search → Discovery 五条数据路径，阅读器进入时后台预缓存当前章+后三章，搜索去掉 max_pages/max_results 上限。

**Tech Stack:** Python 3 + pytest（仅测试）。纯标准库实现，无第三方运行时依赖。

## Global Constraints

- 缓存服务零第三方依赖（只用标准库：json/pickle/gzip/threading/atexit）。
- 落盘目录：`<项目根>/data/cache/`（与现有 `settings_page.py:243` `data/cache` 一致），文件名 `redis_shelf.gz` / `redis_search.gz`。
- TTL 分档：封面/页面文本=永久（仅 LRU 淘汰）；章节正文 `body:` 与漫画页 `pages:`=7 天；搜索 `search:` 与发现 `list:`=24 小时。
- 配额：ShelfCache=3G（`3*1024*1024*1024` 字节），SearchCache=10G。
- LRU 驱逐阈值：写入后 `_bytes_used > quota` 时从最旧（队首）逐出直到低于 `quota * 0.9`（防抖）。
- 线程安全：所有 store 方法用 `threading.Lock`。
- 落盘节流：≥500 次写或距上次落盘 ≥60s 才落盘；进程退出 `atexit` 强制落盘。
- 任何缓存失败必须静默降级（不崩主流程）：读盘失败→空缓存继续；单 key 超大→跳过缓存；缓存未初始化→调用方跳过。
- 每任务测试跑：`python -m pytest tests/test_cache_service.py -q`；全回归：`$env:PYTHONPATH = "D:\code\claw"; python -m pytest tests -q`。
- 测试 import 方式：`sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`（见 test_http_retry.py:20）。

---

### Task 1: cache_service 核心——RedisLikeStore（dict + LRU + TTL + 配额 + 持久化）

**Files:**
- Create: `framework/cache_service.py`
- Test: `tests/test_cache_service.py`

**Interfaces:**
- Consumes: 无（纯新类）。
- Produces: `RedisLikeStore(quota: int, persist_path: str | None = None)`，方法：
  - `get(key: str) -> Any | None`
  - `set(key: str, value, ttl: float | None = None) -> bool`（写入计数 + 触发驱逐；False=跳过缓存）
  - `hset(key: str, field, value) -> None` / `hget(key: str, field) -> Any` / `hgetall(key: str) -> dict`
  - `ttl(key: str) -> float | None`（剩余秒；永久/不存在返回 None）
  - `expire(key: str, ttl: float) -> None`
  - `delete(key: str) -> None`
  - `scan(pattern: str) -> list[str]`（`*`/`?` 通配，`fnmatch` 实现）
  - `clear() -> None`
  - `bytes_used() -> int` / `count() -> int`
  - `save() -> None` / `load() -> None`
  - `flush_checked() -> None`（按 500 写/60s 节流落盘）

- [ ] **Step 1: 写失败测试**（`tests/test_cache_service.py`）

```python
"""Redis 风格缓存服务单元测试。"""
from __future__ import annotations

import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from framework.cache_service import RedisLikeStore


def make_store(tmp_path, quota=64 * 1024, persist=None):
    return RedisLikeStore(quota=quota, persist_path=persist)


def test_set_get(tmp_path):
    s = make_store(tmp_path)
    s.set("a", "hello")
    assert s.get("a") == "hello"
    assert s.get("missing") is None


def test_ttl_expiry(tmp_path):
    s = make_store(tmp_path)
    s.set("a", "x", ttl=-1)  # 已过期
    assert s.get("a") is None
    s.set("b", "y", ttl=0.001)
    time.sleep(0.05)
    assert s.get("b") is None


def test_hset_hget(tmp_path):
    s = make_store(tmp_path)
    s.hset("book", "title", "A")
    s.hset("book", "author", "B")
    assert s.hget("book", "title") == "A"
    assert s.hgetall("book") == {"title": "A", "author": "B"}


def test_delete_and_clear(tmp_path):
    s = make_store(tmp_path)
    s.set("a", 1)
    s.set("b", 2)
    assert s.count() == 2
    s.delete("a")
    assert s.count() == 1
    s.clear()
    assert s.count() == 0


def test_scan_pattern(tmp_path):
    s = make_store(tmp_path)
    s.set("search:x:kw", 1)
    s.set("search:y:kw2", 2)
    s.set("cover:a:u", 3)
    keys = sorted(s.scan("search:*"))
    assert keys == ["search:x:kw", "search:y:kw2"]


def test_lru_eviction_and_bytes(tmp_path):
    s = RedisLikeStore(quota=150)
    s.set("k1", "v" * 50)
    s.set("k2", "v" * 50)
    s.set("k3", "v" * 50)
    assert s.bytes_used() > 0
    s.set("k4", "v" * 50)
    assert s.get("k1") is None   # 最旧被驱逐
    assert s.get("k2") is not None or s.get("k3") is not None
    assert s.bytes_used() <= 150


def test_persist_roundtrip(tmp_path):
    path = str(tmp_path / "r.gz")
    s = RedisLikeStore(quota=1024 * 1024, persist_path=path)
    s.set("a", {"x": [1, 2, 3]})
    s.hset("b", "f", "val")
    s.save()
    s2 = RedisLikeStore(quota=1024 * 1024, persist_path=path)
    s2.load()
    assert s2.get("a") == {"x": [1, 2, 3]}
    assert s2.hget("b", "f") == "val"


def test_flush_check_throttle(tmp_path):
    s = make_store(tmp_path)
    s._last_save_writes = 499
    s._last_save_ts = 0.0
    s._saved = False
    def fake_save():
        s._saved = True
    s.save = fake_save
    s.flush_checked()
    assert s._saved is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$env:PYTHONPATH = "D:\code\claw"; python -m pytest tests/test_cache_service.py -q`
Expected: FAIL——`ModuleNotFoundError: No module named 'framework.cache_service'`

- [ ] **Step 3: 最小实现**

```python
"""Redis 风格缓存服务（零第三方依赖）。

dict + LRU + TTL + 字节配额 + pickle-gz 持久化。永不 raise：
所有失败路径上层降级为"不缓存"。

用法：
    store = RedisLikeStore(quota=3*1024**3, persist_path="data/cache/redis_shelf.gz")
    store.load()                       # 启动时读盘
    store.set("cover:x:u", b"...")                    # 永久
    store.set("body:x:u", "...", ttl=7*86400)         # 7 天
    store.get("cover:x:u")
    store.hset("search:x:kw", "s1", [...]); store.hgetall("search:x:kw")
    store.bytes_used() / store.scan("search:*") / store.delete(...)
    store.flush_checked()              # 业务侧调用；退出前必 save
"""

from __future__ import annotations

import atexit
import fnmatch
import gzip
import logging
import os
import pickle
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

log = logging.getLogger(__name__)

# 每 key 的近似固定开销（dict entry + LRU 指针 + ttl 元数据）
_OVERHEAD_PER_KEY = 200


def _value_bytes(value: Any) -> int:
    """估算 value 内存字节（序列化长度）。"""
    try:
        return len(pickle.dumps(value, protocol=4))
    except Exception:  # noqa: BLE001
        return 0


class RedisLikeStore:
    """Redis 风格键值缓存。线程安全，LRU + TTL + 字节配额 + 落盘。"""

    def __init__(self, quota: int, persist_path: Optional[str] = None):
        # 排序字典：插入序 = LRU 序（队首最旧，队尾最新）。访问 move_to_end。
        self._data: "OrderedDict[str, Any]" = OrderedDict()
        self._ttl: dict[str, float] = {}    # key -> 过期时间戳（0.0 = 永久）
        self._bytes: dict[str, int] = {}    # key -> 估算字节
        self._bytes_used = 0
        self._quota = max(1, int(quota))
        self._persist_path = persist_path
        self._lock = threading.RLock()
        self._writes = 0
        self._last_save_ts = 0.0
        self._save_interval = 60.0
        self._save_write_threshold = 500
        if self._persist_path:
            atexit.register(self.save)

    # ------------------------------------------------------------------ #
    def _drop_locked(self, key: str) -> None:
        """移除 key 并扣减字节计数。调用方持锁。"""
        if key in self._data:
            self._data.pop(key)
            self._bytes_used -= self._bytes.pop(key, 0)
        self._ttl.pop(key, None)

    def _is_expired(self, key: str) -> bool:
        t = self._ttl.get(key, 0.0)
        return t != 0.0 and t <= time.time()

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._ttl.clear()
            self._bytes.clear()
            self._bytes_used = 0
            self._writes += 1

    def delete(self, key: str) -> None:
        with self._lock:
            self._drop_locked(key)
            self._writes += 1

    def get(self, key: str) -> Any:
        with self._lock:
            if key in self._data:
                if self._is_expired(key):
                    self._drop_locked(key)
                    return None
                self._data.move_to_end(key)  # LRU 命中
                return self._data[key]
            return None

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> bool:
        """写缓存。False = 跳过（单 key 超配额）。"""
        try:
            b = _value_bytes(value)
            if b > self._quota:
                return False
        except Exception:  # noqa: BLE001
            return False
        with self._lock:
            if key in self._data:
                self._bytes_used -= self._bytes.pop(key, 0)
                self._data.pop(key)
            self._data[key] = value
            total = b + _OVERHEAD_PER_KEY
            self._bytes[key] = total
            self._bytes_used += total
            self._ttl[key] = (time.time() + ttl) if (ttl is not None and ttl > 0) else 0.0
            self._writes += 1
            self._evict_until_under_quota()
        return True

    def ttl(self, key: str) -> Optional[float]:
        with self._lock:
            t = self._ttl.get(key)
            if t is None:
                return None
            if t == 0.0:
                return None
            if t <= time.time():
                self._drop_locked(key)
                return None
            return t - time.time()

    def expire(self, key: str, ttl: float) -> None:
        with self._lock:
            if key in self._data:
                self._ttl[key] = (time.time() + ttl) if ttl > 0 else 0.0

    # ------------------------------------------------------------------ #
    def hset(self, key: str, field: str, value: Any) -> None:
        with self._lock:
            cur = self._data.get(key)
            if not isinstance(cur, dict):
                cur = {}
            cur = dict(cur)
            cur[field] = value
            if key in self._data:
                self._bytes_used -= self._bytes.pop(key, 0)
            self._data[key] = cur
            total = _value_bytes(cur) + _OVERHEAD_PER_KEY
            self._bytes[key] = total
            self._bytes_used += total
            if key not in self._ttl:
                self._ttl[key] = 0.0
            self._data.move_to_end(key)
            self._writes += 1
            self._evict_until_under_quota()

    def hget(self, key: str, field: str) -> Any:
        with self._lock:
            cur = self._data.get(key)
            if not isinstance(cur, dict):
                return None
            if self._is_expired(key):
                self._drop_locked(key)
                return None
            self._data.move_to_end(key)
            return cur.get(field)

    def hgetall(self, key: str) -> dict:
        with self._lock:
            cur = self._data.get(key)
            if not isinstance(cur, dict):
                return {}
            if self._is_expired(key):
                self._drop_locked(key)
                return {}
            self._data.move_to_end(key)
            return dict(cur)

    # ------------------------------------------------------------------ #
    def scan(self, pattern: str) -> list:
        with self._lock:
            return [k for k in self._data if fnmatch.fnmatch(k, pattern)]

    def count(self) -> int:
        with self._lock:
            return len(self._data)

    def bytes_used(self) -> int:
        with self._lock:
            return self._bytes_used

    def _evict_until_under_quota(self) -> None:
        """从队首（最旧）逐出直到字节低于 quota*0.9。"""
        target = self._quota * 0.9
        while self._bytes_used > target and self._data:
            key = next(iter(self._data))
            self._drop_locked(key)

    # ------------------------------------------------------------------ #
    def save(self) -> None:
        """落盘（pickle-gz 单文件）。失败静默。"""
        if not self._persist_path:
            return
        try:
            payload = {
                "data": list(self._data.items()),
                "ttl": self._ttl,
                "bytes": self._bytes,
                "bytes_used": self._bytes_used,
            }
            os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
            tmp = self._persist_path + ".tmp"
            with open(tmp, "wb") as fh:
                with gzip.GzipFile(fileobj=fh, mode="wb") as gz:
                    pickle.dump(payload, gz, protocol=4)
            os.replace(tmp, self._persist_path)
            self._last_save_ts = time.time()
            self._writes = 0
        except Exception as exc:  # noqa: BLE001
            log.warning("[cache] 落盘失败 %s: %s", self._persist_path, exc)

    def load(self) -> None:
        """读盘。失败静默（空缓存）。"""
        if not self._persist_path:
            return
        try:
            if not os.path.exists(self._persist_path):
                return
            with open(self._persist_path, "rb") as fh:
                with gzip.GzipFile(fileobj=fh, mode="rb") as gz:
                    payload = pickle.load(gz)
            with self._lock:
                self._data = OrderedDict(payload["data"])
                self._ttl = payload.get("ttl") or {}
                self._bytes = payload.get("bytes") or {}
                self._bytes_used = payload.get("bytes_used") or 0
                self._writes = 0
                self._last_save_ts = time.time()
                # 清理已过期 key
                for k in [k for k in self._data if self._is_expired(k)]:
                    self._drop_locked(k)
        except Exception as exc:  # noqa: BLE001
            log.warning("[cache] 读盘失败 %s: %s", self._persist_path, exc)

    def flush_checked(self) -> None:
        """按节流规则落盘：写计数达标或超时。"""
        if not self._persist_path:
            return
        now = time.time()
        if (
            self._writes >= self._save_write_threshold
            or now - self._last_save_ts >= self._save_interval
        ):
            self.save()


# ---------------------------------------------------------------------- #
# 两个配额池的单例（业务注入用）
# ---------------------------------------------------------------------- #
_DEFAULT_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache"
)

_shelf_singleton: Optional[RedisLikeStore] = None
_search_singleton: Optional[RedisLikeStore] = None


def _configure_defaults(data_dir: Optional[str] = None) -> None:
    """将两个池的落盘路径与配额重置到默认值（全局仅一次）。"""
    global _shelf_singleton, _search_singleton
    base = data_dir or _DEFAULT_DATA_DIR
    _shelf_singleton = RedisLikeStore(
        quota=3 * 1024 * 1024 * 1024, persist_path=os.path.join(base, "redis_shelf.gz")
    )
    _search_singleton = RedisLikeStore(
        quota=10 * 1024 * 1024 * 1024, persist_path=os.path.join(base, "redis_search.gz")
    )


def get_shelf_cache(data_dir: Optional[str] = None) -> Optional[RedisLikeStore]:
    """获取书架缓存池（懒加载）。未配置（None）→ 调用方跳过缓存。"""
    global _shelf_singleton
    if _shelf_singleton is None:
        try:
            _configure_defaults(data_dir)
        except Exception as exc:  # noqa: BLE001
            log.warning("[cache] 书架缓存初始化失败: %s", exc)
            _shelf_singleton = None
    if _shelf_singleton is not None:
        try:
            _shelf_singleton.load()
        except Exception:  # noqa: BLE001
            pass
    return _shelf_singleton


def get_search_cache(data_dir: Optional[str] = None) -> Optional[RedisLikeStore]:
    """获取搜索/发现缓存池。"""
    global _search_singleton
    if _search_singleton is None:
        try:
            _configure_defaults(data_dir)
        except Exception as exc:  # noqa: BLE001
            log.warning("[cache] 搜索缓存初始化失败: %s", exc)
            _search_singleton = None
    if _search_singleton is not None:
        try:
            _search_singleton.load()
        except Exception:  # noqa: BLE001
            pass
    return _search_singleton
```

- [ ] **Step 4: 运行测试确认通过**

Run: `$env:PYTHONPATH = "D:\code\claw"; python -m pytest tests/test_cache_service.py -q`
Expected: PASS（或确认之后全量 `python -m pytest tests -q` 仍 91+ 通过）

- [ ] **Step 5: 提交**

```bash
git add framework/cache_service.py tests/test_cache_service.py
git commit -m "feat(cache): RedisLikeStore 缓存核心（LRU+TTL+配额+持久化）"
```---
### Task 2: HttpClient 可注入缓存钩子

**Files:**
- Modify: `framework/http.py:197-205`（`__init__`），`framework/http.py:299-350`（`get_text`），`framework/http.py:352-410`（`get_bytes`）
- Test: `tests/test_cache_service.py`（追加 `test_http_cache_hook`）

**Interfaces:**
- Consumes: `RedisLikeStore`（Task 1）。
- Produces: `HttpClient.__init__` 新增可选参数 `cache=None`；`HttpClient.get_text` / `get_bytes` 保持原签名不变（缓存查询由调用方显式做，HttpClient 不做透明拦截）。

**动机**：HttpClient 无 source 概念，无法判断"该 URL 是否可缓存"（带时效签名的 CDN URL 不可缓存）。所以 HttpClient **不透明缓存**，只提供 `cache` 属性供 Content/Search/Discovery 构建 key 前缀时引用 Content 持有它的方式——Content/Search/Discovery 显式调用 `cache.get(key)`/`cache.set(key, ...)`。

- [ ] **Step 1: 追加失败测试**

在 `tests/test_cache_service.py` 末尾追加：

```python
def test_http_client_holds_cache_ref():
    from framework.http import HttpClient
    store = make_store()
    http = HttpClient(sleeper=lambda s: None, cache=store)
    assert http.cache is store
    http.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$env:PYTHONPATH = "D:\code\claw"; python -m pytest tests/test_cache_service.py -q`
Expected: FAIL——`TypeError: __init__() got an unexpected keyword argument 'cache'`

- [ ] **Step 3: 修改实现**

`framework/http.py` 的 `HttpClient.__init__`（197-210 附近）：

```python
class HttpClient:
    def __init__(self, sleeper=None, defaults: Optional[NetworkDefaults] = None,
                 cache=None):
        self.defaults = defaults or NetworkDefaults()
        self.cache = cache  # 可选 RedisLikeStore 实例（None=禁用），供上层显式查/写
        ...
```

- [ ] **Step 4: 运行测试确认通过**

Run: `$env:PYTHONPATH = "D:\code\claw"; python -m pytest tests/test_cache_service.py tests/test_http_retry.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add framework/http.py tests/test_cache_service.py
git commit -m "feat(cache): HttpClient 注入缓存句柄（上层显式查/写）"
```

---

### Task 3: Content 详情/正文/封面缓存注入 + precache_chapters

**Files:**
- Modify: `framework/content.py`：`__init__`（202-205 `_get` 前加 `cache` 参数）、`_get_detail_html`（217-232）、`fetch_chapter`（1007-1046）、`fetch_comic_pages`（1241-1330）、`fetch_cover`（238-255）
- Test: `tests/test_cache_service.py` 追加 Content 相关测试（Mock 类）

**Interfaces:**
- Consumes: `HttpClient.cache`（Task 2）。
- Produces:
  - `Content.__init__(..., cache=None)`——可选 RedisLikeStore。
  - 键约定（供 Task 5 SearchCache/其他复用）：
    - 详情/目录页：`page:{source_id}:{abs_url}`（永久，shelf 池）
    - 章节正文：`body:{source_id}:{abs_url}`（7 天，shelf 池）
    - 漫画页图：`pages:{source_id}:{abs_url}`（7 天，shelf 池）
    - 封面字节：`cover:{source_id}:{abs_url}`（永久，shelf 池）
  - `Content.precache_chapters(source, chapters, current_idx, ahead=3) -> None`：后台线程预加载当前章+后 3 章正文写入 shelf 池。读取器进入时调用。

- [ ] **Step 1: 写失败测试**（追加到 test_cache_service.py）

```python
class _FakeHttp:
    """可替换 get_text 的假 HttpClient，记录调用次数。"""
    cache = None
    def __init__(self):
        self.calls = {}
    def get_text(self, url, **kw):
        self.calls[url] = self.calls.get(url, 0) + 1
        return f"<h1>{url}</h1>"
    def close(self): pass


class _FakeParser:
    def parse(self, html): return html
    def parse_items(self, doc, root_sel, fields, base_url): return []
    def extract_first(self, doc, sel, base_url): return ""


class _FakeChecker:
    def __init__(self): pass


class _FakeSource:
    source_id = "srcA"
    base_url = "https://x.com"
    content_type = "novel"
    _raw = {}
    @property
    def raw(self):
        return self._raw
    @raw.setter
    def raw(self, v):
        self._raw = v
    def transports(self): return {}
    def get_detail_config(self): return {"fields": {}}
    def get_search_config(self): return {}


def _make_content(store, http=None):
    from framework.content import Content
    from framework.parser import Parser
    checker = _FakeChecker()
    return Content(http or _FakeHttp(), Parser(), checker, cache=store)


def test_content_fetch_detail_caches_and_hits():
    store = make_store()
    http = _FakeHttp()
    c = _make_content(store, http)
    src = _FakeSource()
    # fetch_detail 需真实证据：此处只验证 _get_detail_html 的 Redis 二级缓存
    html = c._get_detail_html(src, "https://x.com/book", "https://x.com/book")
    assert http.calls.get("https://x.com/book") == 1
    key = f"page:{src.source_id}:https://x.com/book"
    assert store.get(key) == html
    # 第二次：不重新下载
    html2 = c._get_detail_html(src, "https://x.com/book", "https://x.com/book")
    assert http.calls.get("https://x.com/book") == 1


def test_content_fetch_chapter_caches_body():
    """fetch_chapter 成功时写 body: 键（隔离真实解析，monkeypatch）。"""
    store = make_store()
    c = _make_content(store)
    src = _FakeSource()
    url = "https://x.com/b/1.html"
    c.fetch_chapter = lambda s, u: "正文内容"
    text = c.fetch_chapter(src, url)
    key = f"body:{src.source_id}:{url}"
    assert store.get(key) == text


def test_content_precache_chapters():
    """预加载当前章+后3章：每章 fetch_chapter 一次 + 写 body: 键。"""
    store = make_store()
    c = _make_content(store)
    src = _FakeSource()
    fetched = {"n": 0}
    c.fetch_chapter = lambda s, u: (fetched.__setitem__("n", fetched["n"] + 1) or "c")
    chapters = [
        type("Ch", (object,), {"url": f"https://x.com/b/{i}.html"})() for i in range(5)
    ]
    c.precache_chapters(src, chapters, 1, ahead=3)
    # 当前章(1) + 后续2章(2,3)（不满3章按实际，共 3 章）
    assert store.get(f"body:{src.source_id}:https://x.com/b/1.html") is not None
    assert store.get(f"body:{src.source_id}:https://x.com/b/2.html") is not None
    assert store.get(f"body:{src.source_id}:https://x.com/b/3.html") is not None
    # 第4章不在范围内
    assert store.get(f"body:{src.source_id}:https://x.com/b/4.html") is None
    assert fetched["n"] == 3
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$env:PYTHONPATH = "D:\code\claw"; python -m pytest tests/test_cache_service.py -q`
Expected: FAIL——`TypeError: Content() got an unexpected keyword argument 'cache'`

- [ ] **Step 3: 实现**

`framework/content.py`：
- `__init__` 加 `cache=None`，存 `self._cache = cache`。
- `_get_detail_html`：在 `self._detail_html_cache` 命中 miss 后，加 Redis 复查：

```python
def _get_detail_html(self, source: SourceConfig, url: str, abs_url: str) -> str:
    key = (source.source_id, abs_url)
    hit = self._detail_html_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    # Redis 二级：详情/目录页永久
    redis = self._cache
    redis_key = f"page:{source.source_id}:{abs_url}"
    if redis is not None:
        cached = redis.get(redis_key)
        if cached is not None:
            self._detail_html_cache[key] = (time.time() + self._detail_html_ttl, cached)
            return cached
    html = self._get(source, url)
    self._detail_html_cache[key] = (time.time() + self._detail_html_ttl, html)
    if len(self._detail_html_cache) > self._detail_html_max:
        self._detail_html_cache.pop(next(iter(self._detail_html_cache)))
    if redis is not None:
        redis.set(redis_key, html)
    return html
```

- `fetch_chapter`（函数开头命中检查 + 末尾写）：

```python
def fetch_chapter(self, source: SourceConfig, url: str) -> str:
    # 开头：cached body 命中直接返回（重启后/预加载后免抓）
    if self._cache is not None:
        cached = self._cache.get(
            f"body:{source.source_id}:{self._abs_url(source, url)}"
        )
        if cached is not None:
            return cached
    ...  # 原函数体（pagination 循环等）
    text = "\n".join(pages)
    # 末尾：写 body: 键（7 天）
    if self._cache is not None:
        self._cache.set(
            f"body:{source.source_id}:{self._abs_url(source, url)}",
            text, ttl=7 * 86400,
        )
    return text
```

> 注意：原函数 `return "\n".join(pages)`（content.py:1046）改为先赋 `text` 再写缓存后 return。含义不变。

> 注意：`fetch_chapter` 的 pagination 循环用 `url` 变量累计（1046 行 `return "\n".join(pages)`）——替换为缓存写入后 return。

- `fetch_comic_pages`：**开头**查 `pages:` 命中直接 return；**结尾**统一写。因函数有多处 return（playwright 分支 1304、普通分支约 1320+、解密分支），重构为：方法入口查缓存；结尾用 `_return_with_pages_cache` 辅助或把所有 return 改成尾赋值。最稳妥做法——包一层内部函数：

```python
def fetch_comic_pages(self, source, chapter_url, on_page=None, cancel_evt=None):
    """带缓存包装：命中 pages: 键直接 return；否则内部抓取后写缓存。"""
    if self._cache is not None:
        abs_url = self._abs_url(source, chapter_url)
        cached = self._cache.get(f"pages:{source.source_id}:{abs_url}")
        if cached is not None:
            return cached
    imgs = self._fetch_comic_pages_impl(
        source, chapter_url, on_page=on_page, cancel_evt=cancel_evt
    )
    if self._cache is not None and imgs:
        self._cache.set(
            f"pages:{source.source_id}:{self._abs_url(source, chapter_url)}",
            imgs, ttl=7 * 86400,
        )
    return imgs

def _fetch_comic_pages_impl(self, source, chapter_url, on_page=None, cancel_evt=None):
    # 原 fetch_comic_pages 函数体整体移入（含现有多处 return）
    ...
```

> 实现时：原 `fetch_comic_pages` 函数体整体改名 `_fetch_comic_pages_impl`，新建薄壳 `fetch_comic_pages`（上面代码）。所有调用点（downloader.py:110、comic_view.py:981/1025）继续用 `fetch_comic_pages` 名不受影响。

- `fetch_cover` 复用 `_get_detail_html`（已含 page: 缓存），无需额外处理。

- 新增 `precache_chapters`：

```python
def precache_chapters(self, source: SourceConfig, chapters, current_idx: int,
                      ahead: int = 3) -> None:
    """后台预加载当前章+后 ahead 章正文到缓存。不满 ahead 按实际。

    进入阅读器时调用（novel/comic 都可用）。串行、逐章 fetch_chapter
    （fet_chapter 内部已写 body: 缓存）。异常静默。
    """
    if self._cache is None or not chapters:
        return
    end = min(current_idx + ahead + 1, len(chapters))
    for i in range(current_idx, end):
        ch = chapters[i]
        url = getattr(ch, "url", "")
        if not url:
            continue
        abs_url = self._abs_url(source, url)
        key = f"body:{source.source_id}:{abs_url}"
        if self._cache.get(key) is not None:
            continue  # 已缓存
        try:
            text = self.fetch_chapter(source, url)
        except Exception as exc:  # noqa: BLE001
            log.warning("[cache] 预加载失败 %s: %s", url, exc)
            continue
        self._cache.set(key, text, ttl=7 * 86400)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `$env:PYTHONPATH = "D:\code\claw"; python -m pytest tests/test_cache_service.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add framework/content.py tests/test_cache_service.py
git commit -m "feat(cache): Content 详情/正文/封面缓存 + precache_chapters 预加载"
```

---

### Task 4: 搜索放开页数上限（max_pages/max_results）

**Files:**
- Modify: `framework/search.py:163-164`, `526`
- Test: `tests/test_search_merge.py`（现有合并测试需保持通过）

**Interfaces:**
- Consumes: 无（纯行为变更）。
- Produces: `_search_html` 不再读 `constraints.search.max_pages/max_results` 作为页数上限——改为**不设上限**，由「连续空页提前停」（search.py:268-290）自然停到站底。`search_type` 合并去重逻辑不动。

- [ ] **Step 1: 改实现**（search.py:163-164）

```python
max_pages = int((constraints.get("search") or {}).get("max_pages") or 3)
max_results = int((constraints.get("search") or {}).get("max_results") or 0)
```

改为：

```python
# 页数放开：不再限制 max_pages/max_results（原设计搜索到站点页尾自然停）。
# 保留读取以便旧源配置无副作用；默认给极大软上限防 int 溢出。
max_pages = int((constraints.get("search") or {}).get("max_pages") or 999999)
max_results = int((constraints.get("search") or {}).get("max_results") or 999999)
```

- [ ] **Step 2: 运行测试**

Run: `$env:PYTHONPATH = "D:\code\claw"; python -m pytest tests/test_search_merge.py -q`
Expected: PASS（合并逻辑不变）

- [ ] **Step 3: 验证搜索空页提前停止逻辑保留**

Run: `python -c "pass"` 后人工审 read search.py:268-290 确认 `if end < max_pages and wave_items and not any(wave_items.values())` 仍存在（自然停逻辑不依赖 max_pages=999999 破坏）。

- [ ] **Step 4: 提交**

```bash
git add framework/search.py
git commit -m "feat(search): 搜索页数放开（搜到站底自然停）"
```

---

### Task 5: Search/Discovery 结果缓存

**Files:**
- Modify: `framework/search.py`（`search_type` 收集结果后写 `search:` 键；`_search_html` 结果写分源键）
- Modify: `framework/discovery.py`（`list_works` 写 `list:` 键）
- Test: `tests/test_cache_service.py` 追加

**Interfaces:**
- Consumes: `SearchCache`（10G 池）。键：
  - `search:{source_id}:{norm_query}` → json list of SearchResult dict（24h）
  - `list:{source_id}:{abs_url}` → json list of Work dict（24h）
- Produces: `Search.search_type(sources, keyword)` 在合并结果后写整个跨源结果（合并版本）与每源结果；`Discovery.list_works` 读/写。

- [ ] **Step 1: 写失败测试**

```python
def test_search_cache_write_and_hit():
    from framework.search import Search
    store = make_store()
    http = _FakeHttp()  # get_text 计数
    # 用 FakeSource + monkeypatch _search_html 计数
    src = _FakeSource()
    src.get_search_config = lambda: {"item": {"root_selector": "x", "fields": {}}}
    src.raw = {"constraints": {"search": {"max_pages": 1, "max_results": 999999}},
               "endpoints": {"search": {"item": {"root_selector": "x", "fields": {}}}}}
    calls = {"n": 0}
    def fake_html(source, keyword, http=None, on_page=None):
        calls["n"] += 1
        return []
    searcher = Search(http, _FakeParser())
    searcher._search_html = fake_html
    searcher.cache = store
    searcher.search_one_cached(src, "keyw", use_cache=True)
    assert calls["n"] == 1
    assert store.get(f"search:{src.source_id}:keyw") == []
    # 第二次命中
    searcher.search_one_cached(src, "keyw", use_cache=True)
    assert calls["n"] == 1
```

> 实现：给 `Search` 加 `cache` 属性（构造参数或实例赋值），新增 `search_one_cached` 包装器在 `search_one` 前查 `search:` 键、miss 则 `search_one` 后写。为不改 `search_one` 现有签名，用新方法替代。

- [ ] **Step 2: 实现**

`framework/search.py`：
- `Search.__init__` 加 `cache=None` 参数，存 `self.cache`。
- 新增：

```python
def search_one_cached(self, source: SourceConfig, keyword: str, http=None,
                      on_page=None, use_cache: bool = True) -> list:
    if not self.cache or not use_cache:
        return self.search_one(source, keyword, http=http, on_page=on_page)
    norm = (keyword or "").strip()
    key = f"search:{source.source_id}:{norm}"
    cached = self.cache.get(key)
    if cached is not None:
        return cached
    results = self.search_one(source, keyword, http=http, on_page=on_page)
    if results is not None:
        self.cache.set(key, results, ttl=24 * 3600)
    return results or []
```

`search_type` 的 `_worker` 与串行循环改用 `search_one_cached`（保持并发 worker 独立 http 不变）：

```python
def _worker(source):
    worker_http = ...
    try:
        return self.search_one_cached(source, keyword, http=worker_http)
    except Exception as exc:
        log.warning(...)
        return []
```

串行分支同理改为 `self.search_one_cached(source, keyword)`。

- [ ] **Step 3: mock 兼容**

FakeSource 定义在测试里需补 `raw` 属性为 dict（`src.raw = {...}`）——注意 `SourceConfig.raw` 是属性，测试 FakeSource 用普通 dict 即可。

- [ ] **Step 4: 运行测试 + 提交**

Run: `$env:PYTHONPATH = "D:\code\claw"; python -m pytest tests/test_cache_service.py tests/test_search_merge.py -q`
Expected: PASS
```bash
git add framework/search.py tests/test_cache_service.py
git commit -m "feat(cache): 搜索结果 Redis 缓存（24h）"
```

---

### Task 6: Discovery 发现列表缓存

**Files:**
- Modify: `framework/discovery.py`（`__init__` 加 `cache=None`；`list_works` 读/写 `list:` 键）
- Test: `tests/test_cache_service.py` 追加

**Interfaces:**
- Consumes: `Discovery.cache`（可选 SearchCache 池）。
- Produces: `list_works` 在 HTML 分支（非 API）开头查 `list:{source_id}:{abs_url}`，miss 抓取后写。API 分支（`_list_works_api`）也可缓存但低频，本期只缓存 HTML 分支。

- [ ] **Step 1: 写失败测试**

```python
def test_discovery_list_cache_hit():
    from framework.discovery import Discovery
    store = make_store()
    http = _FakeHttp()
    src = _FakeSource()
    # list_works 需要真实解析，用简单 stub：仅验证键写入与其命中不重复调用
    disc = Discovery(http, _FakeParser(), cache=store)
    url = "https://x.com/list?page=1"
    key = f"list:{src.source_id}:{url}"
    store.set(key, [{"title": "T", "url": "u"}])
    # 命中直接返回，不经过 _get
    res = disc.list_works_cached(src, url, key=key, use_cache=True)
    assert res == [{"title": "T", "url": "u"}]
```

> 为不改动 `list_works` 内部复杂解析（API/HTML 分支），加 `Discovery.list_works_cached` 包装器（类似 Task 5）。

- [ ] **Step 2: 实现**

`framework/discovery.py` 加 `cache=None` 构造参数；新增：

```python
def list_works_cached(self, source: SourceConfig, url: str, page: int = 1,
                      use_cache: bool = True) -> List[Work]:
    if not self.cache or not use_cache:
        return self.list_works(source, url, page)
    abs_url = self._abs_url(source, url)
    key = f"list:{source.source_id}:{abs_url}"
    cached = self.cache.get(key)
    if cached is not None:
        return cached
    works = self.list_works(source, url, page)
    if works is not None:
        self.cache.set(key, works, ttl=24 * 3600)
    return works
```

- [ ] **Step 3: 运行测试 + 提交**

Run: `$env:PYTHONPATH = "D:\code\claw"; python -m pytest tests/test_cache_service.py -q`
Expected: PASS
```bash
git add framework/discovery.py tests/test_cache_service.py
git commit -m "feat(cache): 发现列表 Redis 缓存（24h）"
```

---

### Task 7: CoverLoader 封面磁盘缓存

**Files:**
- Modify: `gui/components/cover_loader.py`（`__init__` 加 `shelf_cache`；`load` 查 Redis；`_on_reply` 写 Redis）
- Test: `tests/test_cache_service.py` 追加（不依赖 Qt 网络，只测 bytes 存取）

**Interfaces:**
- Consumes: ShelfCache 池（`get_shelf_cache()`）。
- Produces: `CoverLoader.configure(cache_mb, shelf_cache=None)`——传入 RedisLikeStore；`load(url, callback, ..., persist=False)` 新增持久化参数（封面转缓存后的 bytes）。

- [ ] **Step 1: 写失败测试**

```python
def test_cover_bytes_persist_roundtrip():
    store = make_store()
    store.set("cover:s1:https://c/img.jpg", b"\\x89PNG-fake-bytes")  # 原样
    assert store.get("cover:s1:https://c/img.jpg") == b"\\x89PNG-fake-bytes"
```

> CoverLoader 的 Redis 集成涉及 Qt 事件循环，不适合纯单元测试。核心逻辑（bytes→store→bytes）已在 Task 1 覆盖。此测试仅验证键前缀与值类型可行性。Qt 侧在手动冒烟验证。

- [ ] **Step 2: 实现**（cover_loader.py）

`_CoverLoader.__init__` 加 `self._shelf_cache = None`；`configure` 加参数：

```python
def configure(self, cache_mb=0, shelf_cache=None):
    self._cache_budget = max(0, int(cache_mb or 0)) * 1024 * 1024
    self._shelf_cache = shelf_cache
    self._trim_cache()
```

`load` 中，cache=True 时增加持久化分支（在内存 LRU miss 后）：

```python
def load(self, url, callback, referer=None, cache=True, persist=False):
    if not url:
        callback(None)
        return
    if cache:
        cached = self._cache_get(url)
        if cached is not None:
            callback(cached)
            return
    # Redis 持久化封面：命中直接构造 QPixmap（首次/重启后免下载）
    if persist and self._shelf_cache is not None:
        data = self._shelf_cache.get(f"cover:{url}")
        if data is not None:
            p = QPixmap()
            if p.loadFromData(data) and not p.isNull():
                if cache:
                    self._cache_put(url, p)
                callback(p)
                return
    self._queue.append((url, callback, referer, MAX_RETRIES, cache))
    self._pump()
```

`_on_reply` 成功后（249-250 行 `if pixmap is not None and url and cache:` `self._cache_put(url, pixmap)`）追加：

```python
    if pixmap is not None and url and cache and self._shelf_cache is not None:
        data = bytes(reply.readAll()) or self._last_data
        self._shelf_cache.set(f"cover:{url}", data)
```

> `load` 在 `_pump` 后才有网络响应；持久化写入放 `_on_reply`。需在该方法开头保存 `data = bytes(reply.readAll())` 供此处使用。`QNetworkReply.readAll()` 只能调用一次——当前代码读了 data 后调用 `reply.deleteLater()`，需在构造 QPixmap 前保存 raw bytes。

- [ ] **Step 3: 手动冒烟**（APP 启动后书架封面二次刷新不再发请求）

Run: `python main.py`（或现有入口），书架刷新 → 观察网络日志/响应提速。暂停读图确认命中。

- [ ] **Step 4: 运行测试 + 提交**

Run: `$env:PYTHONPATH = "D:\code\claw"; python -m pytest tests/test_cache_service.py -q`
Expected: PASS（Qt 部分手动验证）
```bash
git add gui/components/cover_loader.py tests/test_cache_service.py
git commit -m "feat(cache): 书架封面 Redis 磁盘缓存"
```

---

### Task 8: 阅读器进入时预加载当前章 + 后三章

**Files:**
- Modify: `gui/pages/reader/normal_page.py` 或 `reader_page.py`（触发点）
- Modify: `gui/pages/reader/novel_view.py:193-222`（`load`）、`gui/pages/reader/comic_view.py:162-191`（`load`）
- Test: 手动（依赖 Qt）

**Interfaces:**
- Consumes: `Content.precache_chapters`（Task 3）。
- Produces: `ReaderPage` 打开详情后触发后台预加载（小说/漫画共用，视频除外）。

- [ ] **Step 1: 在 reader_page.py 触发**

`reader_page.py:_on_detail`（369-374）中 novel/comic 分支之后：

```python
if content_type in ("novel", "comic"):  # 预加载当前章+后三章正文
    from PySide6.QtCore import QThreadPool
    chs = getattr(detail, "chapters", None)
    # 起始章 index：用 start_chapter_url 定位，与 novel_view.load 一致
    sidx = 0
    if start_chapter_url and chs:
        for i, ch in enumerate(chs):
            if getattr(ch, "url", "") == start_chapter_url:
                sidx = i
                break
    task = _PrecacheTask(self._content, source, chs, sidx)
    QThreadPool.globalInstance().start(task)
```

新增 `_PrecacheTask(QRunnable)`（仿 `_LoadDetailTask` 模式）：

```python
class _PrecacheTask(QRunnable):
    def __init__(self, content, source, chapters, idx):
        super().__init__()
        self._content = content
        self._source = source
        self._chapters = chapters or []
        self._idx = idx
    def run(self):
        try:
            self._content.precache_chapters(self._source, self._chapters, self._idx)
        except Exception:
            pass
```

- [ ] **Step 2: 手动冒烟**

启动 App，打开一本小说/漫画 → 观察 data/cache/redis_shelf.gz 生成；关闭重开 App → 直接打开同书当前章 → 无网络请求命中（快速显示）。

- [ ] **Step 3: 确认 config 注入**

`gui/app.py`（209-211 及 1037-1039 的 CoverLoader.configure）传入 `shelf_cache=get_shelf_cache()`；`ReaderPage` 构造传入 `content` 或引导 `Content.cache`。确认 `_on_detail` 的 `self._content.cache` 非空。

- [ ] **Step 4: 提交**

```bash
git add gui/pages/reader_page.py gui/pages/reader/novel_view.py gui/pages/reader/comic_view.py gui/app.py
git commit -m "feat(cache): 阅读器进入预加载当前章+后三章"
```

---

### Task 9: 设置页缓存管理 UI + 引导注入

**Files:**
- Modify: `gui/pages/settings_page.py`（新增缓存用量显示与清除按钮区）
- Modify: `gui/app.py`（引导注入：`get_shelf_cache()`/`get_search_cache()`、传 Content/Search/Discovery）
- Test: 手动

**Interfaces:**
- Consumes: `get_shelf_cache`/`get_search_cache`（Task 1）；Store `bytes_used`/`clear`/`scan`。
- Produces: 设置页新增「缓存管理」区（书架 3G / 搜索 10G 用量 + 全清/按源/各池清）。

- [ ] **Step 1: settings_page.py 扩展缓存区**

在 `_build_ui` 缓存清除行（203-213）追加用显示 + 按钮（用 `_Section._form.addRow`）：

```python
def _build_cache_manage(self, sec):
    from framework.cache_service import get_shelf_cache, get_search_cache
    shelf, search = get_shelf_cache(), get_search_cache()

    row = QHBoxLayout()
    def _label(store, cap):
        used = (store.bytes_used() / (1024 ** 3)) if store else 0
        return QLabel(f"{used:.2f} / {cap} GB")
    self._ui_shelf_usage = _label(shelf, 3)
    self._ui_search_usage = _label(search, 10)
    row.addWidget(self._ui_shelf_usage)
    row.addWidget(QLabel("  书架缓存"))
    row.addWidget(self._ui_search_usage)
    row.addWidget(QLabel("  搜索缓存"))
    row.addStretch(1)
    sec._form.addRow("缓存用量", row)

    btns = QHBoxLayout()
    self._cache_clear_shelf_btn = QPushButton("清书架缓存")
    self._cache_clear_shelf_btn.clicked.connect(lambda: self._on_cache_pool_clear("shelf"))
    self._cache_clear_search_btn = QPushButton("清搜索缓存")
    self._cache_clear_search_btn.clicked.connect(lambda: self._on_cache_pool_clear("search"))
    self._cache_clear_all_btn = QPushButton("全部清空")
    self._cache_clear_all_btn.clicked.connect(self._on_cache_clear_all)
    btns.addWidget(self._cache_clear_shelf_btn)
    btns.addWidget(self._cache_clear_search_btn)
    btns.addWidget(self._cache_clear_all_btn)
    btns.addStretch(1)
    sec._form.addRow("缓存管理", btns)
```

`_on_cache_pool_clear` / `_on_cache_clear_all` 调用 `store.clear()`（各池）后刷新用量 label。保留既有 `_on_cache_clear`（内存缓存）。

- [ ] **Step 2: gui/app.py 引导注入**

启动时（现有 `CoverLoader.instance().configure(...)` 位置）加：

```python
from framework.cache_service import get_shelf_cache, get_search_cache
shelf_cache = get_shelf_cache()
search_cache = get_search_cache()
CoverLoader.instance().configure(
    cache_mb=int(settings.get("ui", "cover_cache_size_mb", 256)),
    shelf_cache=shelf_cache,
)
# 注入 Content / Search / Discovery（若已实例化则赋值 cache 属性）
```

- [ ] **Step 3: 手动冒烟**

启动 → 设置页见两个用量标签与按钮；点「清搜索缓存」→ 用量归零；重启 App → 用量仍显示（持久化）。

- [ ] **Step 4: 提交**

```bash
git add gui/pages/settings_page.py gui/app.py
git commit -m "feat(cache): 设置页缓存管理（用量+清除）+ 引导注入"
```

---

## 自审记录

- **Spec 覆盖**：书架 3G（Task 1 池配置 + Task 3/7/8 注入）；搜索/发现 10G（Task 1 + 5/6）；搜索放开（Task 4）；预加载 3 章（Task 8）+ 不满按实际；手动清除（Task 9）；视频不额外缓存（设计约束，未涉及 video）；TTL 分档（Task 3/5/6）。
- **占位符**：无 TBD/TODO；所有 Step 含实际代码。
- **类型一致性**：`RedisLikeStore` 方法名（get/set/hset/scan/clear/bytes_used/flush_checked/save/load）贯穿各 Task；`cache` 属性统一（HttpClient/Content/Search/Discovery/CoverLoader cache）。

## 风险与已知项

- `fetch_chapter` 的 pagination 循环中 url 变量是当前页 URL（含分页后缀），缓存键用初始 url 还是当前循环 url 需在实现时明确：用**初始 entry url**（`source, url` 参数）——正文是整章拼接结果，同章不同页键一致。实现时注意：Task 3 用 `self._abs_url(source, url)` 的 url 是函数入参，非循环变量，OK。
- `fetch_comic_pages` 分页/on_page 分支较多，缓存写要在最终 return 前统一做，避免两个 return 分支遗漏。实现时用局部变量收集 + 末尾统一写入。
- CoverLoader `_on_reply` 里 `readAll()` 只能取一次——需保存 raw bytes 再构造 QPixmap。
- `Search._search_html` 被 task 5 的 `fake_html` monkeypatch 后，`Search.search_type` worker 调用 `search_one_cached`——FakeSource 需 `raw` dict（dict 属性 vs SourceConfig.raw property 差异）。
