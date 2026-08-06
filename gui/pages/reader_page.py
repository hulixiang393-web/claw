"""阅读器主页面（ReaderPage）。

单 Tab 复用：打开一部作品，按 content_type 切换小说/漫画/视频视图。
对应 ui-reader.md。从发现/搜索详情抽屉「开始阅读」进入。

用法（App 层）：
    reader.open(source_id, book_url, content_type, start_chapter_url="")
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThreadPool, QRunnable, QObject, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from framework.content import Content, Detail
from framework.source_manager import SourceManager

from .reader.novel_view import NovelView
from .reader.comic_view import ComicView
from .reader.video_view import VideoView
from .reader.epub_view import EpubView
from .base_page import BasePage


class _SwitchSourceSignals(QObject):
    """换源后台任务信号。"""
    done = Signal(object)  # new_detail
    error = Signal(str)


class _SwitchSourceTask(QRunnable):
    """后台切换播放源：重新抓详情（换 sid）。"""

    def __init__(self, content, source, detail_url, sid):
        super().__init__()
        self.signals = _SwitchSourceSignals()
        self._content = content
        self._source = source
        self._url = detail_url
        self._sid = sid

    def run(self) -> None:
        try:
            new_detail, chapters = self._content.switch_source(
                self._source, self._url, self._sid
            )
            try:
                self.signals.done.emit(new_detail)
            except RuntimeError:
                pass
        except Exception as exc:  # noqa: BLE001
            try:
                self.signals.error.emit(str(exc))
            except RuntimeError:
                pass


class ReaderPage(BasePage):
    # 收藏/取消收藏 → App 层写书架收藏库（ui-reader.md #2 通用外壳「收藏」）
    favorite_requested = Signal(object)

    def __init__(
        self,
        source_manager: SourceManager,
        content: Content,
        reading_progress=None,
        font_scale: float = 1.0,
        parent=None,
    ):
        super().__init__(parent)
        self._manager = source_manager
        self._content = content
        self._reading_progress = reading_progress
        self._font_scale = float(font_scale or 1.0)
        self._current_source_id = None
        self._current_book_url = None
        self._current_content_type = ""  # 当前作品类型（收藏时记录）
        self._favorite_checker = None  # 可选回调: url -> bool（App 注入判断是否已收藏）

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- 作品信息条 ----
        info = QHBoxLayout()
        self.title_label = QLabel("未打开作品")
        self.title_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        info.addWidget(self.title_label)
        self.source_label = QLabel("")
        self.source_label.setStyleSheet("color: palette(dark);")
        info.addWidget(self.source_label)
        info.addStretch(1)
        self.fav_btn = QPushButton("☆ 收藏")
        self.fav_btn.setCheckable(True)
        self.fav_btn.setFixedWidth(86)
        self.fav_btn.clicked.connect(self._on_favorite_clicked)
        info.addWidget(self.fav_btn)
        self.open_btn = QPushButton("打开源详情")
        self.open_btn.clicked.connect(self._open_source_page)
        info.addWidget(self.open_btn)
        self.epub_btn = QPushButton("打开本地 epub")
        self.epub_btn.clicked.connect(self._pick_epub)
        info.addWidget(self.epub_btn)
        layout.addLayout(info)

        # ---- 四视图切换 ----
        self.stack = QStackedWidget()
        self.novel_view = NovelView(content, font_scale=self._font_scale)
        self.comic_view = ComicView(content)
        self.video_view = VideoView(content)
        self.epub_view = EpubView(font_scale=self._font_scale)
        self.stack.addWidget(self.novel_view)
        self.stack.addWidget(self.comic_view)
        self.stack.addWidget(self.video_view)
        self.stack.addWidget(self.epub_view)
        layout.addWidget(self.stack, stretch=1)

        # ---- 阅读进度记忆：换章/换集时自动记录 ----
        if reading_progress is not None:
            self.novel_view.chapter_changed.connect(self._on_progress_signal)
            self.comic_view.chapter_changed.connect(self._on_progress_signal)
            self.video_view.episode_changed.connect(self._on_progress_signal)
            # epub 本地阅读：用文件路径作 key 续读
            self.epub_view.chapter_changed.connect(self._on_progress_signal)

        # ---- 换源：VideoView 切源 → 重载分集 ----
        self.video_view.source_changed.connect(self._on_source_changed)

    def _on_source_changed(self, payload) -> None:
        """换源：重新抓取该源详情 + 刷新 VideoView 分集。"""
        if self._content is None or not isinstance(payload, (tuple, list)):
            return
        detail, new_sid = payload[0], payload[1]
        if detail is None or not new_sid:
            return
        # 后台切源（网络请求，不阻塞 UI）
        task = _SwitchSourceTask(
            self._content, self._current_source, detail.url, new_sid
        )
        task.signals.done.connect(self._on_source_switched)
        task.signals.error.connect(self._on_source_switch_failed)
        self._switch_task = task  # 持有引用，防止被 GC
        QThreadPool.globalInstance().start(task)

    def _on_source_switched(self, new_detail) -> None:
        """换源成功：刷新 VideoView 分集列表。"""
        if new_detail is None:
            return
        self.video_view.reload_detail(new_detail)
        # 进度记忆用原 URL（含 sid 变化，保留同一部剧 key）
        self._on_progress_signal((new_detail, new_detail.title, ""))

    def _on_source_switch_failed(self, err: str) -> None:
        self.video_view.play_label.setText(f"换源失败：{err}")
        self.video_view.set_source_sid(self.video_view._current_sid)

    def _on_progress_signal(self, payload) -> None:
        """记录阅读进度（换章/换集触发）。payload=(detail, title, url)。

        epub 本地书：payload=(epub_path, chapter_title)，用文件路径作 key。
        """
        if self._reading_progress is None or not isinstance(payload, (tuple, list)):
            return
        detail, title = payload[0], payload[1]
        url = payload[2] if len(payload) > 2 else ""
        if detail is None:
            return
        try:
            # epub 本地文件：payload[0] 是路径字符串，用路径作 book_url
            if isinstance(detail, str):
                self._reading_progress.save(
                    "", detail, "epub", detail, title or ""
                )
                return
            self._reading_progress.save(
                detail.source_id,
                detail.url,
                detail.content_type,
                url,
                title,
            )
        except Exception:
            pass  # 记忆失败不影响阅读

    # ------------------------------------------------------------------ #
    def open(self, source_id: str, book_url: str, content_type: str, start_chapter_url: str = "") -> None:
        """打开一部作品。"""
        try:
            source = self._manager.get(source_id)
        except Exception:
            self.title_label.setText(f"源不存在：{source_id}")
            return
        self._current_source_id = source_id
        self._current_source = source
        self._current_book_url = book_url
        self._current_content_type = content_type
        self.title_label.setText(f"加载中...")
        self.source_label.setText(source.source_name)
        self.refresh_favorite_state()  # 打开新作品即刷新收藏按钮

        # 后台拉详情（信号跨线程安全）
        from PySide6.QtCore import QThreadPool

        task = _LoadDetailTask(
            self._content, source, book_url, content_type, start_chapter_url
        )
        task.signals.finished.connect(self._on_detail)
        self._detail_task = task  # 持有引用，防止被 GC
        QThreadPool.globalInstance().start(task)

    def _on_detail(self, detail, err, content_type: str, start_chapter_url: str) -> None:
        if err or detail is None:
            self.title_label.setText(f"加载失败：{err}")
            return
        self.title_label.setText(detail.title or "无标题")
        self.refresh_favorite_state()  # 按当前书 URL 刷新收藏按钮
        # 按类型切视图
        if content_type == "novel":
            self.stack.setCurrentWidget(self.novel_view)
            self.novel_view.load(self._manager.get(self._current_source_id), detail, start_chapter_url)
        elif content_type == "comic":
            self.stack.setCurrentWidget(self.comic_view)
            self.comic_view.load(self._manager.get(self._current_source_id), detail, start_chapter_url)
        else:
            self.stack.setCurrentWidget(self.video_view)
            self.video_view.load(self._manager.get(self._current_source_id), detail, start_chapter_url)

    # ------------------------------------------------------------------ #
    def set_favorite_checker(self, cb) -> None:
        """注入收藏判断回调：cb(url) -> bool。App 层接 LibraryStore.has。"""
        self._favorite_checker = cb
        self.refresh_favorite_state()

    def _on_favorite_clicked(self) -> None:
        """点收藏/取消收藏 → 转发给 App 层写收藏库。"""
        if self._current_book_url:
            # 构造最小 Detail（App 层 _on_favorite 只需 url/title 等）
            from framework.content import Detail

            detail = Detail(
                source_id=self._current_source_id or "",
                content_type=self._current_content_type or "",
                url=self._current_book_url,
                title=self.title_label.text() or self._current_book_url,
            )
            self.favorite_requested.emit(detail)

    def refresh_favorite_state(self) -> None:
        """按当前书 URL 刷新收藏按钮状态（☆收藏 / ★已收藏）。"""
        if not self._current_book_url:
            self.fav_btn.setChecked(False)
            self.fav_btn.setText("☆ 收藏")
            self.fav_btn.setEnabled(False)
            return
        self.fav_btn.setEnabled(True)
        is_fav = False
        if self._favorite_checker is not None:
            try:
                is_fav = bool(self._favorite_checker(self._current_book_url))
            except Exception:  # noqa: BLE001
                is_fav = False
        self.fav_btn.blockSignals(True)
        self.fav_btn.setChecked(is_fav)
        self.fav_btn.setText("★ 已收藏" if is_fav else "☆ 收藏")
        self.fav_btn.blockSignals(False)

    def _open_source_page(self) -> None:
        import webbrowser

        if self._current_book_url:
            webbrowser.open(self._current_book_url)

    def _pick_epub(self) -> None:
        """文件选择：打开本地 epub。"""
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "打开 epub 电子书", "", "EPUB 文件 (*.epub)"
        )
        if path:
            self.open_epub(path)

    def open_epub(self, path: str) -> None:
        """打开本地 epub（独立 epub 阅读器）。"""
        self._current_source_id = ""
        self._current_book_url = ""
        self.title_label.setText("epub 阅读")
        self.source_label.setText(path)
        self.fav_btn.setEnabled(False)  # epub 本地书不收藏
        self.fav_btn.setText("☆ 收藏")
        self.stack.setCurrentWidget(self.epub_view)
        if self.epub_view.open(path, start_idx=0):
            # 续读：按上次章节标题定位
            if self._reading_progress is not None:
                rec = self._reading_progress.resume(path)
                last_title = (rec or {}).get("chapter_title", "")
                if last_title:
                    for i, ch in enumerate(self.epub_view._chapters):
                        if ch.title == last_title:
                            self.epub_view._load_chapter(i)
                            break
            self.title_label.setText(self.epub_view._chapters[0].title if self.epub_view._chapters else "epub")
        else:
            self.title_label.setText("epub 打开失败")

    def refresh(self) -> None:
        pass

    def apply_font_scale(self, scale: float) -> None:
        """设置页字体缩放实时生效：转发给小说/epub 视图。"""
        self._font_scale = float(scale or 1.0)
        if hasattr(self, "novel_view"):
            self.novel_view.set_font_scale(self._font_scale)
        if hasattr(self, "epub_view"):
            self.epub_view.set_font_scale(self._font_scale)

    def apply_reading_style(self, bg: str = "", font_size: int = 0) -> None:
        """设置阅读区独立背景/字号（ui-reader #12）：转发给小说视图。"""
        if hasattr(self, "novel_view"):
            self.novel_view.set_reading_style(bg, font_size)

    def shutdown_video(self) -> None:
        """App 退出释放 VLC 播放器（必须先于 shutdown_vlc 释放实例）。"""
        if hasattr(self, "video_view"):
            self.video_view.shutdown_video()


class _DetailSignals(QObject):
    """详情加载信号。"""
    finished = Signal(object, object, object, object)  # (detail, err, content_type, start_url)


class _LoadDetailTask(QRunnable):
    """后台加载详情（QRunnable）。"""

    def __init__(self, content, source, url, content_type, start_url):
        super().__init__()
        self.signals = _DetailSignals()
        self._content = content
        self._source = source
        self._url = url
        self._content_type = content_type
        self._start_url = start_url

    def run(self) -> None:
        detail, err = None, None
        try:
            detail = self._content.fetch_detail(self._source, self._url)
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(
                detail, err, self._content_type, self._start_url
            )
        except RuntimeError:
            pass
