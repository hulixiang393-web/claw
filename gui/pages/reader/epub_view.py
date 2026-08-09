"""epub 阅读视图（epub_view.py）。

独立 epub 阅读器：读本地 .epub 文件，不依赖网络源/Content。
- 小说 epub：章节正文 → 滚动阅读 + 字号可调 + 目录侧栏
- 漫画 epub：章节图片流 → 滚动画廊
支持跨章节（上一/下一章）与目录跳转，续读信号（key=epub 路径）。
对应 ui-reader.md 本地阅读扩展。
"""

from __future__ import annotations

import re
import threading

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

# ebooklib 的 zip 文件句柄跨线程读取非线程安全：所有章节内容读取串行加锁
_READ_LOCK = threading.Lock()


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
    """epub 单章索引：标题 + xhtml 资源名。

    打开只建索引（不读任何内容）→ 秒开；渲染该章时才后台读 xhtml
    提取文本（novel）或懒读图片字节（comic，限宽解码）。
    """

    def __init__(self, title: str, src: str = ""):
        self.title = title or "未命名章节"
        self.src = src  # 章节 xhtml 在 epub 内的资源名（读内容用）
        self.is_comic = False  # 渲染该章后由实际内容判定


class _OpenSignals(QObject):
    done = Signal(object, object, object, object)  # (path, chapters, err, item_index)


class _OpenTask(QRunnable):
    """后台只读 epub 元数据建章节索引（不读章节内容）→ 大 epub 秒开。"""

    def __init__(self, path: str):
        super().__init__()
        self.signals = _OpenSignals()
        self._path = path

    def run(self) -> None:
        err, chapters, index = "", [], {}
        try:
            book = epub.read_epub(self._path)
            index = EpubView._build_item_index(book)
            chapters = EpubView._index_chapters(book)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        try:
            self.signals.done.emit(self._path, chapters, err, index)
        except RuntimeError:
            pass


class _LoadSignals(QObject):
    done = Signal(object, object, object, object)  # (idx, kind, text, imgs)


class _LoadChapterTask(QRunnable):
    """后台加载某一章内容：读 xhtml → 判类型 → 提取文本或读全部图片字节。

    慢的部分（zip 读取/正则/图字节）在后台线程，UI 不卡；图解码在 UI 限宽做。
    """

    def __init__(self, idx: int, src: str, item_index: dict):
        super().__init__()
        self.signals = _LoadSignals()
        self._idx = idx
        self._src = src
        self._index = item_index

    def run(self) -> None:
        kind, text, imgs = "novel", "", []
        with _READ_LOCK:  # ebooklib zip 句柄跨线程读需串行
            try:
                html = EpubView._read_document(self._index, self._src)
                if "<img" in (html or "").lower():
                    kind = "comic"
                    for ref in re.findall(r'src="([^"]+)"', html or "", flags=re.IGNORECASE):
                        raw = EpubView._read_resource(self._index, ref)
                        if raw:
                            imgs.append(raw)
                else:
                    kind = "novel"
                    text = _html_to_text(html or "")
            except Exception:  # noqa: BLE001
                kind, text = "novel", ""
        try:
            self.signals.done.emit(self._idx, kind, text, imgs)
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
        self._item_index = {}  # epub 资源名(小写/basename) → item，章节懒读用

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

    def _on_open_done(self, path: str, chapters: list, err: str, item_index=None) -> None:
        """后台建索引完成（主线程回调）。过期结果（已切书）丢弃。"""
        if path != self._path:
            return
        if err or not chapters:
            self.text.setText(f"epub 读取失败：{err or '无可用章节内容'}")
            return
        self._chapters = chapters
        self._item_index = item_index or {}
        self._is_comic = False
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
    def _index_chapters(book) -> list[_Chapter]:
        """只读元数据建章节索引（spine + 标题），不读章节内容 → 大 epub 秒开。"""
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
            title = (getattr(item, "title", "") or "").strip()
            if not title:
                # toc 映射：完整路径或文件名匹配
                leaf = name.rsplit("/", 1)[-1]
                title = (toc_titles.get(name) or toc_titles.get(leaf) or "").strip()
            if not title:
                from pathlib import Path

                title = Path(name).stem  # 无标题时用文件名兜底
            chapters.append(_Chapter(title=title or f"第{len(chapters)+1}章", src=name))
        return chapters

    @staticmethod
    def _build_item_index(book) -> dict:
        """epub 资源名(小写, 含 basename) → item，章节/图片 O(1) 懒读。"""
        index: dict = {}
        for item in book.get_items():
            n = (item.get_name() or "").lower()
            if not n:
                continue
            index[n] = item
            index[n.rsplit("/", 1)[-1]] = item  # basename 也挂一份，兼容 href 写法
        return index

    @staticmethod
    def _read_document(item_index: dict, src: str) -> str:
        """按 xhtml 资源名读文档文本（后台线程用）。"""
        item = EpubView._find_item(item_index, src)
        if item is None:
            return ""
        content = item.get_content()
        if isinstance(content, (bytes, bytearray)):
            return bytes(content).decode("utf-8", errors="replace")
        return str(content or "")

    @staticmethod
    def _read_resource(item_index: dict, ref: str) -> bytes | None:
        """按图片资源名懒读字节（后台线程用）。先精确名再 basename/后缀匹配。"""
        item = EpubView._find_item(item_index, ref)
        if item is None:
            return None
        try:
            c = item.get_content()
            return bytes(c) if isinstance(c, (bytes, bytearray)) else None
        except Exception:  # noqa: BLE001 —— 单资源读取失败跳过
            return None

    @staticmethod
    def _find_item(item_index: dict, ref: str):
        ref = (ref or "").lower()
        item = item_index.get(ref)
        if item is None:
            leaf = ref.rsplit("/", 1)[-1]
            item = item_index.get(leaf)
            if item is None:
                for k, v in item_index.items():
                    if k.endswith("/" + leaf):
                        item = v
                        break
        return item

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
        """加载章节：后台读该章内容（文本/图字节），UI 不阻塞；旧章内容保留到新内容就绪。"""
        if not (0 <= idx < len(self._chapters)):
            return
        self._current_idx = idx
        ch = self._chapters[idx]
        self.toc_list.setCurrentRow(idx)
        self.progress_label.setText(f"第{idx+1}/{len(self._chapters)}章 · {ch.title}")
        task = _LoadChapterTask(idx, ch.src, self._item_index)
        task.signals.done.connect(self._on_chapter_loaded)
        self._load_task = task  # 持有引用防 GC
        QThreadPool.globalInstance().start(task)

    def _on_chapter_loaded(self, idx: int, kind: str, text: str, imgs: list) -> None:
        """章节内容就绪（主线程）。过期结果（已切章）丢弃。"""
        if idx != self._current_idx:
            return
        ch = self._chapters[idx]
        self._is_comic = kind == "comic"
        if kind == "comic":
            self._switch_gallery()
            self._render_comic_imgs(imgs)
        else:
            self._ensure_text_label()  # 漫画章后正文 QLabel 可能已被 setWidget 删除
            self.scroll.setWidget(self.text)
            self.text.setText(f"【{ch.title}】\n\n{text}")
            self.scroll.verticalScrollBar().setValue(0)
        self.chapter_changed.emit((self._path, ch.title))

    def _render_comic_imgs(self, imgs: list) -> None:
        """漫画章：逐张限宽解码（QImageReader 不解码全尺寸，省内存、不卡 UI）。"""
        from PySide6.QtCore import QBuffer, QIODevice, QSize
        from PySide6.QtGui import QImageReader

        while self._gallery_layout.count():
            child = self._gallery_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        if not imgs:
            lbl = QLabel("本话无图片")
            lbl.setAlignment(Qt.AlignCenter)
            self._gallery_layout.addWidget(lbl)
            return
        target_w = self.scroll.width() or 600
        for raw in imgs:
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignCenter)
            try:
                buf = QBuffer()
                buf.setData(raw)
                buf.open(QIODevice.ReadOnly)
                reader = QImageReader(buf)
                size = reader.size()
                if size.isValid() and size.width() > target_w:
                    h = max(1, round(size.height() * target_w / size.width()))
                    reader.setScaledSize(QSize(target_w, h))
                qimg = reader.read()
                buf.close()
                if qimg is not None and not qimg.isNull():
                    lbl.setPixmap(QPixmap.fromImage(qimg))
                else:
                    lbl.setText("图片加载失败")
            except Exception:  # noqa: BLE001 —— 单图解码失败跳过，不拖垮整章
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
