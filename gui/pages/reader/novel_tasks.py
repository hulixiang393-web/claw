"""小说阅读后台任务（B4 拆分）：章节正文加载信号与 QRunnable。

原样迁移自 novel_view.py，行为不改变。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal


class _LoadChapterSignals(QObject):
    """章节加载信号。"""

    finished = Signal(object, object, object)  # (chapter, text, err)


class _LoadChapterTask(QRunnable):
    """后台加载章节正文（信号跨线程安全）。"""

    def __init__(self, content, source, chapter, load_images=False):
        super().__init__()
        self.signals = _LoadChapterSignals()
        self._content = content
        self._source = source
        self._chapter = chapter
        self._load_images = bool(load_images)

    def run(self) -> None:
        text, err = "", None
        try:
            if (
                getattr(self._source, "source_id", "") == "bookstack"
                and hasattr(self._content, "fetch_bookstack_chapter")
            ):
                rich = self._content.fetch_bookstack_chapter(self._source, self._chapter.url)
                self._chapter._cached_rich_content = rich
                self._chapter._cached_image_urls = [
                    {"type": "image", "url": url} for url in rich.image_urls
                ]
                text = rich.text
                self._chapter._cached_text = text
                self._chapter._images_attempted = True
            elif self._load_images and hasattr(self._content, "fetch_chapter_with_images"):
                try:
                    text, self._chapter._cached_image_urls = self._content.fetch_chapter_with_images(
                        self._source, self._chapter.url
                    )
                    self._chapter._images_attempted = True
                except (AttributeError, NotImplementedError, TypeError):
                    text = self._content.fetch_chapter(self._source, self._chapter.url)
                    self._chapter._images_attempted = True
            else:
                text = self._content.fetch_chapter(self._source, self._chapter.url)
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(self._chapter, text, err)
        except RuntimeError:
            pass  # 页面已销毁
