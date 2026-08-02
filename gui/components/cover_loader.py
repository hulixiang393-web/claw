"""封面异步加载器（CoverLoader）。

共享单个 QNetworkAccessManager + 并发限流（同时最多 MAX_CONCURRENT 个请求），
避免每个 WorkCard 独立发请求导致网络拥塞卡顿。

用法：
    loader = CoverLoader.instance()
    loader.load(url, callback)   # callback(pixmap) 加载完成（可能为 None）
"""

from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

MAX_CONCURRENT = 6  # 同时最多 6 个封面请求（调大提升加载速度）
REQUEST_TIMEOUT_MS = 8000  # 请求超时 8s，防止慢图/404 阻塞队列


class _CoverLoader(QObject):
    """单例封面加载器。"""

    def __init__(self):
        super().__init__()
        self._manager = QNetworkAccessManager(self)
        self._manager.finished.connect(self._on_reply)
        self._queue: List[tuple] = []
        self._pending: dict = {}  # reply → callback
        self._active = 0

    # ------------------------------------------------------------------ #
    def load(self, url: str, callback: Callable[[Optional[QPixmap]], None]) -> None:
        if not url:
            callback(None)
            return
        self._queue.append((url, callback))
        self._pump()

    def _pump(self) -> None:
        while self._active < MAX_CONCURRENT and self._queue:
            url, callback = self._queue.pop(0)
            request = QNetworkRequest(QUrl(url))
            request.setHeader(QNetworkRequest.UserAgentHeader, "Mozilla/5.0 SpiderDemo")
            request.setTransferTimeout(REQUEST_TIMEOUT_MS)  # 超时，防卡队列
            self._active += 1
            # 用属性存回调，reply 完成后取出
            reply = self._manager.get(request)
            self._pending[reply] = callback

    def _on_reply(self, reply: QNetworkReply) -> None:
        callback = self._pending.pop(reply, None)
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
