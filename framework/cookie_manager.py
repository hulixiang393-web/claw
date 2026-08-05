"""Cookie 管理（cookie_manager.py）。

存储/加载每个源的登录 Cookie。Cookie 从 QWebEngineView 登录弹窗抓取，
保存到独立文件 data/cookies/<source_id>.json（与源配置分离，可 gitignore）。

结构：
{
  "cookies": { "name": "value", ... },   # 去重后的 cookie 字典
  "cookies_list": [ {"name","value","domain","path",...} ],  # 原始 cookie 列表
  "logged_at": 1754000000                # 登录时间戳
}
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

# cookie 存储根目录
COOKIE_DIR_NAME = "cookies"


class CookieManager:
    def __init__(self, base_dir: str | Path):
        self._base_dir = Path(base_dir)
        self._cookie_dir = self._base_dir / COOKIE_DIR_NAME
        self._cookie_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    def _path(self, source_id: str) -> Path:
        return self._cookie_dir / f"{source_id}.json"

    # ------------------------------------------------------------------ #
    def save(
        self,
        source_id: str,
        cookies_list: List[dict],
        logged_at: Optional[int] = None,
    ) -> None:
        """保存 cookie 列表。cookies_list 是 QWebEngineCookie 序列化后的 dict 列表。"""
        # 去重：同名同域取最后
        merged: Dict[str, str] = {}
        raw_list = []
        for c in cookies_list:
            name = c.get("name") or ""
            value = c.get("value") or ""
            if not name:
                continue
            merged[name] = value
            raw_list.append(
                {
                    "name": name,
                    "value": value,
                    "domain": c.get("domain") or "",
                    "path": c.get("path") or "",
                    "secure": bool(c.get("secure")),
                }
            )
        data = {
            "cookies": merged,
            "cookies_list": raw_list,
            "logged_at": logged_at or int(time.time()),
        }
        try:
            self._path(source_id).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._cache[source_id] = data
        except OSError:
            pass  # cookie 写失败不阻塞

    # ------------------------------------------------------------------ #
    def load(self, source_id: str) -> Dict[str, str]:
        """加载 cookie 字典 {name: value}。无则空。"""
        cached = self._cache.get(source_id)
        if cached is not None:
            return dict(cached.get("cookies") or {})
        path = self._path(source_id)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._cache[source_id] = data
            return dict(data.get("cookies") or {})
        except (json.JSONDecodeError, OSError):
            return {}

    # ------------------------------------------------------------------ #
    def to_cookie_header(self, source_id: str) -> str:
        """cookie 字典 → "k=v; k2=v2" 字符串（用于 Cookie 请求头）。"""
        cookies = self.load(source_id)
        if not cookies:
            return ""
        return "; ".join(f"{k}={v}" for k, v in cookies.items())

    # ------------------------------------------------------------------ #
    def logged_at(self, source_id: str) -> Optional[int]:
        """登录时间戳，未登录返回 None。"""
        cached = self._cache.get(source_id)
        if cached is None:
            path = self._path(source_id)
            if path.exists():
                try:
                    self._cache[source_id] = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    return None
                cached = self._cache[source_id]
        return cached.get("logged_at") if cached else None

    def is_logged_in(self, source_id: str) -> bool:
        return bool(self.load(source_id))

    # ------------------------------------------------------------------ #
    def clear(self, source_id: str) -> None:
        """清除某源的 cookie。"""
        self._cache.pop(source_id, None)
        try:
            self._path(source_id).unlink(missing_ok=True)
        except OSError:
            pass