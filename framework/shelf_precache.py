"""书架收藏的离线章节缓存后台任务。"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRunnable, Signal

log = logging.getLogger(__name__)


class _Signals(QObject):
    progress = Signal(int, int)
    finished = Signal(int)
    failed = Signal(str)


class ShelfPrecacheJob(QRunnable):
    def __init__(
        self,
        content,
        source,
        chapters,
        start_idx,
        ahead,
        repository,
        book_key,
        on_progress=None,
    ):
        super().__init__()
        self._content = content
        self._source = source
        self._chapters = chapters or []
        self._start_idx = max(0, int(start_idx))
        self._ahead = max(0, int(ahead))
        self._repository = repository
        self._book_key = book_key
        self._on_progress = on_progress
        self._cancelled = False
        self.signals = _Signals()

    def cancel(self) -> None:
        self._cancelled = True

    def _emit(self, signal, *args) -> None:
        try:
            signal.emit(*args)
        except Exception:  # noqa: BLE001
            log.debug("[precache] signal emission failed", exc_info=True)

    def _write_repository(self, source_id: str, chapter_key: str, text: str) -> None:
        if self._repository is None:
            return
        self._repository.upsert_book(
            self._book_key, source_id, self._book_key, "", {}
        )
        if self._repository.get_content(self._book_key, chapter_key) is None:
            self._repository.put_content(
                self._book_key, chapter_key, "text/plain", text, {}
            )

    def run(self) -> None:
        total = len(self._chapters)
        if total == 0 or self._repository is None or not self._book_key:
            return
        end = min(self._start_idx + self._ahead + 1, total)
        if end <= self._start_idx:
            return

        span = end - self._start_idx
        done = 0
        kept: list[str] = []
        for index in range(self._start_idx, end):
            if self._cancelled:
                return
            chapter = self._chapters[index]
            url = getattr(chapter, "url", "")
            if not url:
                continue
            abs_url = self._content._abs_url(self._source, url)
            key = f"body:{self._source.source_id}:{abs_url}"
            try:
                text = self._content._cache_get(key, book_key=self._book_key)
                if text is None:
                    text = self._content.fetch_chapter(self._source, url)
                    if text:
                        self._content._cache_set(
                            key, text, ttl=7 * 86400, book_key=self._book_key
                        )
                        self._write_repository(self._source.source_id, abs_url, text)
                if text is not None:
                    kept.append(abs_url)
                    done += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("[precache] %s cache failed: %s", url, exc)
                continue
            if self._on_progress is not None:
                try:
                    self._on_progress(done, span)
                except Exception:  # noqa: BLE001
                    log.debug("[precache] progress callback failed", exc_info=True)
            self._emit(self.signals.progress, done, span)

        try:
            self._repository.set_book_pinned(self._book_key, True)
            self._repository.evict_book_content(self._book_key, kept)
            self._emit(self.signals.finished, done)
        except Exception as exc:  # noqa: BLE001
            log.warning("[precache] finalize failed %s: %s", self._book_key, exc)
            self._emit(self.signals.failed, str(exc))
