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
from .proxy_pool import ProxyPool

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


class AntiScrapeError(RequestError):
    """反爬识别：HTTP 403/429/5xx，或响应含验证码/封禁特征词。

    配置了代理池时由 HttpClient 换 IP 重试（不重复请求同一 IP）；
    未配置代理池时按普通失败走原有重试逻辑。
    """


# 反爬状态码：403 禁止、429 限流、5xx 服务端风控
ANTI_SCRAPE_STATUSES = frozenset({403, 429})

# 验证码/封禁特征词（对响应文本前 2000 字符小写匹配）
ANTI_SCRAPE_KEYWORDS = (
    "captcha", "geetest", "g-recaptcha", "verify you are human",
    "you have been blocked", "access denied", "access is denied",
    "forbidden", "anti-bot", "riskcontrol", "waf",
    "验证码", "人机验证", "滑动验证", "拖动验证",
    "访问过于频繁", "访问频率过高", "请求过于频繁",
    "被封", "封禁", "已封", "被禁止访问", "ip被限制", "ip 被限制", "访问被拒绝",
)


def _is_anti_scrape_status(status: int) -> bool:
    """按状态码判定反爬：403/429/5xx。"""
    return status in ANTI_SCRAPE_STATUSES or 500 <= status < 600


def _is_anti_scrape_text(text: str) -> bool:
    """按特征词判定反爬：验证码/封禁提示等。只扫前 2000 字符降低误报与开销。"""
    snippet = (text or "")[:2000].lower()
    return any(k in snippet for k in ANTI_SCRAPE_KEYWORDS)


class HttpClient:
    # 连接池参数：并发下载（漫画多线程取图、批量任务）时同 host 请求远超 requests
    # 默认池（10）→ 会排队等连接。放大池容量，配合 keep-alive 显著提速。
    _POOL_CONNECTIONS = 40
    _POOL_MAXSIZE = 128

    def __init__(self, sleeper=None, defaults: Optional[NetworkDefaults] = None):
        self._sleeper = sleeper if sleeper is not None else time.sleep
        self.defaults = defaults or NetworkDefaults()
        self._session = None
        if _REQUESTS_AVAILABLE:
            self._session = requests.Session()
            # 关闭系统代理环境变量继承：框架按源显式管理代理（proxy / proxy_pool），
            # "直连"即真直连。否则 HTTP(S)_PROXY 环境变量会让 requests 静默走
            # 环境代理，可能被拦截返回 502 等（实测开发机代理干扰）。
            self._session.trust_env = False
            self._bump_connection_pool()

    def _bump_connection_pool(self) -> None:
        """放大 requests HTTPAdapter 连接池（每 host 并发连接数）。

        默认 pool_maxsize=10 时，8 线程取图 + 主请求并发会互相阻塞等连接；
        放大后同 host 可同时建立更多连接，漫画/批量下载吞吐明显提升。
        """
        try:
            from requests.adapters import HTTPAdapter

            adapter = HTTPAdapter(
                pool_connections=self._POOL_CONNECTIONS,
                pool_maxsize=self._POOL_MAXSIZE,
                max_retries=0,
            )
            self._session.mount("http://", adapter)
            self._session.mount("https://", adapter)
        except Exception:  # noqa: BLE001 —— 池放大失败不影响使用
            pass

    # ------------------------------------------------------------------ #
    def _headers_with_ua(self, headers: Optional[dict]) -> Optional[dict]:
        """调用方未传 headers 时注入全局默认 UA（有 UA 则不覆盖）。"""
        if headers is None and self.defaults.user_agent:
            return {"User-Agent": self.defaults.user_agent}
        return headers

    # ------------------------------------------------------------------ #
    def _run_with_proxy_switch(self, once, proxy, proxy_pool, url_desc):
        """代理池换 IP 重试外壳：每次用 proxy_pool.next() 取代理，失败（含反爬）
        则 mark_bad 换下一个，最多换 max_switches 次。

        auto 池（触发反爬才启用）：首次请求直连，遇 AntiScrapeError 自动
        engage() 启用代理并用第一个代理重试——只针对触发反爬的源（每源持有
        独立池实例，互不影响）。普通失败（非反爬）不触发启用。

        未配置代理池（或池为空）时行为与原逻辑完全一致：直连/单代理跑一次。
        once(current_proxy)：单次（含原 retries 重试）请求，抛异常表示该 IP 失败。
        """
        if proxy_pool is None or len(proxy_pool) == 0:
            return once(proxy)
        last_error: Exception | None = None
        total = proxy_pool.max_switches + 1  # 初始 IP + 最多换 N 次
        for attempt in range(total):
            current_proxy = proxy_pool.next()
            try:
                return once(current_proxy)
            except AntiScrapeError as exc:
                last_error = exc
                if proxy_pool.is_auto and not proxy_pool.engaged:
                    # 触发反爬 → 启用代理池（针对该源），立即用第一个代理重试
                    proxy_pool.engage()
                    continue
                if attempt >= total - 1:
                    break
                proxy_pool.mark_bad()
                if not proxy_pool.available():
                    break  # 代理全部失效：抛最后错误，不再直连兜底
                self._sleeper(min(0.5 * (2 ** attempt), 2.0))
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if proxy_pool.is_auto and not proxy_pool.engaged:
                    raise  # 普通失败（非反爬）：不自动启用代理，直连错误原样抛
                if attempt >= total - 1:
                    break
                proxy_pool.mark_bad()
                if not proxy_pool.available():
                    break
                self._sleeper(min(0.5 * (2 ** attempt), 2.0))
        if isinstance(last_error, RequestError):
            raise last_error
        raise RequestError(f"请求失败 {url_desc}：{last_error}")

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
        proxy_pool: Optional[ProxyPool] = None,
    ) -> str:
        """GET 返回响应文本。retries耗尽抛RequestError。
        encoding: 显式指定响应编码（如utf-8），None则用resp.text自动检测。
        proxy_pool: 代理IP池，反爬/失败时换IP重试（最多换 max_switches 次）。
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

        def _once(current_proxy: Optional[str]) -> str:
            last_error: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    return self._get_once(url, headers, current_proxy, timeout, encoding)
                except AntiScrapeError as exc:
                    if proxy_pool is not None:
                        raise  # 反爬：不重复请求同一IP，立即换IP重试
                    last_error = exc
                    if attempt < retries:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < retries:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
            raise RequestError(f"请求失败 GET {url}：{last_error}")

        return self._run_with_proxy_switch(_once, proxy, proxy_pool, f"GET {url}")

    def get_bytes(
        self,
        url: str,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float | None = None,
        retries: int | None = None,
        proxy_pool: Optional[ProxyPool] = None,
    ) -> bytes:
        """GET 返回响应字节（图片等二进制内容）。重试耗尽抛 RequestError。
        proxy_pool: 代理IP池，反爬/失败时换IP重试。"""
        if timeout is None:
            timeout = self.defaults.timeout
        if retries is None:
            retries = self.defaults.retries
        if proxy is None:
            proxy = self.defaults.proxy
        headers = self._headers_with_ua(headers)
        self._sleeper(0.0)

        def _once(current_proxy: Optional[str]) -> bytes:
            last_error: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    if self._session is not None:
                        proxies = {"http": current_proxy, "https": current_proxy} if current_proxy else None
                        resp = self._session.get(
                            url, headers=headers, proxies=proxies, timeout=timeout
                        )
                        if _is_anti_scrape_status(resp.status_code):
                            raise AntiScrapeError(f"反爬响应 HTTP {resp.status_code} {url}")
                        resp.raise_for_status()
                        return resp.content
                    import urllib.request

                    req = urllib.request.Request(url, headers=headers or {})
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        if resp.status >= 400:
                            raise RequestError(f"HTTP {resp.status} {url}")
                        return resp.read()
                except AntiScrapeError as exc:
                    if proxy_pool is not None:
                        raise
                    last_error = exc
                    if attempt < retries:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < retries:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
            raise RequestError(f"请求失败 GET {url}：{last_error}")

        return self._run_with_proxy_switch(_once, proxy, proxy_pool, f"GET {url}")

    def get_json(
        self,
        url: str,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float | None = None,
        retries: int | None = None,
        proxy_pool: Optional[ProxyPool] = None,
    ) -> dict:
        """GET 并解析 JSON 响应。proxy_pool: 代理IP池，反爬/失败时换IP重试。"""
        import json

        text = self.get_text(url, headers, proxy, timeout, retries, proxy_pool=proxy_pool)
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
        proxy_pool: Optional[ProxyPool] = None,
    ) -> dict:
        """POST JSON 并解析响应 JSON。供解密 custom_endpoint 等调用。
        proxy_pool: 代理IP池，反爬/失败时换IP重试。"""
        import json as _json

        if timeout is None:
            timeout = self.defaults.timeout
        if retries is None:
            retries = self.defaults.retries
        if proxy is None:
            proxy = self.defaults.proxy
        headers = self._headers_with_ua(headers)
        self._sleeper(0.0)

        def _once(current_proxy: Optional[str]) -> dict:
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
                            proxies={"http": current_proxy, "https": current_proxy} if current_proxy else None,
                        )
                        if _is_anti_scrape_status(resp.status_code):
                            raise AntiScrapeError(f"反爬响应 HTTP {resp.status_code} {url}")
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
                    if _is_anti_scrape_text(text):
                        raise AntiScrapeError(f"反爬特征响应 POST {url}")
                    try:
                        return _json.loads(text)
                    except _json.JSONDecodeError:
                        return {}
                except AntiScrapeError as exc:
                    if proxy_pool is not None:
                        raise
                    last_error = exc
                    if attempt < retries:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < retries:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
            raise RequestError(f"请求失败 POST {url}：{last_error}")

        return self._run_with_proxy_switch(_once, proxy, proxy_pool, f"POST {url}")

    def post_form(
        self,
        url: str,
        form_data: Optional[dict] = None,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float | None = None,
        retries: int | None = None,
        encoding: Optional[str] = None,
        proxy_pool: Optional[ProxyPool] = None,
    ) -> str:
        """POST 表单并返回响应文本。encoding 指定响应编码（如utf-8）。
        proxy_pool: 代理IP池，反爬/失败时换IP重试。"""
        from urllib.parse import urlencode

        if timeout is None:
            timeout = self.defaults.timeout
        if retries is None:
            retries = self.defaults.retries
        if proxy is None:
            proxy = self.defaults.proxy
        headers = self._headers_with_ua(headers)
        self._sleeper(0.0)

        def _once(current_proxy: Optional[str]) -> str:
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
                            proxies={"http": current_proxy, "https": current_proxy} if current_proxy else None,
                        )
                        if _is_anti_scrape_status(resp.status_code):
                            raise AntiScrapeError(f"反爬响应 HTTP {resp.status_code} {url}")
                        resp.raise_for_status()
                        if encoding:
                            resp.encoding = encoding
                        text = resp.text
                    else:
                        import urllib.request

                        body = urlencode(form_data or {}).encode("utf-8")
                        req = urllib.request.Request(url, data=body, headers=post_headers)
                        with urllib.request.urlopen(req, timeout=timeout) as resp:
                            if encoding:
                                text = resp.read().decode(encoding, errors="replace")
                            else:
                                text = resp.read().decode("utf-8", errors="replace")
                    if _is_anti_scrape_text(text):
                        raise AntiScrapeError(f"反爬特征响应 POST {url}")
                    return text
                except AntiScrapeError as exc:
                    if proxy_pool is not None:
                        raise
                    last_error = exc
                    if attempt < retries:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < retries:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
            raise RequestError(f"请求失败 POST {url}：{last_error}")

        return self._run_with_proxy_switch(_once, proxy, proxy_pool, f"POST {url}")

    def _get_once(self, url, headers, proxy, timeout, encoding=None) -> str:
        if self._session is not None:
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = self._session.get(url, headers=headers, proxies=proxies, timeout=timeout)
            if _is_anti_scrape_status(resp.status_code):
                raise AntiScrapeError(f"反爬响应 HTTP {resp.status_code} {url}")
            resp.raise_for_status()
            if encoding:
                resp.encoding = encoding
                text = resp.text
            else:
                # 未指定编码：requests 默认 ISO-8859-1 会乱码中文站。
                # 从响应头 charset / HTML meta 推断；取不到用 apparent_encoding 兜底。
                charset = (resp.headers.get("Content-Type") or "").split("charset=")[-1].strip().lower()
                if charset and charset not in ("iso-8859-1",):
                    resp.encoding = charset
                else:
                    resp.encoding = resp.apparent_encoding or "utf-8"
                text = resp.text
            if _is_anti_scrape_text(text):
                raise AntiScrapeError(f"反爬特征响应 {url}")
            return text
        # urllib 降级
        import urllib.request
        import urllib.error

        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status >= 400:
                    raise RequestError(f"HTTP {resp.status} {url}")
                charset = (resp.headers.get("Content-Type") or "").split("charset=")[-1].strip().lower()
                if not charset or charset == "iso-8859-1":
                    charset = "utf-8"
                if encoding:
                    charset = encoding
                return resp.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            if _is_anti_scrape_status(exc.code):
                raise AntiScrapeError(f"反爬响应 HTTP {exc.code} {url}") from exc
            raise

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
