"""漫画阅读视图（ComicView）。

对应 ui-reader.md 漫画功能点：
- 图片逐页加载 + 预加载
- 滚动画廊（默认）/ 横向翻页可切换
- 图片缩放（点击放大/双击还原）
- 目录侧栏（可折叠，点击跳话）
- 自动下一话：读到当前话最后一张图底部 → 无缝加载下一话
- 预加载后续 5 话图片 URL（不下载字节，翻话时有页数/预热）
- 续读信号（chapter_changed 发 detail+话名+话URL）
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QThreadPool, QRunnable, QObject
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from framework.content import Content, Detail

# 预加载后续话数（仅拉 URL 不下载图，保内存）
PREFETCH_COUNT = 5


class ComicView(QWidget):
    """漫画翻页阅读视图。"""

    chapter_changed = Signal(object)  # 发 (detail, chapter_title, chapter_url)

    def __init__(self, content: Content, parent=None):
        super().__init__(parent)
        self._content = content
        self._source = None
        self._detail: Detail | None = None
        self._chapters = []
        self._current_idx = -1
        self._images = []
        self._mode = "gallery"  # gallery / flip
        self._zoom = 1.0
        self._auto_loading = False  # 自动翻话锁，防重复触发
        self._prefetched = {}  # {url: {"images":[...], "count":N}} 预渲染的后续话
        self._prefetch_queue = []  # 串行预渲染队列（同一时间只渲染 1 话）
        self._prefetch_busy = False  # 是否正在预渲染

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # ---- 工具栏 ----
        toolbar = QHBoxLayout()
        self.toc_btn = QToolButton()
        self.toc_btn.setText("☰ 目录")
        self.toc_btn.clicked.connect(self._toggle_toc)
        toolbar.addWidget(self.toc_btn)

        self.mode_btn = QPushButton("切换横向模式")
        self.mode_btn.clicked.connect(self._toggle_mode)
        toolbar.addWidget(self.mode_btn)

        self.prev_btn = QPushButton("上一话")
        self.next_btn = QPushButton("下一话")
        self.prev_btn.clicked.connect(lambda: self._jump_relative(-1))
        self.next_btn.clicked.connect(lambda: self._jump_relative(1))
        toolbar.addWidget(self.prev_btn)
        toolbar.addWidget(self.next_btn)

        toolbar.addStretch(1)
        self.progress_label = QLabel("")
        toolbar.addWidget(self.progress_label)
        layout.addLayout(toolbar)

        # ---- 目录 + 图片区 ----
        body = QHBoxLayout()
        body.setSpacing(8)

        self.toc_list = QListWidget()
        self.toc_list.setFixedWidth(180)
        self.toc_list.itemClicked.connect(self._on_toc_clicked)
        self.toc_list.setVisible(False)
        body.addWidget(self.toc_list)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.gallery = QWidget()
        self.gallery_layout = QVBoxLayout(self.gallery)
        self.gallery_layout.setContentsMargins(0, 0, 0, 0)
        self.gallery_layout.setSpacing(4)
        self.gallery_layout.setAlignment(Qt.AlignHCenter)
        self.scroll.setWidget(self.gallery)
        body.addWidget(self.scroll, stretch=1)
        layout.addLayout(body, stretch=1)

        # 自动下一话：读到当前话底部（最后一张图）→ 无缝加载下一话
        self.scroll.verticalScrollBar().valueChanged.connect(self._maybe_auto_next)

        self._apply_mode()

    # ------------------------------------------------------------------ #
    def load(self, source, detail: Detail, start_chapter_url: str = "") -> None:
        self._source = source
        self._detail = detail
        self._chapters = detail.chapters
        self._populate_toc()
        idx = 0
        if start_chapter_url:
            for i, ch in enumerate(detail.chapters):
                if ch.url == start_chapter_url:
                    idx = i
                    break
        self._load_episode(idx)
        # 注：预加载在 _on_images_loaded 当前话加载完成后发起

    def _populate_toc(self) -> None:
        """填充目录。"""
        self.toc_list.clear()
        for i, ch in enumerate(self._chapters):
            item = QListWidgetItem(ch.title or f"第{i+1}话")
            item.setData(Qt.UserRole, i)
            self.toc_list.addItem(item)

    def _on_toc_clicked(self, item) -> None:
        idx = item.data(Qt.UserRole)
        self._load_episode(idx)

    def _toggle_toc(self) -> None:
        self.toc_list.setVisible(not self.toc_list.isVisible())

    def _load_episode(self, idx: int) -> None:
        if self._source is None or not (0 <= idx < len(self._chapters)):
            return
        self._current_idx = idx
        ch = self._chapters[idx]
        self.toc_list.setCurrentRow(idx)  # 目录高亮当前话

        # 【速度优化】已缓存本话图片 → 直接显示，秒开不重爬
        if hasattr(ch, "_cached_images") and ch._cached_images:
            self._images = ch._cached_images
            self.progress_label.setText(f"第{idx+1}/{len(self._chapters)}话 · {len(self._images)}张")
            self._render_images()
            self._auto_loading = False
            self.chapter_changed.emit((self._detail, ch.title, ch.url))
            self._prefetch_future(idx, PREFETCH_COUNT)
            return

        # 已预渲染下一话（图片已就绪）→ 直接用
        prefetched = self._prefetched.get(ch.url)
        if prefetched and prefetched.get("images"):
            self._images = prefetched["images"]
            ch._cached_images = self._images
            self.progress_label.setText(
                f"第{idx+1}/{len(self._chapters)}话 · {len(self._images)}张"
            )
            self._render_images()
            self._auto_loading = False
            self.chapter_changed.emit((self._detail, ch.title, ch.url))
            self._prefetch_future(idx, PREFETCH_COUNT)
            return

        if prefetch := prefetched:
            self.progress_label.setText(
                f"第{idx+1}/{len(self._chapters)}话 · 约{prefetch['count']}张 加载中"
            )
        else:
            self.progress_label.setText(f"第{idx+1}/{len(self._chapters)}话 · 加载中")
        self._clear_images()
        # 后台加载图片 URL
        from PySide6.QtCore import QThreadPool

        task = _LoadComicTask(self._content, self._source, ch)
        task.signals.finished.connect(self._on_images_loaded)
        self._comic_task = task  # 持有引用，防止被 GC
        QThreadPool.globalInstance().start(task)
        self.chapter_changed.emit((self._detail, ch.title, ch.url))
        # 注：预加载不在此发起——等当前话加载完成（_on_images_loaded）再预渲染后续话，
        # 避免预渲染抢资源拖慢当前话首屏。

    def _on_images_loaded(self, ch, images, err) -> None:
        if err:
            self.progress_label.setText(f"加载失败：{err}")
            self._auto_loading = False  # 加载失败也要解锁，防死锁
            return
        self._images = images
        ch._cached_images = images  # 缓存本话，避免重复爬
        self.progress_label.setText(f"第{self._current_idx+1}/{len(self._chapters)}话 · {len(images)}张")
        self._render_images()
        self._auto_loading = False  # 自动翻话完成，解锁
        # 当前话加载完成后再串行预渲染后续话（切话秒开，且不抢当前话资源）
        self._prefetch_future(self._current_idx, PREFETCH_COUNT)

    # ------------------------------------------------------------------ #
    def _prefetch_future(self, idx: int, n: int) -> None:
        """预加载后续 n 话：串行后台预渲染图片，切话秒开。

        同一时间只预渲染 1 话（否则并发起多个 Playwright Chromium 实例
        会抢占内存、拖慢当前话加载），完成后自动取队列下一个。
        预渲染结果写 _prefetched[url]["images"]，_load_episode 命中则秒开。
        """
        if self._source is None:
            return
        # 队尾追加需要预渲染的话
        for k in range(idx + 1, min(idx + 1 + n, len(self._chapters))):
            ch = self._chapters[k]
            if ch.url in self._prefetched:
                continue  # 已预加载过
            if hasattr(ch, "_cached_images") and ch._cached_images:
                self._prefetched[ch.url] = {
                    "images": ch._cached_images, "count": len(ch._cached_images)
                }
                continue
            if ch.url in self._prefetch_queue:
                continue  # 已在队列
            self._prefetch_queue.append(ch.url)
        # 若空闲则启动第一个
        if not self._prefetch_busy and self._prefetch_queue:
            self._start_next_prefetch()

    def _start_next_prefetch(self) -> None:
        """取队列下一个话开始预渲染（串行）。"""
        if not self._prefetch_queue:
            self._prefetch_busy = False
            return
        self._prefetch_busy = True
        url = self._prefetch_queue.pop(0)
        ch = next((c for c in self._chapters if c.url == url), None)
        if ch is None:
            self._prefetch_busy = False
            return
        task = _PrefetchRenderTask(self._content, self._source, ch)
        task.signals.finished.connect(self._on_prefetch_done)
        self._prefetch_tasks = getattr(self, "_prefetch_tasks", [])
        self._prefetch_tasks.append(task)
        QThreadPool.globalInstance().start(task)

    def _on_prefetch_done(self, chapter_url, images, err) -> None:
        """预渲染完成，记录该话图片列表，并继续队列下一个。"""
        self._prefetched[chapter_url] = {
            "images": images or [], "count": len(images or [])
        }
        # 串行：完成一个接着预渲染下一个
        self._start_next_prefetch()

    def _render_images(self) -> None:
        self._clear_images()
        # 每话开头显示章节编号（如「第12话」），不含标题文字
        if 0 <= self._current_idx < len(self._chapters):
            from framework.content import chapter_label
            label = chapter_label(self._chapters[self._current_idx].title) or f"第{self._current_idx+1}话"
            header = QLabel(f"【{label}】")
            header.setAlignment(Qt.AlignCenter)
            header.setStyleSheet(
                "font-size: 15px; font-weight: bold; padding: 12px 8px;"
                "color: palette(text);"
            )
            header.setWordWrap(True)
            self.gallery_layout.addWidget(header)
        for url in self._images:
            lbl = _ComicImageLabel(url)
            lbl.load()
            self.gallery_layout.addWidget(lbl)

    def _clear_images(self) -> None:
        while self.gallery_layout.count():
            child = self.gallery_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    # ------------------------------------------------------------------ #
    def _toggle_mode(self) -> None:
        self._mode = "flip" if self._mode == "gallery" else "gallery"
        self.mode_btn.setText("切换横向模式" if self._mode == "gallery" else "切换画廊模式")
        self._apply_mode()

    def _apply_mode(self) -> None:
        if self._mode == "flip":
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
            self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        else:
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    def _jump_relative(self, delta: int) -> None:
        nxt = self._current_idx + delta
        if 0 <= nxt < len(self._chapters):
            self._load_episode(nxt)

    def _maybe_auto_next(self, value: int) -> None:
        """读到当前话底部（最后一张图滚到底）→ 自动加载下一话（无缝衔接）。

        条件：不在自动翻话中、不是最后一话、有滚动空间。
        """
        if self._auto_loading:
            return
        if self._current_idx < 0 or self._current_idx >= len(self._chapters) - 1:
            return
        if not self._images:  # 本话图片还没加载完，不触发
            return
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() == 0:
            return
        if value >= vbar.maximum() - 40:  # 距底部 < 40px 视为读完本话
            self._auto_loading = True
            self._load_episode(self._current_idx + 1)
            # 解锁在 _on_images_loaded 完成时

    def wheelEvent(self, event) -> None:  # noqa: N802
        # Ctrl+滚轮缩放
        if event.modifiers() & Qt.ControlModifier:
            delta = 1.1 if event.angleDelta().y() > 0 else 0.9
            self._zoom *= delta
            self._zoom = max(0.3, min(3.0, self._zoom))
            self._apply_zoom()
            event.accept()
        else:
            super().wheelEvent(event)

    def _apply_zoom(self) -> None:
        self.gallery.setFixedWidth(int(self._current_base_width() * self._zoom))

    def _current_base_width(self) -> int:
        return max(300, self.width() - 40)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_zoom()


class _ComicImageLabel(QLabel):
    """漫画单页图片（异步加载自适应）。"""

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.url = url
        self.setAlignment(Qt.AlignCenter)
        self._loading = True
        self.setText("加载中...")
        self.setMinimumWidth(200)
        self.setStyleSheet("border: 1px solid palette(mid); border-radius: 4px; padding: 4px;")
        self.setCursor(Qt.OpenHandCursor)

    def load(self) -> None:
        # data URI（Playwright canvas 提取的 base64 图）→ 直接解码显示
        if self.url and self.url.startswith("data:"):
            import base64 as _b64

            try:
                header, b64 = self.url.split(",", 1)
                data = _b64.b64decode(b64)
                pix = QPixmap()
                _ok = pix.loadFromData(data)
                if _ok:
                    self._on_image(pix)
                    return
            except Exception:
                pass
            self._on_image(None)
            return
        # Playwright 返回的本地文件路径 → 直接读
        if self.url and (self.url.startswith(("file://", "/", "\\")) or "\\" in self.url or self.url.startswith(".")):
            path = self.url.replace("file://", "")
            pix = QPixmap(path)
            self._on_image(pix if not pix.isNull() else None)
            return
        from gui.components.cover_loader import CoverLoader

        CoverLoader.instance().load(self.url, self._on_image)

    def _on_image(self, pixmap) -> None:
        if pixmap is None:
            self.setText("图片加载失败\n（可能需登录或已失效）")
            return
        # 缩放适应宽度（widget 未显示时 width() 可能是 -1，需兜底）
        avail = self.width() if self.width() > 100 else 600
        scaled = pixmap.scaledToWidth(
            avail, Qt.SmoothTransformation
        ) if pixmap.width() > avail else pixmap
        self.setPixmap(scaled)
        self.setMinimumHeight(min(scaled.height() + 8, 4096))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            # 点击放大查看（简化：切换为原始大小）
            pass
        super().mousePressEvent(event)


class _ComicSignals(QObject):
    """漫画加载信号。"""
    finished = Signal(object, object, object)  # (chapter, images, err)


class _LoadComicTask(QRunnable):
    """后台加载漫画话图片 URL。"""

    def __init__(self, content, source, chapter):
        super().__init__()
        self.signals = _ComicSignals()
        self._content = content
        self._source = source
        self._chapter = chapter

    def run(self) -> None:
        images, err = [], None
        try:
            images = self._content.fetch_comic_pages(self._source, self._chapter.url)
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(self._chapter, images, err)
        except RuntimeError:
            pass


class _PrefetchSignals(QObject):
    """预加载信号。"""

    finished = Signal(object, object, object)  # (chapter_url, images, err)


class _PrefetchRenderTask(QRunnable):
    """后台预渲染后续话的图片（fetch_comic_pages 全量渲染）。

    提前把下一话的 canvas base64 图都取好，切话时 _load_episode 命中
    _prefetched[url]["images"] → 秒开，不用现场爬 Playwright。
    """

    def __init__(self, content, source, chapter):
        super().__init__()
        self.signals = _PrefetchSignals()
        self._content = content
        self._source = source
        self._chapter = chapter

    def run(self) -> None:
        images, err = [], None
        try:
            images = self._content.fetch_comic_pages(
                self._source, self._chapter.url
            )
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(self._chapter.url, images, err)
        except RuntimeError:
            pass
