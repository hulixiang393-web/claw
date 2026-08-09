"""epub 阅读视图（epub_view.py）。

独立 epub 阅读器：读本地 .epub 文件，不依赖网络源/Content。
- 小说 epub：章节正文 → 滚动阅读 + 字号可调 + 目录侧栏
- 漫画 epub：章节图片流 → 滚动画廊（限宽解码，不整幅解码）
- 合并单文档（整本书拼一个超大 xhtml，如 2000 章小说/几千图漫画）按
  `<h1>` 拆分章节，拆出的章直接带文本或图片名，渲染时按需读 → 大书秒开不卡。
- 底层用 FastEpub（zip 单遍索引 + 按需读取），不用 ebooklib 全量解析
  （ebooklib read_epub 对 8000+ item 的大书要 20 秒+，本实现毫秒级）。
支持跨章节（上一/下一章）与目录跳转，续读信号（key=epub 路径）。
"""

from __future__ import annotations

import posixpath
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

from framework.epub_reader import (
    FastEpub,
    MERGE_DOC_THRESHOLD,
    _html_to_text,
    split_single_document,
)

# FastEpub 的 zip 文件句柄跨线程读取非线程安全：所有章节内容读取串行加锁
_READ_LOCK = threading.Lock()

# 合并单文档拆分缓存（key=路径+大小+mtime），只保留最近一本，重开同书免重复拆分
_SPLIT_CACHE: dict = {}


def _now() -> float:
    import time

    return time.time()


def _cache_key(path: str) -> tuple:
    import os

    try:
        st = os.stat(path)
        return (path, st.st_size, int(st.st_mtime))
    except OSError:
        return (path, 0, 0)


class _Chapter:
    """epub 单章。普通书 src=xhtml 路径（渲染时读）；合并单文档拆出的章
    直接带 text（小说）或 img_srcs（漫画，字节渲染时懒读）。"""

    def __init__(
        self,
        title: str,
        src: str = "",
        text: str = "",
        img_srcs: list | None = None,
    ):
        self.title = title or "未命名章节"
        self.src = src
        self.text = text
        self.img_srcs = list(img_srcs or [])
        self.is_comic = bool(self.img_srcs)


class _OpenSignals(QObject):
    done = Signal(object, object, object, object)  # (path, chapters, err, epub)


class _OpenTask(QRunnable):
    """后台建章节索引：FastEpub zip 单遍 + 合并单文档拆分 → 大书秒开。"""

    def __init__(self, path: str):
        super().__init__()
        self.signals = _OpenSignals()
        self._path = path

    def run(self) -> None:
        err, chapters, epub = "", [], None
        try:
            epub = FastEpub(self._path)
            chapters = EpubView._build_chapters(epub, self._path)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        try:
            self.signals.done.emit(self._path, chapters, err, epub)
        except RuntimeError:
            pass


class _LoadSignals(QObject):
    done = Signal(object, object, object, object)  # (idx, kind, text, imgs)


class _LoadChapterTask(QRunnable):
    """后台加载某一章内容：拆分章直接取；普通章读 xhtml 判类型提取文本/图字节。"""

    def __init__(self, idx: int, ch: _Chapter, epub: FastEpub):
        super().__init__()
        self.signals = _LoadSignals()
        self._idx = idx
        self._ch = ch
        self._epub = epub

    def run(self) -> None:
        kind, text, imgs = "novel", "", []
        with _READ_LOCK:  # zip 文件句柄跨线程读需串行
            try:
                if self._ch.text:
                    kind, text = "novel", self._ch.text
                elif self._ch.img_srcs:
                    kind = "comic"
                    for ref in self._ch.img_srcs:
                        raw = EpubView._read_bytes(self._epub, self._ch.src, ref)
                        if raw:
                            imgs.append(raw)
                elif self._epub is not None:
                    html = EpubView._read_doc(self._epub, self._ch.src)
                    if "<img" in (html or "").lower():
                        kind = "comic"
                        for ref in re.findall(r'src="([^"]+)"', html or "", flags=re.IGNORECASE):
                            raw = EpubView._read_bytes(self._epub, self._ch.src, ref)
                            if raw:
                                imgs.append(raw)
                    else:
                        kind, text = "novel", _html_to_text(html or "")
            except Exception:  # noqa: BLE001
                kind, text = "novel", ""
        try:
            self.signals.done.emit(self._idx, kind, text, imgs)
        except RuntimeError:
            pass


class EpubView(QWidget):
    """epub 阅读视图（本地文件）。"""

    chapter_changed = Signal(object)  # 发 (epub_path, chapter_title) 供续读
    position_changed = Signal(object)  # 发 (epub_path, 章内滚动比例 0~1)，节流

    def __init__(self, font_scale: float = 1.0, parent=None):
        super().__init__(parent)
        self._path = ""
        self._chapters: list[_Chapter] = []
        self._current_idx = -1
        self._is_comic = False
        self._font_delta = 0
        self._base_font = self._clamp_font(round(17 * float(font_scale or 1.0)))
        self._epub: FastEpub | None = None  # 当前书读取器（zip 按需读取）
        self._last_pos_emit = 0.0  # 章内位置节流
        self._pending_pos = 0.0  # 待恢复的章内滚动比例（续读定位用，恢复后清零）

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
        self.prev_btn.setFixedWidth(70)
        self.next_btn.setFixedWidth(70)
        self.prev_btn.clicked.connect(lambda: self._jump_relative(-1))
        self.next_btn.clicked.connect(lambda: self._jump_relative(1))
        toolbar.addWidget(self.prev_btn)
        toolbar.addWidget(self.next_btn)

        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("color: palette(dark);")
        toolbar.addWidget(self.progress_label, stretch=1)
        layout.addLayout(toolbar)

        # ---- 主体：目录 + 正文 ----
        body = QHBoxLayout()
        body.setSpacing(0)

        self.toc_list = QListWidget()
        self.toc_list.setFixedWidth(180)
        self.toc_list.itemClicked.connect(self._on_toc_clicked)
        self.toc_list.setVisible(False)
        body.addWidget(self.toc_list)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

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
        """异步打开本地 epub。立即返回；后台建索引完成后回调 on_loaded(chapters)。

        on_loaded 在主线程执行，可在此做续读定位（按章节标题）等收尾。
        """
        self._ensure_text_label()  # 上次打开混排 epub 可能已删掉正文 QLabel
        import os

        path = os.path.abspath(path)  # 统一绝对路径（进度/续读 key 一致）
        self._path = path
        self._start_idx = max(0, int(start_idx or 0))
        self._on_loaded = on_loaded
        self._chapters = []
        self._is_comic = False
        self._pending_pos = 0.0
        self.text.setText("正在打开 epub，请稍候…")
        self.scroll.setWidget(self.text)
        task = _OpenTask(path)
        task.signals.done.connect(self._on_open_done)
        self._open_task = task  # 持有引用防 GC
        QThreadPool.globalInstance().start(task)
        return True

    def _on_open_done(self, path: str, chapters: list, err: str, epub=None) -> None:
        """后台建索引完成（主线程回调）。过期结果（已切书）丢弃。"""
        if path != self._path:
            if epub is not None:
                epub.close()
            return
        if err or not chapters:
            if epub is not None:
                epub.close()
            self.text.setText(f"epub 读取失败：{err or '无可用章节内容'}")
            return
        self._chapters = chapters
        if self._epub is not None:
            self._epub.close()
        self._epub = epub
        self._is_comic = False
        self._populate_toc()
        idx = max(0, min(self._start_idx, len(self._chapters) - 1))
        self._load_chapter(idx)
        if self._on_loaded is not None:
            try:
                self._on_loaded(chapters)
            except Exception:  # noqa: BLE001 —— 续读收尾失败不影响阅读
                pass

    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_chapters(epub: FastEpub, path: str) -> list[_Chapter]:
        """建章节索引。合并单文档（spine 一个且很大）按 <h1> 拆分并缓存。"""
        key = _cache_key(path)
        cached = _SPLIT_CACHE.get(key)
        if cached is not None:
            return list(cached)
        hrefs = epub.spine_hrefs
        if len(hrefs) == 1 and epub.file_size(hrefs[0]) > MERGE_DOC_THRESHOLD:
            html = EpubView._read_doc(epub, hrefs[0])
            parts = split_single_document(html)
            if len(parts) > 1:
                chapters = [
                    _Chapter(t, src=hrefs[0], text=txt, img_srcs=srcs)
                    for t, txt, srcs in parts
                ]
                _SPLIT_CACHE.clear()  # 只保留最近一本，避免多本拆分结果堆积内存
                _SPLIT_CACHE[key] = chapters
                return list(chapters)
        chapters: list[_Chapter] = []
        for href in hrefs:
            leaf = posixpath.basename(href)
            title = (epub.toc_titles.get(href) or epub.toc_titles.get(leaf) or "").strip()
            if not title:
                title = posixpath.splitext(leaf)[0]
            chapters.append(_Chapter(title or f"第{len(chapters)+1}章", src=href))
        return chapters

    @staticmethod
    def _read_doc(epub: FastEpub, src: str) -> str:
        """按 zip 内路径读正文文档文本（后台线程用）。"""
        if not src or epub is None or not epub.has(src):
            return ""
        raw = epub.read(src)
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw).decode("utf-8", errors="replace")
        return str(raw or "")

    @staticmethod
    def _read_bytes(epub: FastEpub, base: str, ref: str) -> bytes | None:
        """按引用读单资源字节（后台线程用）。先原样/相对 base 路径，再 basename 匹配。"""
        if epub is None or not ref:
            return None
        base_dir = posixpath.dirname(base or "")
        cands = [ref, posixpath.join(base_dir, ref)]
        for c in cands:
            if epub.has(c):
                try:
                    raw = epub.read(c)
                    return bytes(raw) if isinstance(raw, (bytes, bytearray)) else None
                except Exception:  # noqa: BLE001
                    return None
        full = epub.basename_index.get(posixpath.basename(ref))
        if full:
            try:
                raw = epub.read(full)
                return bytes(raw) if isinstance(raw, (bytes, bytearray)) else None
            except Exception:  # noqa: BLE001
                pass
        return None

    # ------------------------------------------------------------------ #
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
        self.toc_list.setUpdatesEnabled(False)
        try:
            self.toc_list.addItems([c.title or f"第{i+1}章" for i, c in enumerate(self._chapters)])
        finally:
            self.toc_list.setUpdatesEnabled(True)

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
        task = _LoadChapterTask(idx, ch, self._epub)
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
            self.scroll.verticalScrollBar().setValue(0)
        else:
            self._ensure_text_label()  # 漫画章后正文 QLabel 可能已被 setWidget 删除
            self.scroll.setWidget(self.text)
            self.text.setText(f"【{ch.title}】\n\n{text}")
            self.scroll.verticalScrollBar().setValue(0)
            # 续读：恢复上次章内滚动位置（延迟到文本布局完成后）
            if self._pending_pos > 0:
                pos = self._pending_pos
                self._pending_pos = 0.0
                from PySide6.QtCore import QTimer

                QTimer.singleShot(0, lambda p=pos: self.scroll.verticalScrollBar().setValue(
                    int(p * self.scroll.verticalScrollBar().maximum())
                ))
        self.chapter_changed.emit((self._path, ch.title))

    def _on_scroll(self, value: int) -> None:
        """章内滚动 → 节流记录阅读比例（记住读到哪）。"""
        if not self._path:
            return
        max_v = self.scroll.verticalScrollBar().maximum()
        ratio = (value / max_v) if max_v > 0 else 0.0
        now = _now()
        if now - self._last_pos_emit < 0.4:
            return
        self._last_pos_emit = now
        try:
            self.position_changed.emit((self._path, round(ratio, 4)))
        except RuntimeError:
            pass

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
