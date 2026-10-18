"""代理 IP 池（proxy_pool.py）。

反爬时更换 IP 继续爬取：多线程爬取共用一个池，next() 取当前代理、
mark_bad() 把当前代理标记失效并切到下一个、rotate() 手动轮换、
available() 判断是否还有可用代理。请求失败/触发反爬（403/封禁/验证码
特征）时由 HttpClient 换 IP 重试（最多换 max_switches 次）。

两种用法：
  1. 源显式配置（transports.proxy_pool）——始终启用（auto=False）。
  2. 框架级全局代理池（auto=True）——默认直连，该源首次触发反爬时由
     HttpClient 调 engage() 自动启用，后续该源请求走代理。每源持有独立
     池实例，一个源启用不影响其他源（针对触发反爬的源，非全量代理）。

代理来源（ProxyPool.from_config 解析 transports.proxy_pool 或全局配置）：
  - list[str]                                  ["http://ip:port", ...]
  - list[dict]                                 [{"proxy": "http://ip:port", "name": "..."}]
  - dict                                       {"proxies": [...], "max_switches": N}
  - dict（指向代理池 JSON 文件）                 {"file": "path/proxies.json", "max_switches": N}
  - str（代理池 JSON 文件路径）                  "path/proxies.json"
代理池 JSON 文件：顶层为 list，或 {"proxies": [...]}。

全局代理配置（global_proxy_config）来源优先级：
  app_config.json network.proxy_pool → 默认 data/proxies.json。
"""

from __future__ import annotations

import threading
from pathlib import Path
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
        auto: bool = False,
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
        # auto=True：触发反爬前不启用（next() 返回 None → 直连），该源首次
        # 触发反爬时由 HttpClient 调 engage() 自动启用——针对反爬源，非全量代理。
        self.auto = auto
        self.engaged = not auto

    # ------------------------------------------------------------------ #
    # 构造辅助
    # ------------------------------------------------------------------ #
    @classmethod
    def from_config(cls, cfg, auto: bool = False) -> Optional["ProxyPool"]:
        """从 proxy_pool 配置构建。非法/空配置返回 None（调用方当未配置处理）。

        auto=True：触发反爬才启用（全局池）；False（默认）始终启用（源显式配置）。
        """
        proxies, max_switches = _extract_pool_config(cfg)
        if not proxies:
            return None
        return cls(proxies, max_switches=max_switches, auto=auto)

    # ------------------------------------------------------------------ #
    # 取用 / 轮换 / 失效标记
    # ------------------------------------------------------------------ #
    def next(self) -> Optional[str]:
        """取当前代理 URL（不推进）；无可用代理返回 None。

        auto 池未启用（engaged=False）时返回 None：调用方走直连。
        """
        with self._lock:
            if not self.engaged:
                return None  # auto 池未启用：直连（不代理）
            entry = self._current_entry()
            return entry.url if entry is not None else None

    def engage(self) -> None:
        """启用代理池（auto 池触发反爬后由 HttpClient 调用）。

        启用后 next() 开始返回代理，该源后续请求走代理。
        """
        with self._lock:
            self.engaged = True

    @property
    def is_auto(self) -> bool:
        """是否 auto 池（触发反爬后自动启用）。"""
        return self.auto

    def mark_bad(self) -> Optional[str]:
        """把当前代理标记失效并切换到下一个可用代理。

        返回新当前代理 URL；全部失效后返回 None。
        """
        with self._lock:
            if self.auto and not self.engaged:
                return None  # 未启用的 auto 池不标记（http 层也不会调用）
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


# ---------------------------------------------------------------------- #
# 框架级全局代理池（供未显式配置 proxy_pool 的源自动启用）
# ---------------------------------------------------------------------- #
_GLOBAL_PROXY_CFG = None  # 缓存 (proxies, max_switches) 或 (None, None)
_GLOBAL_PROXY_LOCK = threading.Lock()


def _base_dir() -> Path:
    """应用根目录：与 gui/app.py 一致（frozen → exe 目录；开发 → 项目根）。"""
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def global_proxy_config(override: Optional[dict] = None):
    """框架级全局代理配置（每源 auto 池复用同一份代理列表）。

    返回 (proxies, max_switches)；未配置/空列表返回 (None, None)。
    来源优先级：
      1. override（测试注入 / 调用方显式传入，同时写入缓存）
      2. app_config.json 的 network.proxy_pool
      3. 默认 data/proxies.json
    """
    global _GLOBAL_PROXY_CFG
    if override is not None:
        with _GLOBAL_PROXY_LOCK:
            _GLOBAL_PROXY_CFG = _resolve_global_proxy_cfg(override)
        return _GLOBAL_PROXY_CFG
    if _GLOBAL_PROXY_CFG is None:
        with _GLOBAL_PROXY_LOCK:
            if _GLOBAL_PROXY_CFG is None:
                _GLOBAL_PROXY_CFG = _load_global_proxy_cfg()
    return _GLOBAL_PROXY_CFG


def reset_global_proxy_config() -> None:
    """清空全局代理配置缓存（测试/配置热更新用）。"""
    global _GLOBAL_PROXY_CFG
    with _GLOBAL_PROXY_LOCK:
        _GLOBAL_PROXY_CFG = None


def _load_global_proxy_cfg():
    base = _base_dir()
    cfg = None
    try:
        raw = (base / "app_config.json").read_text(encoding="utf-8")
        import json as _json

        cfg = (_json.loads(raw).get("network") or {}).get("proxy_pool")
    except Exception:  # noqa: BLE001
        cfg = None
    if cfg is None:
        p = base / "data" / "proxies.json"
        if p.exists():
            cfg = {"file": str(p)}
    return _resolve_global_proxy_cfg(cfg)


def _resolve_global_proxy_cfg(cfg):
    if not cfg:
        return (None, None)
    # 相对文件路径 → 相对应用根目录（开发/打包后 data/ 都在根目录旁）
    if isinstance(cfg, dict) and cfg.get("file") and not Path(str(cfg["file"])).is_absolute():
        cfg = dict(cfg)
        cfg["file"] = str(_base_dir() / cfg["file"])
    proxies, max_switches = _extract_pool_config(cfg)
    return (proxies or None, max_switches)
