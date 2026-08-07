"""代理 IP 池（proxy_pool.py）。

反爬时更换 IP 继续爬取：多线程爬取共用一个池，next() 取当前代理、
mark_bad() 把当前代理标记失效并切到下一个、rotate() 手动轮换、
available() 判断是否还有可用代理。请求失败/触发反爬（403/封禁/验证码
特征）时由 HttpClient 换 IP 重试（最多换 max_switches 次）。

代理来源（ProxyPool.from_config 解析 transports.proxy_pool）：
  - list[str]                                  ["http://ip:port", ...]
  - list[dict]                                 [{"proxy": "http://ip:port", "name": "..."}]
  - dict                                       {"proxies": [...], "max_switches": N}
  - dict（指向代理池 JSON 文件）                 {"file": "path/proxies.json", "max_switches": N}
  - str（代理池 JSON 文件路径）                  "path/proxies.json"
代理池 JSON 文件：顶层为 list，或 {"proxies": [...]}。
"""

from __future__ import annotations

import threading
from typing import Any, List, Optional

DEFAULT_MAX_PROXY_SWITCHES = 3  # 换 IP 次数上限（总尝试 = 上限 + 1）


class _ProxyEntry:
    """单个代理：URL + 是否可用（被标记失效后不再轮换到）。"""

    __slots__ = ("url", "name", "alive")

    def __init__(self, url: str, name: str = ""):
        self.url = url
        self.name = name
        self.alive = True


class ProxyPool:
    """线程安全的代理 IP 池（轮换 + 失效标记）。

    语义：
      next()        取当前代理 URL（不推进）；无可用代理返回 None（调用方走直连）。
      mark_bad()    把当前代理标记失效并切换到下一个（返回新当前或 None）。
      rotate()      手动轮换到下一个可用代理（不标记失效）。
      available()   是否还有可用代理。
      reset()       全部恢复可用（冷却期后重新使用）。
    """

    def __init__(
        self,
        proxies: Optional[list] = None,
        *,
        max_switches: int = DEFAULT_MAX_PROXY_SWITCHES,
    ):
        self._lock = threading.RLock()
        entries: List[_ProxyEntry] = []
        for item in proxies or []:
            entry = _parse_proxy_entry(item)
            if entry is not None:
                entries.append(entry)
        self._entries = entries
        self.max_switches = max(_safe_int(max_switches, DEFAULT_MAX_PROXY_SWITCHES), 0)
        self._index = self._first_alive(0) or 0

    # ------------------------------------------------------------------ #
    # 构造辅助
    # ------------------------------------------------------------------ #
    @classmethod
    def from_config(cls, cfg) -> Optional["ProxyPool"]:
        """从 transports.proxy_pool 配置构建。非法/空配置返回 None（调用方当未配置处理）。"""
        proxies, max_switches = _extract_pool_config(cfg)
        if not proxies:
            return None
        return cls(proxies, max_switches=max_switches)

    # ------------------------------------------------------------------ #
    # 取用 / 轮换 / 失效标记
    # ------------------------------------------------------------------ #
    def next(self) -> Optional[str]:
        """取当前代理 URL（不推进）；无可用代理返回 None。"""
        with self._lock:
            entry = self._current_entry()
            return entry.url if entry is not None else None

    def mark_bad(self) -> Optional[str]:
        """把当前代理标记失效并切换到下一个可用代理。

        返回新当前代理 URL；全部失效后返回 None。
        """
        with self._lock:
            current = self._current_entry()
            if current is not None:
                current.alive = False
            nxt = self._first_alive(self._index + 1)
            self._index = nxt if nxt is not None else self._index
            entry = self._current_entry()
            return entry.url if entry is not None else None

    def rotate(self) -> Optional[str]:
        """手动轮换到下一个可用代理（不标记失效）。"""
        with self._lock:
            nxt = self._first_alive(self._index + 1)
            if nxt is not None:
                self._index = nxt
            entry = self._current_entry()
            return entry.url if entry is not None else None

    def available(self) -> bool:
        """是否还有可用代理。"""
        with self._lock:
            return any(e.alive for e in self._entries)

    def reset(self) -> None:
        """重置：全部代理恢复可用，从第一个重新开始。"""
        with self._lock:
            for e in self._entries:
                e.alive = True
            self._index = self._first_alive(0) or 0

    def bad_count(self) -> int:
        """已失效代理数（调试/展示用）。"""
        with self._lock:
            return sum(1 for e in self._entries if not e.alive)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"<ProxyPool size={len(self._entries)} "
                f"alive={sum(1 for e in self._entries if e.alive)} "
                f"max_switches={self.max_switches}>"
            )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _current_entry(self) -> Optional[_ProxyEntry]:
        if not self._entries:
            return None
        entry = self._entries[self._index % len(self._entries)]
        return entry if entry.alive else None

    def _first_alive(self, start: int) -> Optional[int]:
        """从 start 起（模 len）找第一个可用代理下标；无可用返回 None。"""
        n = len(self._entries)
        if n == 0:
            return None
        for offset in range(n):
            idx = (start + offset) % n
            if self._entries[idx].alive:
                return idx
        return None


# ---------------------------------------------------------------------- #
# 配置解析
# ---------------------------------------------------------------------- #
def _parse_proxy_entry(item: Any) -> Optional[_ProxyEntry]:
    """str → 直接当 URL；dict → 取 proxy/url 键（可带 name）。非法返回 None。"""
    if isinstance(item, str):
        url = item.strip()
        return _ProxyEntry(url) if url else None
    if isinstance(item, dict):
        url = item.get("proxy") or item.get("url") or item.get("addr") or ""
        if not isinstance(url, str) or not url.strip():
            return None
        return _ProxyEntry(url.strip(), name=str(item.get("name") or ""))
    return None


def _extract_pool_config(cfg) -> tuple:
    """把 transports.proxy_pool 配置规整为 (proxies_list, max_switches)。"""
    max_switches = DEFAULT_MAX_PROXY_SWITCHES
    if isinstance(cfg, str):
        return _load_proxy_file(cfg), max_switches
    if isinstance(cfg, dict):
        fpath = cfg.get("file") or cfg.get("path")
        max_switches = _safe_int(cfg.get("max_switches"), max_switches)
        if fpath:
            return _load_proxy_file(fpath), max_switches
        proxies = cfg.get("proxies") or []
        return proxies, max_switches
    if isinstance(cfg, list):
        return cfg, max_switches
    return [], max_switches


def _load_proxy_file(path: str) -> List[Any]:
    """读代理池 JSON 文件：顶层 list 或 {"proxies": [...]}。坏文件返回 []。"""
    import json
    from pathlib import Path

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("proxies"), list):
        return raw["proxies"]
    return []


def _safe_int(val, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default
