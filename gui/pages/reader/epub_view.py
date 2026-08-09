"""epub 阅读视图（epub_view.py）。

独立 epub 阅读器：读本地 .epub 文件，不依赖网络源/Content。
- 小说 epub：章节正文 → 滚动阅读 + 字号可调 + 目录侧栏
- 漫画 epub：章节图片流 → 滚动画廊
支持跨章节（上一/下一章）与目录跳转，续读信号（key=epub 路径）。
对应 ui-reader.md 本地阅读扩展。
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, QObject, QRunnable, QThreadPool, Signal
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

try:
    import ebooklib
    from ebooklib import epub

    HAS_EBOOKLIB = True
except Exception:  # noqa: BLE001
    HAS_EBOOKLIB = False


def _html_to_text(html: str) -> str:
    """epub 章节 XHTML → 纯文本（段落换行，去标签）。"""
    # 提取 body 内文本，<p>/<br> → 换行，其余标签去除
    text = html or ""
    # 段落换行
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _chapter_title(item, html: str) -> str:
    """提取章节标题：item.title → HTML <title> → 文件名（真实 epub 多无 item.title）。"""
    t = (getattr(item, "title", "") or "").strip()
    if t:
        return t
    m = re.search(r"<title[^>]*>([^<]+)</title>", html or "", flags=re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return ""


class _Chapter:
    """epub 单章：标题 + 文本（novel）或图片资源名列表（comic，懒加载）。

    img_srcs 只记 epub 内图片资源名，打开时**不读图字节**；渲染该章时才
    从 book 懒读，大漫画 epub 打开快、内存省。
    """

    def __init__(
        self,
        title: str,
        text: str = "",
        img_srcs: list[str] | None = None,
        images: list[bytes] | None = None,
    ):
        self.title = title or "未命名章节"
        self.text = text
        self.img_srcs = list(img_srcs or [])  # 图片资源名（懒加载）
        self.images = list(images or [])  # 兼容旧字段（已读字节）
        self.is_comic = bool(self.img_srcs or self.images)


class _OpenSignals(QObject):
    done = Signal(object, object, object, object, object)  # (path, chapters, err, book, image_index)


class _OpenTask(QRunnable):
    """后台解析 epub：只读章节结构 + 文本，图片字节懒加载（大 epub 打开快）。"""

    def __init__(self, path: str):
        super().__init__()
        self.signals = _OpenSignals()
        self._path = path

    def run(self) -> None:
        err, chapters, book, index = "", [], None, {}
        try:
            book = epub.read_epub(self._path)
            index = EpubView._build_image_index(book)
            chapters = EpubView._extract_chapters_static(book)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        try:
            self.signals.done.emit(self._path, chapters, err, book, index)
        except RuntimeError:
            pass


class EpubView(QWidget):
    """epub 阅读视图（本地文件）。"""

    chapter_changed = Signal(object)  # 发 (epub_path, chapter_title) 供续读

    def __init__(self, font_scale: float = 1.0, parent=None):
        super().__init__(parent)
        self._path = ""
        self._chapters: list[_Chapter] = []
        self._current_idx = -1
        self._is_comic = False
        self._font_delta = 0
        self._base_font = self._clamp_font(round(17 * float(font_scale or 1.0)))
        self._book = None          # epub 解析对象（懒读图用，本地文件长期持有）
        self._image_index = {}     # 图片资源名 → ebooklib item

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- 工具栏 ----
        toolbar = QHBoxLayout()
        self.toc_btn = QToolButton()
        self.toc_btn.setText("☰ 目录")
        self.toc_btn.clicked.connect(self._toggle_toc)
        toolbar.addWidget(self.toc_btn)

        self.font_down = QPushButton("A-")
        self.font_up = QPushButton("A+")
        self.font_down.setFixedWidth(40)
        self.font_up.setFixedWidth(40)
        self.font_down.clicked.connect(lambda: self._adjust_font(-1))
        self.font_up.clicked.connect(lambda: self._adjust_font(1))
        toolbar.addWidget(self.font_down)
        toolbar.addWidget(self.font_up)

        self.prev_btn = QPushButton("上一章")
        self.next_btn = QPushButton("下一章")
        self.prev_btn.clicked.connect(lambda: self._jump_relative(-1))
        self.next_btn.clicked.connect(lambda: self._jump_relative(1))
        toolbar.addWidget(self.prev_btn)
        toolbar.addWidget(self.next_btn)

        toolbar.addStretch(1)
        self.progress_label = QLabel("")
        toolbar.addWidget(self.progress_label)
        layout.addLayout(toolbar)

        # ---- 目录 + 正文 ----
        body = QHBoxLayout()
        body.setSpacing(8)

        self.toc_list = QListWidget()
        self.toc_list.setFixedWidth(180)
        self.toc_list.itemClicked.connect(self._on_toc_clicked)
        self.toc_list.setVisible(False)
        body.addWidget(self.toc_list)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 正文容器：小说用 QLabel，漫画用垂直图流
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._apply_font()
        self.scroll.setWidget(self.text)
        body.addWidget(self.scroll, stretch=1)
        layout.addLayout(body, stretch=1)

    # ------------------------------------------------------------------ #
    def open(self, path: str, start_idx: int = 0, on_loaded=None) -> bool:
        """异步打开本地 epub。立即返回；后台解析完成后回调 on_loaded(chapters)。

        on_loaded 在主线程执行，可在此做续读定位（按章节标题）等收尾。
        """
        self._ensure_text_label()  # 上次打开混排 epub 可能已删掉正文 QLabel
        if not HAS_EBOOKLIB:
            self.text.setText("未安装 EbookLib，无法阅读 epub")
            return False
        self._path = path
        self._start_idx = max(0, int(start_idx or 0))
        self._on_loaded = on_loaded
        self._chapters = []
        self._is_comic = False
        self.text.setText("正在打开 epub，请稍候…")
        self.scroll.setWidget(self.text)
        task = _OpenTask(path)
        task.signals.done.connect(self._on_open_done)
        self._open_task = task  # 持有引用防 GC
        QThreadPool.globalInstance().start(task)
        return True

    def _on_open_done(
        self, path: str, chapters: list, err: str, book=None, image_index=None
    ) -> None:
        """后台解析完成（主线程回调）。过期结果（已切书）丢弃。"""
        if path != self._path:
            return
        if err or not chapters:
            self.text.setText(f"epub 读取失败：{err or '无可用章节内容'}")
            return
        self._chapters = chapters
        self._book = book
        self._image_index = image_index or {}
        self._is_comic = any(ch.is_comic for ch in chapters)
        # 漫画视图：切画廊模式
        if self._is_comic:
            self._switch_gallery()
        self._populate_toc()
        idx = max(0, min(self._start_idx, len(self._chapters) - 1))
        self._load_chapter(idx)
        if self._on_loaded is not None:
            try:
                self._on_loaded(chapters)
            except Exception:  # noqa: BLE001 —— 续读收尾失败不影响阅读
                pass

    @staticmethod
    def _toc_titles(book) -> dict:
        """从 book.toc（navMap）展平 href→章节标题。真实 epub 的标题在此可靠。

        ebooklib read_epub 会 lxml 重写 xhtml 并剥掉 <title>，因此章节标题
        以目录（toc.ncx / nav）为准；item.title / <title> 仅作补充。
        """
        titles: dict[str, str] = {}

        def walk(entries) -> None:
            if not entries:
                return
            for e in entries:
                if isinstance(e, tuple) and len(e) >= 2:
                    head, rest = e[0], e[1]
                    if hasattr(head, "href"):
                        titles[head.href] = getattr(head, "title", "") or ""
                    walk(rest)
                elif hasattr(e, "href"):
                    titles[e.href] = getattr(e, "title", "") or ""

        walk(book.toc)
        return titles

    @staticmethod
    def _extract_chapters_static(book) -> list[_Chapter]:
        """静态提取章节（后台线程用，无 UI 依赖）。"""
        chapters: list[_Chapter] = []
        toc_titles = EpubView._toc_titles(book)
        items = {i.get_id(): i for i in book.get_items()}
        for idref, _linear in book.spine:
            item = items.get(idref)
            if item is None:
                continue
            name = (item.get_name() or "").lower()
            if not name.endswith((".xhtml", ".html")):
                continue
            # 跳过导航/封面等非正文文档
            if idref in ("nav", "ncx") or "nav" in name:
                continue
            content = item.get_content()
            if isinstance(content, (bytes, bytearray)):
                html = bytes(content).decode("utf-8", errors="replace")
            else:
                html = str(content)
            title = _chapter_title(item, html)
            if not title:
                # toc 映射：完整路径或文件名匹配
                leaf = name.rsplit("/", 1)[-1]
                title = (toc_titles.get(name) or toc_titles.get(leaf) or "").strip()
            if not title:
                from pathlib import Path

                title = Path(name).stem  # 无标题时用文件名兜底（比"未命名"可读）
            # 检测本话图片：只记录资源名，字节等渲染该章时再懒读（大 epub 打开快）
            img_refs = re.findall(r'src="([^"]+)"', html, flags=re.IGNORECASE)
            img_refs = [r for r in img_refs if r]
            if img_refs:
                chapters.append(_Chapter(title, img_srcs=img_refs))
            else:
                chapters.append(_Chapter(title, text=_html_to_text(html)))
        return chapters

    def _extract_chapters(self, book) -> list[_Chapter]:
        return EpubView._extract_chapters_static(book)

    @staticmethod
    def _build_image_index(book) -> dict:
        """图片资源名 → item 索引（含去前缀/带路径两种键），渲染时 O(1) 查找。"""
        index: dict = {}
        for item in book.get_items():
            if item.get_type() != ebooklib.ITEM_IMAGE:
                continue
            n = item.get_name() or ""
            if not n:
                continue
            index[n] = item
            index[n.rsplit("/", 1)[-1]] = item  # basename 也挂一份，兼容 href 写法
        return index

    def _read_image(self, src: str) -> bytes | None:
        """懒读单张图片字节（渲染该章时调用）。先精确名，再 basename 匹配。"""
        item = self._image_index.get(src)
        if item is None:
            leaf = src.rsplit("/", 1)[-1]
            item = self._image_index.get(leaf)
            if item is None:
                for k, v in self._image_index.items():
                    if k.endswith("/" + leaf):
                        item = v
                        break
        if item is None:
            return None
        try:
            c = item.get_content()
            return bytes(c) if isinstance(c, (bytes, bytearray)) else None
        except Exception:  # noqa: BLE001 —— 单图读取失败跳过
            return None

    def _switch_gallery(self) -> None:
        """漫画模式：改用图片流容器（setWidget 会删除旧 widget，被删则重建）。"""
        from PySide6.QtWidgets import QVBoxLayout as _V
        import shiboken6

        gal = getattr(self, "_gallery_widget", None)
        if gal is None or not shiboken6.isValid(gal):
            self._gallery_widget = QWidget()
            self._gallery_layout = _V(self._gallery_widget)
            self._gallery_layout.setContentsMargins(0, 0, 0, 0)
            self._gallery_layout.setSpacing(4)
            self._gallery_layout.setAlignment(Qt.AlignHCenter)
        self.scroll.setWidget(self._gallery_widget)

    def _ensure_text_label(self) -> None:
        """正文 QLabel 被 setWidget 切换删除时重建（混排 epub 漫画/小说来回切）。"""
        import shiboken6

        if shiboken6.isValid(self.text):
            return
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # 直接套字号样式：_apply_font 在漫画模式（_is_comic）会提前返回，
        # 混排 epub 重建正文 QLabel 时需独立应用（否则重建后无样式）。
        size = self._clamp_font(self._base_font + self._font_delta)
        self.text.setStyleSheet(
            f"font-size: {size}px; line-height: 1.8; padding: 8px 12px;"
        )

    def _populate_toc(self) -> None:
        self.toc_list.clear()
        for i, ch in enumerate(self._chapters):
            item = QListWidgetItem(ch.title or f"第{i+1}章")
            item.setData(Qt.UserRole, i)
            self.toc_list.addItem(item)

    def _on_toc_clicked(self, item) -> None:
        idx = item.data(Qt.UserRole)
        self._load_chapter(idx)

    def _toggle_toc(self) -> None:
        self.toc_list.setVisible(not self.toc_list.isVisible())

    def _jump_relative(self, delta: int) -> None:
        nxt = self._current_idx + delta
        if 0 <= nxt < len(self._chapters):
            self._load_chapter(nxt)

    # ------------------------------------------------------------------ #
    def _load_chapter(self, idx: int) -> None:
        if not (0 <= idx < len(self._chapters)):
            return
        self._current_idx = idx
        ch = self._chapters[idx]
        self.toc_list.setCurrentRow(idx)
        self.progress_label.setText(f"第{idx+1}/{len(self._chapters)}章 · {ch.title}")
        if ch.is_comic:
            self._render_comic_chapter(ch)
        else:
            self._ensure_text_label()  # 漫画章后正文 QLabel 可能已被 setWidget 删除
            self.scroll.setWidget(self.text)
            self.text.setText(f"【{ch.title}】\n\n{ch.text}")
            self.scroll.verticalScrollBar().setValue(0)
        self.chapter_changed.emit((self._path, ch.title))

    def _render_comic_chapter(self, ch: _Chapter) -> None:
        # 小说章后点漫画章：scroll 当前 widget 仍是 text → 必须 setWidget 切回画廊，
        # 否则漫画内容画在 text 上不显示（空白/残留旧小说正文）。
        self._switch_gallery()
        while self._gallery_layout.count():
            child = self._gallery_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        for src in ch.img_srcs:
            raw = self._read_image(src)  # 懒加载：仅渲染本章时才读图字节
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignCenter)
            pix = QPixmap()
            if raw and pix.loadFromData(raw):
                lbl.setPixmap(pix.scaledToWidth(self.scroll.width() or 600, Qt.SmoothTransformation))
            else:
                lbl.setText("图片加载失败")
            self._gallery_layout.addWidget(lbl)

    @staticmethod
    def _clamp_font(size: int) -> int:
        return max(12, min(28, size))

    def set_font_scale(self, scale: float) -> None:
        """外部设置字体缩放（设置页实时生效）。保留用户 A+/A- 微调量。"""
        if self._is_comic:
            return
        self._base_font = self._clamp_font(round(17 * float(scale or 1.0)))
        self._apply_font()

    def _apply_font(self) -> None:
        if self._is_comic:
            return
        self._ensure_text_label()  # 被删则重建，避免操作失效对象
        size = self._clamp_font(self._base_font + self._font_delta)
        self.text.setStyleSheet(
            f"font-size: {size}px; line-height: 1.8; padding: 8px 12px;"
        )

    def _adjust_font(self, delta: int) -> None:
        if self._is_comic:
            return
        self._font_delta += delta
        self._apply_font()

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        pass
