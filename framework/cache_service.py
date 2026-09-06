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


def _ensure_loaded(store: Optional[RedisLikeStore]) -> Optional[RedisLikeStore]:
    """首次访问时读盘（幂等）。"""
    if store is not None and not getattr(store, "_loaded", False):
        try:
            store.load()
            store._loaded = True
        except Exception:  # noqa: BLE001
            pass
    return store


def get_shelf_cache(data_dir: Optional[str] = None) -> Optional[RedisLikeStore]:
    """获取书架缓存池（懒加载）。初始化失败返回 None → 调用方跳过缓存。"""
    global _shelf_singleton
    if _shelf_singleton is None:
        try:
            _configure_defaults(data_dir)
        except Exception as exc:  # noqa: BLE001
            log.warning("[cache] 书架缓存初始化失败: %s", exc)
            _shelf_singleton = None
    return _ensure_loaded(_shelf_singleton)


def get_search_cache(data_dir: Optional[str] = None) -> Optional[RedisLikeStore]:
    """获取搜索/发现缓存池。"""
    global _search_singleton
    if _search_singleton is None:
        try:
            _configure_defaults(data_dir)
        except Exception as exc:  # noqa: BLE001
            log.warning("[cache] 搜索缓存初始化失败: %s", exc)
            _search_singleton = None
    return _ensure_loaded(_search_singleton)