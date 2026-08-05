"""封面异步加载器（CoverLoader）。

共享单个 QNetworkAccessManager + 并发限流（同时最多 MAX_CONCURRENT 个请求），
避免每个 WorkCard 独立发请求导致网络拥塞卡顿。

- 支持系统代理（Clash 等，与 Playwright 一致）
- 按图片域名推导 Referer（部分图床要求 Referer 同源）

用法：
    loader = CoverLoader.instance()
    loader.load(url, callback)   # callback(pixmap) 加载完成（可能为 None）
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Callable, List, Optional
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkProxy,
    QNetworkReply,
    QNetworkRequest,
)

from framework.playwright_helper import _system_proxy

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_BROWSER_UA = BROWSER_UA
MAX_CONCURRENT = 16  # 同时最多 16 个封面请求（提升一页 20 张封面加载速度）
REQUEST_TIMEOUT_MS = 8000  # 请求超时 8s，防止慢图/404 阻塞队列

# 图片 CDN → 所属主站（用于 Referer）。命中后按主站域名回填 Referer。
_REFERER_RULES = (
    # comicbox 图床 → 主站
    ("ccavbox.com", "https://www.comicbox.xyz/"),
    # B 站封面 CDN → B 站（防风控，部分环境需 Referer 才放行）
    ("hdslb.com", "https://www.bilibili.com/"),
)


def _infer_referer(url: str) -> Optional[str]:
    """按图片 URL 推导 Referer。外链图床需要同源 Referer 才放行。"""
    host = (urlsplit(url).hostname or "").lower()
    for img_domain, referer in _REFERER_RULES:
        if img_domain in host:
            return referer
    return None


class _CoverLoader(QObject):
    """单例封面加载器。"""

    def __init__(self):
        super().__init__()
        self._manager = QNetworkAccessManager(self)
        self._manager.finished.connect(self._on_reply)
        self._manager_direct = QNetworkAccessManager(self)  # 无代理 fallback manager
        self._manager_direct.finished.connect(self._on_direct_reply)
        self._queue: List[tuple] = []
        self._pending: dict = {}  # reply → (callback, url, used_proxy)
        self._direct_pending: dict = {}  # direct reply → callback
        self._active = 0
        self._proxy_set = False
        self._proxy_url: Optional[str] = None
        # 内存封面缓存（LRU，字节预算来自设置 cover_cache_size_mb）
        self._cache: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._cache_bytes = 0
        self._cache_budget = 0  # 0 = 关闭缓存

    def configure(self, cache_mb: float | int = 0) -> None:
        """设置缓存字节预算（MB）。0 关闭缓存。启动时调用一次。"""
        self._cache_budget = max(0, int(cache_mb or 0)) * 1024 * 1024
        self._trim_cache()

    def clear_cache(self) -> None:
        """清空内存封面缓存。"""
        self._cache.clear()
        self._cache_bytes = 0

    def _cache_get(self, url: str) -> Optional[QPixmap]:
        """LRU 命中：移到尾部（最新）。"""
        pm = self._cache.get(url)
        if pm is not None:
            self._cache.move_to_end(url)
            return pm
        return None

    def _cache_put(self, url: str, pixmap: QPixmap) -> None:
        if self._cache_budget <= 0 or not url or pixmap.isNull():
            return
        old = self._cache.get(url)
        if old is not None:
            self._cache_bytes -= self._pixmap_bytes(old)
        self._cache[url] = pixmap
        self._cache_bytes += self._pixmap_bytes(pixmap)
        self._trim_cache()

    @staticmethod
    def _pixmap_bytes(pm: QPixmap) -> int:
        try:
            img = pm.toImage()
            return img.byteCount() if not img.isNull() else 0
        except Exception:  # noqa: BLE001
            return 0

    def _trim_cache(self) -> None:
        """按字节预算 LRU 淘汰（删最久未用）。"""
        while self._cache_bytes > self._cache_budget > 0 and self._cache:
            _, pm = self._cache.popitem(last=False)
            self._cache_bytes -= self._pixmap_bytes(pm)

    def _ensure_proxy(self) -> None:
        """QNetwork 走系统代理（与 Playwright 一致），避免 Clash 下直连超时。

        记录 _proxy_url 供失败回退直连判断。
        """
        if self._proxy_set:
            return
        proxy_url = _system_proxy()
        if proxy_url:
            parts = urlsplit(proxy_url)
            if parts.hostname:
                proxy = QNetworkProxy(
                    QNetworkProxy.HttpProxy,
                    parts.hostname,
                    parts.port or 7890,
                )
                self._manager.setProxy(proxy)
                self._proxy_url = proxy_url
                log.info("[cover] 走代理 %s", proxy_url)
        self._proxy_set = True

    # ------------------------------------------------------------------ #
    def load(self, url: str, callback: Callable[[Optional[QPixmap]], None]) -> None:
        if not url:
            callback(None)
            return
        cached = self._cache_get(url)
        if cached is not None:
            callback(cached)
            return
        self._queue.append((url, callback))
        self._pump()

    def _pump(self) -> None:
        self._ensure_proxy()
        while self._active < MAX_CONCURRENT and self._queue:
            url, callback = self._queue.pop(0)
            request = QNetworkRequest(QUrl(url))
            request.setHeader(QNetworkRequest.UserAgentHeader, _BROWSER_UA)
            request.setTransferTimeout(REQUEST_TIMEOUT_MS)  # 超时，防卡队列
            referer = _infer_referer(url)
            if referer:
                request.setRawHeader(b"Referer", referer.encode("utf-8"))
            self._active += 1
            # 用属性存回调 + 代理标记，reply 完成后取出
            reply = self._manager.get(request)
            used_proxy = self._proxy_url is not None
            self._pending[reply] = (callback, url, used_proxy)

    def _on_reply(self, reply: QNetworkReply) -> None:
        callback, url, used_proxy = self._pending.pop(reply, (None, "", False))
        self._active -= 1
        pixmap = None
        try:
            if reply.error() == QNetworkReply.NoError:
                data = reply.readAll()
                p = QPixmap()
                if p.loadFromData(data) and not p.isNull():
                    pixmap = p
        except Exception:
            pixmap = None
        reply.deleteLater()
        # 代理请求失败 → 用无代理 manager 异步重试（不阻塞 UI；直接改代理会丢 fallback）
        if pixmap is None and used_proxy and url:
            if callback is not None:
                req2 = QNetworkRequest(QUrl(url))
                req2.setHeader(QNetworkRequest.UserAgentHeader, _BROWSER_UA)
                req2.setTransferTimeout(REQUEST_TIMEOUT_MS)
                referer = _infer_referer(url)
                if referer:
                    req2.setRawHeader(b"Referer", referer.encode("utf-8"))
                r2 = self._manager_direct.get(req2)
                self._direct_pending[r2] = callback
            self._pump()
            return
        if pixmap is not None and url:
            self._cache_put(url, pixmap)
        if callback:
            callback(pixmap)
        self._pump()

    def _on_direct_reply(self, reply: QNetworkReply) -> None:
        """无代理 fallback 完成。"""
        callback = self._direct_pending.pop(reply, None)
        pixmap = None
        try:
            if reply.error() == QNetworkReply.NoError:
                data = reply.readAll()
                p = QPixmap()
                if p.loadFromData(data) and not p.isNull():
                    pixmap = p
        except Exception:
            pixmap = None
        reply.deleteLater()
        if pixmap is not None:
            # 用回调闭包里的 URL 无法取得，若需要缓存 direct 结果 -> url 需改结构；
            # 保守不缓存 direct fallback（低频且无 url key）。仅调回。
            pass
        if callback:
            callback(pixmap)
        self._pump()


class CoverLoader:
    """对外门面（单例）。"""

    _instance: Optional[_CoverLoader] = None

    @classmethod
    def instance(cls) -> _CoverLoader:
        if cls._instance is None:
            cls._instance = _CoverLoader()
        return cls._instance
