"""事件总线（EventBus）与事件常量。

GUI 通过 EventBus 订阅爬取进度/错误，不阻塞 UI 线程。
对应 PROJECT.md §4.4 事件常量。

用法：
    bus = EventBus()
    handler = lambda e: print(e)
    bus.subscribe(handler)
    bus.emit(Event("SEARCH_PROGRESS", {"source_id": "x", "done": 1, "total": 10}))
    bus.unsubscribe(handler)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List

log = logging.getLogger(__name__)

# ---- 事件常量 ----------------------------------------------------------- #
EVENT_SEARCH_STARTED = "SEARCH_STARTED"
EVENT_SEARCH_PROGRESS = "SEARCH_PROGRESS"
EVENT_SEARCH_COMPLETED = "SEARCH_COMPLETED"
EVENT_SOURCE_ERROR = "SOURCE_ERROR"

EVENT_DOWNLOAD_STARTED = "DOWNLOAD_STARTED"
EVENT_DOWNLOAD_PROGRESS = "DOWNLOAD_PROGRESS"
EVENT_DOWNLOAD_COMPLETED = "DOWNLOAD_COMPLETED"
EVENT_DOWNLOAD_FAILED = "DOWNLOAD_FAILED"

EVENT_STRUCTURE_CHANGED = "STRUCTURE_CHANGED"
EVENT_LOG = "LOG"
EVENT_THEME_CHANGED = "THEME_CHANGED"
EVENT_BULK_FETCH_PROGRESS = "BULK_FETCH_PROGRESS"
EVENT_BULK_FETCH_COMPLETED = "BULK_FETCH_COMPLETED"


@dataclass
class Event:
    """一条事件。type 为事件常量，payload 为任意数据。"""

    type: str
    payload: Any = None
    source_id: str | None = None


class EventBus:
    """线程安全的事件总线：订阅者列表 + 广播。"""

    def __init__(self) -> None:
        self._subscribers: List[Callable[[Event], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, handler: Callable[[Event], None]) -> None:
        with self._lock:
            # 去重：同一 handler 重复订阅会泄漏（unsubscribe 只移第一个）
            if handler not in self._subscribers:
                self._subscribers.append(handler)

    def unsubscribe(self, handler: Callable[[Event], None]) -> None:
        with self._lock:
            if handler in self._subscribers:
                self._subscribers.remove(handler)

    def emit(self, event: Event) -> None:
        """广播事件。订阅者异常被隔离，不影响其它订阅者，但记日志以便排查。"""
        with self._lock:
            handlers = list(self._subscribers)
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                # 订阅者内部错误不影响总线，但记日志避免静默埋雷
                log.exception("事件订阅者处理失败：%s", type(handler).__name__)

    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()
