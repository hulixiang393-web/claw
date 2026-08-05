"""HTTP 客户端（http.py）。

消费 transports 配置（base_url/headers/proxy/timeout/retries/interval_ms）。
优先 requests，未装则 urllib 降级。异常统一包装为 RequestError。
对应 design.md §5 三层架构的「执行引擎」层。

全局默认值：HttpClient(defaults=NetworkDefaults(...)) 由 App 层从 settings 构建，
调用方未传参时兜底（对应 app_config.network.*）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .errors import RequestError

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 SpiderFramework/1.0"
)


@dataclass
class NetworkDefaults:
    """全局网络默认值（来自 settings，缺省用框架硬编码值）。"""

    timeout: float = 10.0
    retries: int = 3
    interval_ms: int = 0
    proxy: Optional[str] = None
    user_agent: str = DEFAULT_USER_AGENT

try:
    import requests

    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False


class HttpClient:
    def __init__(self, sleeper=None, defaults: Optional[NetworkDefaults] = None):
        self._sleeper = sleeper if sleeper is not None else time.sleep
        self.defaults = defaults or NetworkDefaults()
        self._session = None
        if _REQUESTS_AVAILABLE:
            self._session = requests.Session()

    # ------------------------------------------------------------------ #
    def _headers_with_ua(self, headers: Optional[dict]) -> Optional[dict]:
        """调用方未传 headers 时注入全局默认 UA（有 UA 则不覆盖）。"""
        if headers is None and self.defaults.user_agent:
            return {"User-Agent": self.defaults.user_agent}
        return headers

    # ------------------------------------------------------------------ #
    def get_text(
        self,
        url: str,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float | None = None,
        retries: int | None = None,
        interval_ms: int | None = None,
        encoding: Optional[str] = None,
    ) -> str:
        """GET 返回响应文本。retries耗尽抛RequestError。
        encoding: 显式指定响应编码（如utf-8），None则用resp.text自动检测。
        未传的参数用全局默认值（NetworkDefaults）。
        """
        if timeout is None:
            timeout = self.defaults.timeout
        if retries is None:
            retries = self.defaults.retries
        if interval_ms is None:
            interval_ms = self.defaults.interval_ms
        headers = self._headers_with_ua(headers)
        self._sleeper(interval_ms / 1000.0)
        if proxy is None:
            proxy = self.defaults.proxy
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return self._get_once(url, headers, proxy, timeout, encoding)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < retries:
                    self._sleeper(min(0.5 * (2 ** attempt), 2.0))
        raise RequestError(f"请求失败 GET {url}：{last_error}")

    def get_bytes(
        self,
        url: str,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> bytes:
        """GET 返回响应字节（图片等二进制内容）。重试耗尽抛 RequestError。"""
        if timeout is None:
            timeout = self.defaults.timeout
        if retries is None:
            retries = self.defaults.retries
        if proxy is None:
            proxy = self.defaults.proxy
        headers = self._headers_with_ua(headers)
        self._sleeper(0.0)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                if self._session is not None:
                    proxies = {"http": proxy, "https": proxy} if proxy else None
                    resp = self._session.get(
                        url, headers=headers, proxies=proxies, timeout=timeout
                    )
                    resp.raise_for_status()
                    return resp.content
                import urllib.request

                req = urllib.request.Request(url, headers=headers or {})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status >= 400:
                        raise RequestError(f"HTTP {resp.status} {url}")
                    return resp.read()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < retries:
                    self._sleeper(min(0.5 * (2 ** attempt), 2.0))
        raise RequestError(f"请求失败 GET {url}：{last_error}")

    def get_json(
        self,
        url: str,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> dict:
        """GET 并解析 JSON 响应。"""
        import json

        text = self.get_text(url, headers, proxy, timeout, retries)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    def post_json(
        self,
        url: str,
        json_body: Optional[dict] = None,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> dict:
        """POST JSON 并解析响应 JSON。供解密 custom_endpoint 等调用。"""
        import json as _json

        if timeout is None:
            timeout = self.defaults.timeout
        if retries is None:
            retries = self.defaults.retries
        if proxy is None:
            proxy = self.defaults.proxy
        headers = self._headers_with_ua(headers)
        self._sleeper(0.0)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                post_headers = dict(headers or {})
                post_headers.setdefault("Content-Type", "application/json")
                if self._session is not None:
                    resp = self._session.post(
                        url,
                        json=json_body or {},
                        headers=post_headers,
                        timeout=timeout,
                    )
                    resp.raise_for_status()
                    text = resp.text
                else:
                    import urllib.request

                    body = _json.dumps(json_body or {}).encode("utf-8")
                    req = urllib.request.Request(
                        url, data=body, headers=post_headers
                    )
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        text = resp.read().decode("utf-8", errors="replace")
                try:
                    return _json.loads(text)
                except _json.JSONDecodeError:
                    return {}
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < retries:
                    self._sleeper(min(0.5 * (2 ** attempt), 2.0))
        raise RequestError(f"请求失败 POST {url}：{last_error}")

    def post_form(
        self,
        url: str,
        form_data: Optional[dict] = None,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float | None = None,
        retries: int | None = None,
        encoding: Optional[str] = None,
    ) -> str:
        """POST 表单并返回响应文本。encoding 指定响应编码（如utf-8）。"""
        from urllib.parse import urlencode

        if timeout is None:
            timeout = self.defaults.timeout
        if retries is None:
            retries = self.defaults.retries
        if proxy is None:
            proxy = self.defaults.proxy
        headers = self._headers_with_ua(headers)
        self._sleeper(0.0)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                post_headers = dict(headers or {})
                post_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
                if self._session is not None:
                    resp = self._session.post(
                        url,
                        data=urlencode(form_data or {}),
                        headers=post_headers,
                        timeout=timeout,
                    )
                    resp.raise_for_status()
                    if encoding:
                        resp.encoding = encoding
                    return resp.text
                import urllib.request

                body = urlencode(form_data or {}).encode("utf-8")
                req = urllib.request.Request(url, data=body, headers=post_headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if encoding:
                        return resp.read().decode(encoding, errors="replace")
                    return resp.read().decode("utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < retries:
                    self._sleeper(min(0.5 * (2 ** attempt), 2.0))
        raise RequestError(f"请求失败 POST {url}：{last_error}")

    def _get_once(self, url, headers, proxy, timeout, encoding=None) -> str:
        if self._session is not None:
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = self._session.get(url, headers=headers, proxies=proxies, timeout=timeout)
            resp.raise_for_status()
            if encoding:
                resp.encoding = encoding
                return resp.text
            # 未指定编码：requests 默认 ISO-8859-1 会乱码中文站。
            # 从响应头 charset / HTML meta 推断；取不到用 apparent_encoding 兜底。
            charset = (resp.headers.get("Content-Type") or "").split("charset=")[-1].strip().lower()
            if charset and charset not in ("iso-8859-1",):
                resp.encoding = charset
            else:
                resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        # urllib 降级
        import urllib.request

        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                raise RequestError(f"HTTP {resp.status} {url}")
            charset = (resp.headers.get("Content-Type") or "").split("charset=")[-1].strip().lower()
            if not charset or charset == "iso-8859-1":
                charset = "utf-8"
            if encoding:
                charset = encoding
            return resp.read().decode(charset, errors="replace")

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
