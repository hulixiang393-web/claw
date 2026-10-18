"""漫画阅读后台任务（B4 拆分）：解码/加载/预渲染信号与 QRunnable。

原样迁移自 comic_view.py，行为不改变。_ComicImageLabel 仍留在视图模块
（依赖 ComicView._image_key_for_url）。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtGui import QImage


class _PixmapDecodeSignals(QObject):
    """后台图片解码完成信号（跨线程回主线程）。"""

    done = Signal(object)  # QImage（解码失败/空 → null QImage）


class _PixmapDecodeTask(QRunnable):
    """后台线程解码图片字节 → QImage（JPEG/PNG 解压是重活，移出主线程防卡 UI）。

    主线程收到 QImage 后仅做 QPixmap.fromImage（轻量拷贝），不再阻塞解压。
    """

    def __init__(self, data: bytes):
        super().__init__()
        self.signals = _PixmapDecodeSignals()
        self._data = data

    def run(self) -> None:
        img = QImage()
        try:
            if self._data:
                img.loadFromData(self._data)
        except Exception:  # noqa: BLE001
            img = QImage()
        try:
            self.signals.done.emit(img)
        except RuntimeError:
            pass


class _ComicSignals(QObject):
    """漫画加载信号。"""
    finished = Signal(object, object, object, object)  # (gen, chapter, images, err)
    partial = Signal(object, object, object, object)  # (gen, chapter, 已就绪前缀, None) 边抓边显示


class _LoadComicTask(QRunnable):
    """后台加载漫画话图片 URL（on_page 分批回调，边抓边显示）。"""

    def __init__(self, content, source, chapter, gen: int = 0, cancel_evt=None, use_cache=True):
        super().__init__()
        self.signals = _ComicSignals()
        self._content = content
        self._source = source
        self._chapter = chapter
        self._gen = gen
        self._cancel_evt = cancel_evt  # 换书取消令牌：置位后分页抓取尽早退出
        self._use_cache = use_cache

    def run(self) -> None:
        images, err = [], None
        try:
            try:
                images = self._content.fetch_comic_pages(
                    self._source, self._chapter.url,
                    on_page=self._emit_partial, cancel_evt=self._cancel_evt,
                    use_cache=self._use_cache,
                )
            except TypeError as exc:
                if "use_cache" not in str(exc):
                    raise
                images = self._content.fetch_comic_pages(
                    self._source, self._chapter.url,
                    on_page=self._emit_partial, cancel_evt=self._cancel_evt,
                )
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(self._gen, self._chapter, images, err)
        except RuntimeError:
            pass

    def _emit_partial(self, part) -> None:
        """解密/抓取进度分批回调 → 主线程 partial 信号（增量渲染）。"""
        try:
            self.signals.partial.emit(self._gen, self._chapter, list(part), None)
        except RuntimeError:
            pass


class _PrefetchSignals(QObject):
    """预加载信号。"""

    finished = Signal(object, object, object, object)  # (gen, chapter_url, images, err)


class _PrefetchRenderTask(QRunnable):
    """后台预渲染后续话的图片（fetch_comic_pages 全量渲染）。

    提前把下一话的 canvas base64 图都取好，切话时 _load_episode 命中
    _prefetched[url]["images"] → 秒开，不用现场爬 Playwright。
    """

    def __init__(self, content, source, chapter, gen: int = 0, cancel_evt=None, use_cache=True):
        super().__init__()
        self.signals = _PrefetchSignals()
        self._content = content
        self._source = source
        self._chapter = chapter
        self._gen = gen
        self._cancel_evt = cancel_evt  # 换书取消令牌：置位后分页抓取尽早退出
        self._use_cache = use_cache

    def run(self) -> None:
        images, err = [], None
        try:
            try:
                images = self._content.fetch_comic_pages(
                    self._source, self._chapter.url, cancel_evt=self._cancel_evt,
                    use_cache=self._use_cache,
                )
            except TypeError as exc:
                if "use_cache" not in str(exc):
                    raise
                images = self._content.fetch_comic_pages(
                    self._source, self._chapter.url, cancel_evt=self._cancel_evt
                )
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(self._gen, self._chapter.url, images, err)
        except RuntimeError:
            pass
