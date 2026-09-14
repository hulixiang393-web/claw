"""阅读器主页面（ReaderPage）。

单 Tab 复用：打开一部作品，按 content_type 切换小说/漫画/视频视图。
对应 ui-reader.md。从发现/搜索详情抽屉「开始阅读」进入。

用法（App 层）：
    reader.open(source_id, book_url, content_type, start_chapter_url="")
"""

from __future__ import annotations

import logging

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

log = logging.getLogger(__name__)

# 阅读背景主题（bg, fg）：白/米黄/护眼绿/夜间黑。「背景」按钮按此循环，
# fg 为空 = 跟随主题；夜间黑配浅字保证可读。选择写回 ui.reading_bg 记忆。
READING_BG_THEMES = [
    ("", ""),                # 0 白 / 跟随主题
    ("#FBF3D2", ""),         # 1 米黄
    ("#C7EDCC", ""),         # 2 护眼绿
    ("#2B2B2B", "#E8E8E8"),  # 3 夜间黑
]


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
    # 「下载」→ App 层拉详情入下载队列（小说/漫画产 epub，视频产 mp4）
    download_requested = Signal(object)
    # 全屏态切换 → App 层隐藏/恢复 Tab 栏（沉浸阅读）
    fullscreen_changed = Signal(bool)
    # 「背景」循环切换 → App 层写回 ui.reading_bg（记忆护眼背景色选择）
    reading_bg_changed = Signal(str)

    def __init__(
        self,
        source_manager: SourceManager,
        content: Content,
        reading_progress=None,
        font_scale: float = 1.0,
        parent=None,
        search=None,
    ):
        super().__init__(parent)
        self._manager = source_manager
        self._content = content
        self._search = search  # 可选：视频阅读页相关推荐（同源搜索）
        self._reading_progress = reading_progress
        self._font_scale = float(font_scale or 1.0)
        self._current_source_id = None
        self._current_book_url = None
        self._current_start_url = ""
        self._current_content_type = ""  # 当前作品类型（收藏时记录）
        self._favorite_checker = None  # 可选回调: url -> bool（App 注入判断是否已收藏）
        self._pending_position = None  # 打开书续读位置（0~1 比例），_on_detail 传给视图
        self._pending_page = None  # 打开书续读翻页页索引（小说翻页模式）
        self._bg_idx = 0  # 当前护眼背景主题索引（READING_BG_THEMES）
        self._reading_font_size = 0  # 当前阅读字号（背景循环不改变字号）

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- 作品信息条（容器化：全屏阅读时整体隐藏，沉浸）----
        self.info_bar = QWidget()
        info = QHBoxLayout(self.info_bar)
        info.setContentsMargins(0, 0, 0, 0)
        self.title_label = QLabel("未打开作品")
        self.title_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        info.addWidget(self.title_label)
        self.source_label = QLabel("")
        self.source_label.setStyleSheet("color: palette(dark);")
        info.addWidget(self.source_label)
        info.addStretch(1)
        self.dl_btn = QPushButton("⬇ 下载")
        self.dl_btn.setFixedWidth(80)
        self.dl_btn.clicked.connect(self._on_download_clicked)
        info.addWidget(self.dl_btn)
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
        layout.addWidget(self.info_bar)

        # ---- 四视图切换 ----
        self.stack = QStackedWidget()
        self.novel_view = NovelView(content, font_scale=self._font_scale)
        self.comic_view = ComicView(content)
        self.video_view = VideoView(content, search=self._search)
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
            self.epub_view.position_changed.connect(self._on_epub_position)
            # 章内位置续读（滚动/翻页/播放进度，节流后落盘）
            self.novel_view.position_changed.connect(self._on_progress_signal)
            self.comic_view.position_changed.connect(self._on_progress_signal)
            self.video_view.position_changed.connect(self._on_progress_signal)

        # ---- 换源：VideoView 切源 → 重载分集 ----
        self.video_view.source_changed.connect(self._on_source_changed)
        # ---- 播放器内下载：转发 App 层下载链路（与顶部「下载」一致）----
        self.video_view.download_requested.connect(self.download_requested)
        # ---- 相关推荐：点推荐卡 → 打开该作品（复用 open 流程）----
        self.video_view.recommend_open_requested.connect(self._on_recommend_open)

        # ---- 全屏阅读：小说/漫画/epub 工具条 ⛶ → 切主窗全屏（视频自带全屏不重复）----
        self._fullscreen = False
        self._fs_was_max = False  # 进入全屏前窗口是否最大化（退出时还原）
        self.novel_view.fullscreen_requested.connect(self._toggle_fullscreen)
        self.comic_view.fullscreen_requested.connect(self._toggle_fullscreen)
        self.epub_view.fullscreen_requested.connect(self._toggle_fullscreen)
        # Esc 退出全屏（WindowShortcut：主窗激活即生效，不依赖子控件焦点）
        from PySide6.QtGui import QKeySequence, QShortcut

        sc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        sc.activated.connect(self._exit_fullscreen)
        # 「背景」按钮 → 循环切换护眼背景色（三视图共用一个循环态，切换一致）
        self.novel_view.background_cycle_requested.connect(self.cycle_reading_bg)
        self.comic_view.background_cycle_requested.connect(self.cycle_reading_bg)
        self.epub_view.background_cycle_requested.connect(self.cycle_reading_bg)

    def _toggle_fullscreen(self) -> None:
        """切换全屏阅读：主窗全屏 + 隐藏信息条/Tab 栏（沉浸），退出时还原。"""
        win = self.window()
        if not self._fullscreen:
            self._fs_was_max = bool(win.windowState() & Qt.WindowMaximized)
            win.showFullScreen()
        else:
            win.showNormal()
            if self._fs_was_max:
                win.showMaximized()
        self._fullscreen = not self._fullscreen
        self._apply_fullscreen_ui()

    def _exit_fullscreen(self) -> None:
        """退出全屏（幂等：非全屏态直接返回）。"""
        if not self._fullscreen:
            return
        self._toggle_fullscreen()

    def _apply_fullscreen_ui(self) -> None:
        """全屏态 UI 联动：信息条显隐 + 各视图按钮提示 + 通知 App 层。"""
        self.info_bar.setVisible(not self._fullscreen)
        tip = "退出全屏（Esc）" if self._fullscreen else "全屏阅读"
        for v in (self.novel_view, self.comic_view, self.epub_view):
            btn = getattr(v, "fullscreen_btn", None)
            if btn is not None:
                btn.setToolTip(tip)
        self.fullscreen_changed.emit(self._fullscreen)

    def hideEvent(self, event) -> None:  # noqa: N802
        """切走阅读器 Tab：若在全屏则自动退出（防主窗卡在全屏无法导航）。"""
        if self._fullscreen:
            self._exit_fullscreen()
        super().hideEvent(event)

    def _on_recommend_open(self, payload) -> None:
        """点视频页相关推荐卡 → 打开该作品（复用 open 流程，自动落盘当前进度）。"""
        if not isinstance(payload, (tuple, list)) or len(payload) < 3:
            return
        sid, url, ctype = payload[0], payload[1], payload[2]
        if sid and url:
            self.open(sid, url, ctype)

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
        # 进度记忆由 reload_detail → _load_episode(0) → episode_changed 正常落盘
        # （新源当前集 url）。这里不再补一条空 url 的进度——那会把已存的
        # chapter_url 覆盖成空，导致该书续读丢失。

    def _on_source_switch_failed(self, err: str) -> None:
        self.video_view.play_label.setText(f"换源失败：{err}")
        self.video_view.set_source_sid(self.video_view._current_sid)

    def _on_progress_signal(self, payload) -> None:
        """记录阅读进度（换章/换集/滚动/翻页/播放触发）。

        payload=(detail, title, url, position, page)：
        - 换章/换集：3 元组（无位置）；滚动/翻页/播放：5 元组。
        - epub 本地书：payload=(epub_path, chapter_title)，用文件路径作 key。
        """
        if self._reading_progress is None or not isinstance(payload, (tuple, list)):
            return
        detail, title = payload[0], payload[1]
        url = payload[2] if len(payload) > 2 else ""
        position = payload[3] if len(payload) > 3 else None
        page = payload[4] if len(payload) > 4 else None
        if detail is None:
            return
        try:
            # epub 本地文件：payload[0] 是路径字符串，用路径作 book_url
            if isinstance(detail, str):
                self._reading_progress.save(
                    "", detail, "epub", detail, title or "",
                    position=position,
                )
                return
            self._reading_progress.save(
                detail.source_id,
                detail.url,
                detail.content_type,
                url,
                title,
                position=position,
                page=page,
            )
        except Exception:
            pass  # 记忆失败不影响阅读

    def _on_epub_position(self, payload) -> None:
        """本地 epub 章内滚动位置记忆（保留当前章节标题）。"""
        if self._reading_progress is None or not isinstance(payload, (tuple, list)):
            return
        path, ratio = payload[0], payload[1]
        if not path:
            return
        try:
            rec = self._reading_progress.resume(path) or {}
            self._reading_progress.save(
                "", path, "epub", path,
                rec.get("chapter_title", "") or "",
                position=ratio,
            )
        except Exception:
            pass  # 记忆失败不影响阅读

    def _flush_current_progress(self) -> None:
        """切书/退出前落盘当前作品的章内位置（换书不丢最后几秒进度）。

        只冲刷当前显示视图（其他视图残留上一本书的 _detail，会存错书）。
        """
        if self._reading_progress is None or not self._current_book_url:
            return
        view = self.stack.currentWidget()
        if view in (self.novel_view, self.comic_view, self.video_view):
            ctx = view.current_context()
            if ctx is not None:
                pos, page = view.position_snapshot()
                self._on_progress_signal((*ctx, pos, page))

    # ------------------------------------------------------------------ #
    def open(self, source_id: str, book_url: str, content_type: str, start_chapter_url: str = "") -> None:
        """打开一部作品（续读：记忆的章节/位置优先于调用方传入）。"""
        # 切书前落盘当前作品的最新位置（防换书丢失最后几秒进度）
        self._flush_current_progress()
        try:
            source = self._manager.get(source_id)
        except Exception:
            self.title_label.setText(f"源不存在：{source_id}")
            return
        # 续读恢复：记忆里有这本书 → 用记忆的章覆盖 start_chapter_url，并取位置
        resume_pos, resume_page = None, None
        if self._reading_progress is not None and book_url:
            rec = self._reading_progress.resume(book_url)
            if rec and rec.get("chapter_url"):
                start_chapter_url = rec["chapter_url"]
                resume_pos = rec.get("position")
                resume_page = rec.get("page")
        self._pending_position = resume_pos
        self._pending_page = resume_page
        self._current_source_id = source_id
        self._current_source = source
        self._current_book_url = book_url
        self._current_start_url = start_chapter_url
        self._current_content_type = content_type
        self._current_detail = None  # 详情就绪后由 _on_detail 填充（收藏复用完整元数据）
        self.dl_btn.setEnabled(True)
        self.title_label.setText(f"加载中...")
        self.source_label.setText(source.source_name)
        self.refresh_favorite_state()  # 打开新作品即刷新收藏按钮

        # 后台拉详情（信号跨线程安全）
        from PySide6.QtCore import QThreadPool

        task = _LoadDetailTask(
            self._content, source, book_url, content_type, start_chapter_url,
            source_id,
        )
        task.signals.finished.connect(self._on_detail)
        self._detail_task = task  # 持有引用，防止被 GC
        QThreadPool.globalInstance().start(task)

    def _on_detail(self, detail, err, content_type: str, start_chapter_url: str, source_id: str) -> None:
        if err or detail is None:
            self.title_label.setText(f"加载失败：{err}")
            return
        # 请求代际校验：快速连开多部作品时旧详情任务后到（detail.url/source 已非
        # 当前目标），若照常应用会用新的 _current_source_id/_pending_position 渲染
        # 旧书 → 错源/错续读位置。不匹配直接丢弃。
        if source_id != self._current_source_id or detail.url != self._current_book_url:
            return
        # 切走视频视图前释放播放资源：换小说/漫画/另一部视频都不在后台
        # 继续播放、不堆积播放缓存（stop_playback 幂等，非视频时无副作用）
        self.video_view.stop_playback()
        self._current_detail = detail  # 收藏时复用完整详情（含封面/作者/标签）
        self.title_label.setText(detail.title or "无标题")
        self.refresh_favorite_state()  # 按当前书 URL 刷新收藏按钮
        # 按类型切视图（续读位置随 load 传入，首次显示后定位到页）
        pos, page = getattr(self, "_pending_position", None), getattr(self, "_pending_page", None)
        self._pending_position = None
        self._pending_page = None
        if content_type == "novel":
            self.stack.setCurrentWidget(self.novel_view)
            self.novel_view.load(
                self._manager.get(self._current_source_id), detail, start_chapter_url,
                restore_position=pos, restore_page=page,
            )
        elif content_type == "comic":
            self.stack.setCurrentWidget(self.comic_view)
            self.comic_view.load(
                self._manager.get(self._current_source_id), detail, start_chapter_url,
                restore_position=pos,
            )
        else:
            self.stack.setCurrentWidget(self.video_view)
            self.video_view.load(
                self._manager.get(self._current_source_id), detail, start_chapter_url,
                restore_position=pos,
            )
        # 后台预加载当前章+后 3 章正文到 Redis 缓存（小说/漫画，视频除外）。
        # 不阻塞渲染：ReaderPage 已离开_LoadDetailTask 后台线程，此处起独立 QRunnable。
        if content_type in ("novel", "comic"):
            self._start_precache(source_id, detail)

    def _start_precache(self, source_id: str, detail) -> None:
        """后台预加载当前章+后 3 章正文到缓存（小说/漫画共用）。

        需要 self._content.cache 已注入（Task 9 引导）。未注入则 no-op。
        """
        try:
            if getattr(self._content, "_cache", None) is None:
                return
            chapters = getattr(detail, "chapters", None)
            if not chapters:
                return
            source = self._manager.get(source_id)
            if source is None:
                return
            sidx = 0
            start_url = getattr(self, "_current_start_url", "")
            if start_url:
                for i, ch in enumerate(chapters):
                    if getattr(ch, "url", "") == start_url:
                        sidx = i
                        break
            QThreadPool.globalInstance().start(
                _PrecacheTask(self._content, source, chapters, sidx)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[cache] 预加载调度失败: %s", exc)

    # ------------------------------------------------------------------ #
    def set_favorite_checker(self, cb) -> None:
        """注入收藏判断回调：cb(url) -> bool。App 层接 LibraryStore.has。"""
        self._favorite_checker = cb
        self.refresh_favorite_state()

    def _current_chapter_index(self) -> int:
        """当前章/集 1 基序号（阅读器下载默认范围起点；无则 1）。"""
        view = self.stack.currentWidget()
        idx = getattr(view, "_current_idx", -1)
        if not isinstance(idx, int) or idx < 0:
            return 1
        return idx + 1

    def _on_download_clicked(self) -> None:
        """点「⬇ 下载」→ 转发 App 层：拉详情后弹章节范围（默认当前章→末章）。"""
        if not self._current_book_url:
            return
        self.download_requested.emit(
            (self._current_source_id or "", self._current_book_url,
             self._current_content_type or "", self._current_chapter_index())
        )

    def _on_favorite_clicked(self) -> None:
        """点收藏/取消收藏 → 转发给 App 层写收藏库（复用完整详情元数据）。"""
        if not self._current_book_url:
            return
        from framework.content import Detail

        cur = self._current_detail
        detail = Detail(
            source_id=self._current_source_id or "",
            content_type=self._current_content_type or "",
            url=self._current_book_url,
            title=(getattr(cur, "title", "") or "") 
            or (self.title_label.text() or self._current_book_url),
            cover=(getattr(cur, "cover", "") or "") or "",
            author=(getattr(cur, "author", "") or "") or "",
            tags=list(getattr(cur, "tags", None) or ()) if cur else [],
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
        """打开本地 epub（独立 epub 阅读器）。解析在后台线程，不卡 UI。"""
        self._current_source_id = ""
        self._current_book_url = ""
        self.title_label.setText("epub 阅读")
        self.source_label.setText(path)
        self.fav_btn.setEnabled(False)  # epub 本地书不收藏
        self.fav_btn.setText("☆ 收藏")
        self.dl_btn.setEnabled(False)  # epub 本地书已在本机，无需下载
        self.video_view.stop_playback()  # 切 epub 前也释放视频播放资源
        self.stack.setCurrentWidget(self.epub_view)

        # 先读续读记录再打开：_load_chapter(start) 会触发 chapter_changed 把进度
        # 刷成当前章，若延后到回调里再 resume 会拿到被覆盖的起点而丢失续读位。
        last_title, last_pos = "", 0.0
        if self._reading_progress is not None:
            rec = self._reading_progress.resume(path)
            if rec:
                last_title = rec.get("chapter_title", "") or ""
                last_pos = float(rec.get("position") or 0)

        def _on_loaded(chapters) -> None:
            # 续读：按上次章节标题定位，标题栏同步显示该章。
            # 章内滚动位置在定位到目标章时才设（起始章先加载，不能提前设否则被消耗）。
            show = chapters[0].title if chapters else "epub"
            if last_title:
                for i, ch in enumerate(chapters):
                    if ch.title == last_title:
                        self.epub_view._pending_pos = last_pos
                        self.epub_view._load_chapter(i)
                        show = ch.title
                        break
            self.title_label.setText(show)

        self.epub_view.open(path, start_idx=0, on_loaded=_on_loaded)

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
        """设置阅读区独立背景/字号/前景色（ui-reader #12）：转发给小说/漫画/epub。

        bg：颜色字符串（#RRGGBB），空 = 透明跟随主题；
        font_size：>0 时覆盖全局字号，0 = 跟随全局 font_scale；
        前景色按主题配套（夜间黑配浅字），浅色背景保持默认前景。
        """
        self._bg_idx = self._bg_index_for(bg)
        if font_size > 0:
            self._reading_font_size = font_size
        fg = self._fg_for(bg)
        for view in (self.novel_view, self.comic_view, self.epub_view):
            view.set_reading_style(bg, font_size, fg=fg)

    def cycle_reading_bg(self) -> None:
        """「背景」按钮：循环切换 白/米黄/护眼绿/夜间黑，应用三视图 + 通知 App 记忆。"""
        self._bg_idx = (self._bg_idx + 1) % len(READING_BG_THEMES)
        bg, _fg = READING_BG_THEMES[self._bg_idx]
        self.apply_reading_style(bg, self._reading_font_size)
        self.reading_bg_changed.emit(bg)

    @staticmethod
    def _bg_index_for(bg: str) -> int:
        """按背景色字符串匹配主题索引（未识别回 0 = 白/跟随主题）。"""
        key = (bg or "").strip().lower()
        for i, (b, _) in enumerate(READING_BG_THEMES):
            if b.lower() == key:
                return i
        return 0

    @staticmethod
    def _fg_for(bg: str) -> str:
        """按背景色取配套前景色（浅色背景配默认前景，夜间黑配浅字）。"""
        key = (bg or "").strip().lower()
        for b, f in READING_BG_THEMES:
            if b.lower() == key:
                return f
        return ""

    def shutdown_video(self) -> None:
        """App 退出释放 VLC 播放器（必须先于 shutdown_vlc 释放实例）。"""
        if hasattr(self, "video_view"):
            self.video_view.shutdown_video()


class _DetailSignals(QObject):
    """详情加载信号。"""
    finished = Signal(object, object, object, object, object)  # (detail, err, content_type, start_url, source_id)


class _LoadDetailTask(QRunnable):
    """后台加载详情（QRunnable）。"""

    def __init__(self, content, source, url, content_type, start_url, source_id):
        super().__init__()
        self.signals = _DetailSignals()
        self._content = content
        self._source = source
        self._url = url
        self._content_type = content_type
        self._start_url = start_url
        self._source_id = source_id

    def run(self) -> None:
        detail, err = None, None
        try:
            detail = self._content.fetch_detail(self._source, self._url)
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(
                detail, err, self._content_type, self._start_url, self._source_id
            )
        except RuntimeError:
            pass


class _PrecacheTask(QRunnable):
    """后台预加载当前章+后 3 章正文（QRunnable，异常静默）。"""

    def __init__(self, content, source, chapters, idx):
        super().__init__()
        self._content = content
        self._source = source
        self._chapters = chapters or []
        self._idx = idx

    def run(self) -> None:
        try:
            self._content.precache_chapters(
                self._source, self._chapters, self._idx
            )
        except Exception:  # noqa: BLE001
            pass
