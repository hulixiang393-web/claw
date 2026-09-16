"""epub 阅读视图（epub_view.py）。

独立 epub 阅读器：读本地 .epub 文件，不依赖网络源/Content。
- 小说 epub：章节正文 → 滚动阅读 + 字号可调 + 目录侧栏
- 漫画 epub：章节图片流 → 滚动画廊（限宽解码不整幅解码；后台并发解码 +
  等高占位回填，首屏秒出、不冻结 UI）
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

from PySide6.QtCore import Qt, QObject, QRunnable, QThreadPool, QTimer, Signal
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


class _ComicDecodeSignals(QObject):
    done = Signal(object, object, object)  # (gen, idx, QImage)


class _ComicDecodeTask(QRunnable):
    """后台限宽解码单张漫画页字节 → QImage（gen 过期结果由宿主丢弃）。"""

    def __init__(self, gen: int, idx: int, raw: bytes, target_w: int):
        super().__init__()
        self.signals = _ComicDecodeSignals()
        self._gen = gen
        self._idx = idx
        self._raw = raw
        self._target_w = target_w

    def run(self) -> None:
        from PySide6.QtCore import QBuffer, QIODevice, QSize
        from PySide6.QtGui import QImage, QImageReader

        img = QImage()
        try:
            buf = QBuffer()
            buf.setData(self._raw)
            buf.open(QIODevice.ReadOnly)
            reader = QImageReader(buf)
            size = reader.size()
            if size.isValid() and size.width() > self._target_w:
                h = max(1, round(size.height() * self._target_w / size.width()))
                reader.setScaledSize(QSize(self._target_w, h))
            img = reader.read()
            buf.close()
        except Exception:  # noqa: BLE001 —— 单页解码失败跳过，不拖垮整章
            img = QImage()
        try:
            self.signals.done.emit(self._gen, self._idx, img)
        except RuntimeError:
            pass


class EpubView(QWidget):
    """epub 阅读视图（本地文件）。"""

    chapter_changed = Signal(object)  # 发 (epub_path, chapter_title) 供续读
    position_changed = Signal(object)  # 发 (epub_path, 章内滚动比例 0~1)，节流
    fullscreen_requested = Signal()  # 工具条 ⛶ → ReaderPage 切主窗全屏
    background_cycle_requested = Signal()  # 「背景」按钮 → ReaderPage 循环切换护眼背景色

    def __init__(self, font_scale: float = 1.0, parent=None):
        super().__init__(parent)
        self._path = ""
        self._chapters: list[_Chapter] = []
        self._current_idx = -1
        self._is_comic = False
        self._zoom = 1.0  # 漫画图流缩放（Ctrl+滚轮，0.25~4.0）
        self._comic_imgs: list = []  # 当前话图片原始字节缓存（缩放时重新解码）
        self._comic_gen = 0  # 漫画渲染代际：切章/缩放自增，旧异步解码结果丢弃
        self._comic_labels: list = []  # 当前话占位 label（解码完成回填）
        self._comic_tasks: list = []  # 持引用防 GC
        self._zoom_timer = QTimer(self)  # Ctrl+滚轮防抖：滚轮连发只重排一次
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.setInterval(150)
        self._zoom_timer.timeout.connect(self._on_zoom_timeout)
        self._font_delta = 0
        self._base_font = self._clamp_font(round(17 * float(font_scale or 1.0)))
        self._epub: FastEpub | None = None  # 当前书读取器（zip 按需读取）
        self._last_pos_emit = 0.0  # 章内位置节流
        self._pending_pos = 0.0  # 待恢复的章内滚动比例（续读定位用，恢复后清零）
        self._reading_bg = ""  # 阅读区独立背景色（空=透明跟随主题）
        self._reading_fg = ""  # 夜间黑等深色背景下的正文前景色（空=跟随主题）

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

        self.bg_btn = QPushButton("背景")
        self.bg_btn.setFixedWidth(50)
        self.bg_btn.setToolTip("切换阅读背景色（白/米黄/护眼绿/夜间黑）")
        self.bg_btn.clicked.connect(self.background_cycle_requested.emit)
        toolbar.addWidget(self.bg_btn)

        self.fullscreen_btn = QPushButton("⛶")
        self.fullscreen_btn.setFixedWidth(40)
        self.fullscreen_btn.setToolTip("全屏阅读")
        self.fullscreen_btn.clicked.connect(self.fullscreen_requested.emit)
        toolbar.addWidget(self.fullscreen_btn)

        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("color: palette(dark);")
        toolbar.addWidget(self.progress_label, stretch=1)
        layout.addLayout(toolbar)

        # ---- 主体：目录 + 正文 ----
        body = QHBoxLayout()
        body.setSpacing(0)

        self.toc_list = QListWidget()
        self.toc_list.setFixedWidth(180)
        self.toc_list.setWordWrap(True)  # 长标题换行完整显示，不被截断
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

        # 鼠标侧键翻章 + Ctrl+滚轮字号：应用级事件过滤器（正文 QLabel 带
        # TextSelectableByMouse 会吞掉子控件级鼠标事件，需覆盖滚动区/正文等）。
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

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
        self._zoom = 1.0
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
        # 漫画图流宽度由内容决定（缩放后比视口宽时可横向滚动）：
        # widgetResizable 会把 gallery 压缩到视口宽，宽图被裁剪且无法滚动
        self.scroll.setWidgetResizable(False)
        self.scroll.setWidget(self._gallery_widget)

    def _ensure_text_label(self) -> None:
        """正文 QLabel 被 setWidget 切换删除时重建（混排 epub 漫画/小说来回切）。"""
        import shiboken6

        # 从漫画图流切回文本：恢复 widgetResizable + 关横向滚动
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        if shiboken6.isValid(self.text):
            return
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # 直接套正文样式：_apply_font 在漫画模式（_is_comic）会提前返回，
        # 混排 epub 重建正文 QLabel 时需独立应用（否则重建后无样式/背景色）。
        self.text.setStyleSheet(self._body_css())

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
        # 背景色应用到滚动区（漫画图流模式 galley 可能刚重建）
        self._apply_viewport_bg()

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

    def _render_comic_imgs(self, imgs: list, preserve_ratio: bool = False) -> None:
        """漫画章：后台并发限宽解码 + 占位回填（首屏秒出，不冻结 UI）。

        整章同步解码（60 页 ≈ 2s）会卡住界面——改为：主线程预读每张尺寸
        （读头部，毫秒级）建等高占位 label（滚动/续读位置不跳），再交给
        线程池并发解码、完成逐张回填。Ctrl+滚轮缩放重走一遍（gen 过期
        丢弃旧渲染）；preserve_ratio=True（缩放）时重排后按阅读比例恢复
        滚动位置，切章走 setValue(0) 从顶部开始。
        """
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImageReader

        self._comic_imgs = list(imgs)
        self._comic_gen += 1
        gen = self._comic_gen
        self._comic_tasks = []
        vbar = self.scroll.verticalScrollBar()
        ratio = (vbar.value() / vbar.maximum()) if vbar.maximum() > 0 else 0.0
        while self._gallery_layout.count():
            child = self._gallery_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        if not imgs:
            lbl = QLabel("本话无图片")
            lbl.setAlignment(Qt.AlignCenter)
            self._gallery_layout.addWidget(lbl)
            return
        vp = self.scroll.viewport().width() or 600
        target_w = int(vp * self._zoom)
        # 缩放后比视口宽 → 开横向滚动、gallery 定宽为内容宽；否则关滚动跟视口宽
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOn if target_w > vp else Qt.ScrollBarAlwaysOff
        )
        self._gallery_widget.setFixedWidth(max(target_w, vp))
        # 预读尺寸 → 占位等高（与解码结果一致，回填不跳位置）
        heights = []
        for raw in imgs:
            h = 700
            try:
                buf = QBuffer()
                buf.setData(raw)
                buf.open(QIODevice.ReadOnly)
                s = QImageReader(buf).size()
                buf.close()
                if s.isValid() and s.width():
                    h = s.height() if s.width() <= target_w else round(
                        s.height() * target_w / s.width()
                    )
                    h = max(200, min(4096, h))
            except Exception:  # noqa: BLE001 —— 尺寸读不到用默认占位
                pass
            heights.append(h)
        self._comic_labels = []
        for i, h in enumerate(heights):
            lbl = QLabel("加载中…")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setMinimumWidth(max(target_w, vp))
            lbl.setMinimumHeight(h)
            self._gallery_layout.addWidget(lbl)
            self._comic_labels.append(lbl)
        for i, raw in enumerate(imgs):
            task = _ComicDecodeTask(gen, i, raw, target_w)
            task.signals.done.connect(self._on_comic_decoded)
            QThreadPool.globalInstance().start(task)
            self._comic_tasks.append(task)
        # 缩放才保留比例；切章由 _on_chapter_loaded 的 setValue(0) 归零
        if preserve_ratio and ratio > 0:
            QTimer.singleShot(0, lambda r=ratio: self._restore_comic_ratio(r))

    def _on_comic_decoded(self, gen: int, idx: int, img) -> None:
        """后台解码完成回填（主线程）：过期 gen 丢弃；占位高度让位给实际图。"""
        if gen != self._comic_gen or not (0 <= idx < len(self._comic_labels)):
            return  # 切章/缩放后的过期结果
        import shiboken6

        lbl = self._comic_labels[idx]
        if not shiboken6.isValid(lbl):
            return
        if img is None or img.isNull():
            lbl.setText("图片加载失败")
        else:
            lbl.setText("")
            lbl.setPixmap(QPixmap.fromImage(img))
        lbl.setMinimumHeight(0)
        lbl.updateGeometry()  # 通知布局：尺寸提示已变为实际图高
        self._gallery_widget.adjustSize()
        vbar = self.scroll.verticalScrollBar()
        vbar.setValue(min(vbar.value(), vbar.maximum()))

    def _restore_comic_ratio(self, ratio: float) -> None:
        """按阅读比例恢复滚动位置（缩放/重排后防跳回顶部）。"""
        if ratio <= 0:
            return
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() > 0:
            vbar.setValue(int(ratio * vbar.maximum()))

    def _on_zoom_timeout(self) -> None:
        """Ctrl+滚轮防抖到期 → 真正重排一次（滚轮连发只排一次，保留阅读比例）。"""
        if self._comic_imgs:
            self._render_comic_imgs(self._comic_imgs, preserve_ratio=True)

    @staticmethod
    def _clamp_font(size: int) -> int:
        return max(12, min(28, size))

    def set_font_scale(self, scale: float) -> None:
        """外部设置字体缩放（设置页实时生效）。保留用户 A+/A- 微调量。"""
        if self._is_comic:
            return
        self._base_font = self._clamp_font(round(17 * float(scale or 1.0)))
        self._apply_font()

    def _body_css(self) -> str:
        """正文 QLabel 样式（字号/行高/背景/前景统一生成，重建标签后仍一致）。"""
        size = self._clamp_font(self._base_font + self._font_delta)
        bg = self._reading_bg
        bg_css = f" background-color: {bg};" if bg else ""
        fg_css = f" color: {self._reading_fg};" if self._reading_fg else ""
        return f"font-size: {size}px; line-height: 1.8; padding: 8px 12px;{bg_css}{fg_css}"

    def _apply_font(self) -> None:
        if self._is_comic:
            return
        self._ensure_text_label()  # 被删则重建，避免操作失效对象
        self.text.setStyleSheet(self._body_css())

    def _adjust_font(self, delta: int) -> None:
        if self._is_comic:
            return
        self._font_delta += delta
        self._apply_font()

    def set_reading_style(self, bg: str = "", font_size: int = 0, fg: str = "") -> None:
        """设置阅读区独立背景色/字号/前景色（ui-reader #12）。

        bg：颜色字符串（#RRGGBB），空 = 透明跟随主题；
        font_size：>0 时覆盖全局字号，0 = 跟随全局 font_scale；
        fg：正文前景色（夜间黑等深色背景配浅字），空 = 跟随主题。
        """
        self._reading_bg = bg or ""
        self._reading_fg = fg or ""
        if font_size > 0:
            self._base_font = self._clamp_font(font_size)
        self._apply_font()
        self._apply_viewport_bg()

    def _apply_viewport_bg(self) -> None:
        """按 _reading_bg 设置滚动区背景（漫画图流模式也生效；空 = 重置跟随主题）。"""
        bg = self._reading_bg
        css = f"background-color: {bg};" if bg else ""
        if hasattr(self, "scroll"):
            self.scroll.viewport().setStyleSheet(css)
            import shiboken6

            gal = getattr(self, "_gallery_widget", None)
            if gal is not None and shiboken6.isValid(gal):
                gal.setStyleSheet(css)

    # ------------------------------------------------------------------ #
    def _is_descendant(self, obj) -> bool:
        """obj（或其父链）是否属于本视图 —— 应用级过滤器只处理本视图内事件。"""
        w = obj if isinstance(obj, QWidget) else None
        while w is not None:
            if w is self:
                return True
            w = w.parentWidget()
        return False

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """应用级事件过滤器：鼠标侧键翻章 + Ctrl+滚轮调字号（本视图内）。

        正文 QLabel 带 TextSelectableByMouse 会吞掉子控件级鼠标事件，故用
        应用级过滤器覆盖滚动区/正文/目录等所有子控件；非本视图事件直接放行。
        """
        # 视图不可见时（如切到其他页/弹窗创建控件）直接放行，
        # 避免对无关控件的每个事件做父链遍历（应用级过滤器全量触发）卡顿弹窗。
        if not self.isVisible() or not self._is_descendant(obj):
            return super().eventFilter(obj, event)
        if event.type() == event.Type.MouseButtonPress:
            btn = event.button()
            if btn == Qt.XButton2:
                self._jump_relative(1)  # 鼠标前侧键 → 下一章
                return True
            if btn == Qt.XButton1:
                self._jump_relative(-1)  # 鼠标后侧键 → 上一章
                return True
        elif event.type() == event.Type.Wheel and (event.modifiers() & Qt.ControlModifier):
            if self._is_comic and self._comic_imgs:
                # 漫画 epub：Ctrl+滚轮缩放图片（后台重解码，防抖合并滚轮连发）
                factor = 1.1 if event.angleDelta().y() > 0 else 0.9
                self._zoom = max(0.25, min(4.0, self._zoom * factor))
                self._zoom_timer.start()
            else:
                delta = 1 if event.angleDelta().y() > 0 else -1
                self._adjust_font(delta)
            event.accept()
            return True
        return super().eventFilter(obj, event)

    def refresh(self) -> None:
        pass
