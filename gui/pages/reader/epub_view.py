"""epub 阅读视图（epub_view.py）。

独立 epub 阅读器：读本地 .epub 文件，不依赖网络源/Content。
- 小说 epub：章节正文 → 滚动阅读 + 字号可调 + 目录侧栏
- 漫画 epub：章节图片流 → 滚动画廊
支持跨章节（上一/下一章）与目录跳转，续读信号（key=epub 路径）。
对应 ui-reader.md 本地阅读扩展。
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
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


class _Chapter:
    """epub 单章：标题 + 文本（novel）或图片列表（comic）。"""

    def __init__(self, title: str, text: str = "", images: list[bytes] | None = None):
        self.title = title or "未命名章节"
        self.text = text
        self.images = images or []  # 漫画：本话图片字节
        self.is_comic = bool(images)


class EpubView(QWidget):
    """epub 阅读视图（本地文件）。"""

    chapter_changed = Signal(object)  # 发 (epub_path, chapter_title) 供续读

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path = ""
        self._chapters: list[_Chapter] = []
        self._current_idx = -1
        self._is_comic = False
        self._font_size = 17

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
        self.text.setStyleSheet(
            f"font-size: {self._font_size}px; line-height: 1.8; padding: 8px 12px;"
        )
        self.scroll.setWidget(self.text)
        body.addWidget(self.scroll, stretch=1)
        layout.addLayout(body, stretch=1)

    # ------------------------------------------------------------------ #
    def open(self, path: str) -> bool:
        """打开本地 epub，返回是否成功。"""
        if not HAS_EBOOKLIB:
            self.text.setText("未安装 EbookLib，无法阅读 epub")
            return False
        self._path = path
        try:
            book = epub.read_epub(path)
            self._chapters = self._extract_chapters(book)
        except Exception as exc:  # noqa: BLE001
            self.text.setText(f"epub 读取失败：{exc}")
            return False
        if not self._chapters:
            self.text.setText("epub 无可用章节内容")
            return False
        self._is_comic = any(ch.is_comic for ch in self._chapters)
        # 漫画视图：切画廊模式
        if self._is_comic:
            self._switch_gallery()
        self._populate_toc()
        self._load_chapter(0)
        return True

    def _extract_chapters(self, book) -> list[_Chapter]:
        """从 ebooklib book 提取章节（按 spine 顺序，跳过 nav/cover）。"""
        chapters: list[_Chapter] = []
        # spine 顺序：book.spine 是 [(idref, linear)]；用 item map 定位
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
            title = getattr(item, "title", "") or ""
            # 检测本话图片
            img_refs = re.findall(r'src="([^"]+)"', html, flags=re.IGNORECASE)
            imgs = [self._image_bytes(book, ref) for ref in img_refs]
            imgs = [b for b in imgs if b]
            if imgs:
                chapters.append(_Chapter(title, images=imgs))
            else:
                chapters.append(_Chapter(title, text=_html_to_text(html)))
        return chapters

    @staticmethod
    def _image_bytes(book, ref: str) -> bytes | None:
        """按 epub 内资源名取图片字节。"""
        # 去掉可能的相对路径前缀
        name = ref
        for item in book.get_items():
            n = item.get_name() or ""
            if n == name or n.endswith("/" + name) or name.endswith("/" + n):
                if item.get_type() == ebooklib.ITEM_IMAGE:
                    c = item.get_content()
                    return bytes(c) if isinstance(c, (bytes, bytearray)) else None
        return None

    def _switch_gallery(self) -> None:
        """漫画模式：改用图片流容器。"""
        from PySide6.QtWidgets import QVBoxLayout as _V

        if getattr(self, "_gallery_widget", None) is None:
            self._gallery_widget = QWidget()
            self._gallery_layout = _V(self._gallery_widget)
            self._gallery_layout.setContentsMargins(0, 0, 0, 0)
            self._gallery_layout.setSpacing(4)
            self._gallery_layout.setAlignment(Qt.AlignHCenter)
        self.scroll.setWidget(self._gallery_widget)

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
            self.scroll.setWidget(self.text)
            self.text.setText(f"【{ch.title}】\n\n{ch.text}")
            self.scroll.verticalScrollBar().setValue(0)
        self.chapter_changed.emit((self._path, ch.title))

    def _render_comic_chapter(self, ch: _Chapter) -> None:
        while self._gallery_layout.count():
            child = self._gallery_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        for raw in ch.images:
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignCenter)
            pix = QPixmap()
            if pix.loadFromData(raw):
                lbl.setPixmap(pix.scaledToWidth(self.scroll.width() or 600, Qt.SmoothTransformation))
            else:
                lbl.setText("图片解码失败")
            self._gallery_layout.addWidget(lbl)

    def _adjust_font(self, delta: int) -> None:
        if self._is_comic:
            return
        self._font_size += delta
        self._font_size = max(12, min(28, self._font_size))
        self.text.setStyleSheet(
            f"font-size: {self._font_size}px; line-height: 1.8; padding: 8px 12px;"
        )

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        pass
