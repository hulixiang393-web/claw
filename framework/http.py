"""HTTP 客户端（http.py）。

消费 transports 配置（base_url/headers/proxy/timeout/retries/interval_ms）。
优先 requests，未装则 urllib 降级。异常统一包装为 RequestError。
对应 design.md §5 三层架构的「执行引擎」层。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .errors import RequestError

try:
    import requests

    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False


class HttpClient:
    def __init__(self, sleeper=None):
        self._sleeper = sleeper if sleeper is not None else time.sleep
        self._session = None
        if _REQUESTS_AVAILABLE:
            self._session = requests.Session()

    # ------------------------------------------------------------------ #
    def get_text(
        self,
        url: str,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float = 10.0,
        retries: int = 3,
        interval_ms: int = 0,
    ) -> str:
        """GET 返回响应文本。重试耗尽抛 RequestError。"""
        self._sleeper(interval_ms / 1000.0)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return self._get_once(url, headers, proxy, timeout)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < retries:
                    self._sleeper(min(0.5 * (2 ** attempt), 2.0))
        raise RequestError(f"请求失败 GET {url}：{last_error}")

    def _get_once(self, url, headers, proxy, timeout) -> str:
        if self._session is not None:
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = self._session.get(url, headers=headers, proxies=proxies, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        # urllib 降级
        import urllib.request

        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
