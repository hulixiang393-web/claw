"""漫画阅读视图（ComicView）。

对应 ui-reader.md 漫画功能点：
- 图片逐页加载 + 预加载
- 滚动画廊（默认）/ 横向翻页可切换
- 图片缩放（点击放大/双击还原）
- 目录侧栏（可折叠，点击跳话）
- 预渲染后续话（读到 70% 预渲染下一话、向前缓存 3 话，切话秒开）
- 续读信号（chapter_changed 发 detail+话名+话URL）
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import Qt, QTimer, Signal, QThreadPool, QRunnable, QObject
from PySide6.QtGui import QImage, QPixmap
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

# 预加载后续话数：只预加载下一话（读到 70% 才触发，不加载过多）
PREFETCH_COUNT = 2  # 预渲染后续话数：连看时下一话已就绪、再下一话开始预渲染，切话更顺
PREFETCH_BACK = 3  # 向前缓存话数：向上翻话命中缓存秒开（以当前话为基点前 3 话）
# 懒加载：首屏渲染页数 / 滚动增量渲染每批页数
INITIAL_RENDER_COUNT = 10
LAZY_BATCH = 12


class ComicView(QWidget):
    """漫画翻页阅读视图。"""

    chapter_changed = Signal(object)  # 发 (detail, chapter_title, chapter_url)
    position_changed = Signal(object)  # (detail, title, url, position, None) 章内位置续读
    fullscreen_requested = Signal()  # 工具条 ⛶ → ReaderPage 切主窗全屏
    background_cycle_requested = Signal()  # 「背景」按钮 → ReaderPage 循环切换护眼背景色

    def __init__(self, content: Content, parent=None):
        super().__init__(parent)
        self._content = content
        self._source = None
        self._detail: Detail | None = None
        self._chapters = []
        self._current_idx = -1
        self._gen = 0  # 加载代际：换书自增，旧书异步回调（取流/预取）因代际过期被丢弃
        self._cancel_evt = None  # 换书取消令牌：置位后旧书后台取流/预取任务尽早退出
        self._images = []
        self._mode = "gallery"  # gallery / flip
        self._zoom = 1.0
        self._prefetched = {}  # {url: {"images":[...], "count":N}} 预渲染的后续话
        self._prefetch_queue = []  # 串行预渲染队列（同一时间只渲染 1 话）
        self._prefetch_busy = False  # 是否正在预渲染
        self._rendered_count = 0  # 已渲染图片数（边抓边显示增量用）
        self._rendered_header = False  # 话头 QLabel 是否已创建
        self._pending_swap = False  # 换话保留旧画面：新话首批图就绪后再清空替换
        self._last_pos_save_ts = 0.0  # 上次章内位置存盘时间戳（节流 1.5s 存一次）
        self._pending_position = None  # 打开书续读位置（0~1 滚动比例），话加载后定位
        self._reading_bg = ""  # 阅读区独立背景色（空=透明跟随主题）
        self._reading_fg = ""  # 夜间黑等深色背景下的前景色（漫画以图为主，预留）

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

        self.bg_btn = QPushButton("背景")
        self.bg_btn.setFixedWidth(50)
        self.bg_btn.setToolTip("切换阅读背景色（白/米黄/护眼绿/夜间黑）")
        self.bg_btn.clicked.connect(self.background_cycle_requested.emit)
        toolbar.addWidget(self.bg_btn)

        self.prev_btn = QPushButton("上一话")
        self.next_btn = QPushButton("下一话")
        self.prev_btn.clicked.connect(lambda: self._jump_relative(-1))
        self.next_btn.clicked.connect(lambda: self._jump_relative(1))
        toolbar.addWidget(self.prev_btn)
        toolbar.addWidget(self.next_btn)

        self.fullscreen_btn = QPushButton("⛶")
        self.fullscreen_btn.setFixedWidth(40)
        self.fullscreen_btn.setToolTip("全屏阅读")
        self.fullscreen_btn.clicked.connect(self.fullscreen_requested.emit)
        toolbar.addWidget(self.fullscreen_btn)

        toolbar.addStretch(1)
        self.progress_label = QLabel("")
        toolbar.addWidget(self.progress_label)
        # 缩放指示器（跟随 Ctrl+滚轮实时更新）
        from PySide6.QtWidgets import QSpinBox

        self.zoom_label = QLabel("100%")
        self.zoom_label.setStyleSheet("color: palette(mid); font-size: 11px;")
        toolbar.addWidget(self.zoom_label)
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
        self.scroll.setWidgetResizable(False)  # False：让 gallery.setFixedWidth(缩放) 真正生效
        self.scroll.setAlignment(Qt.AlignHCenter)  # gallery 比视口窄时水平居中（默认是左上角）
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.gallery = QWidget()
        self.gallery_layout = QVBoxLayout(self.gallery)
        self.gallery_layout.setContentsMargins(0, 0, 0, 0)
        self.gallery_layout.setSpacing(4)
        # 注意：不设 AlignHCenter——设了图片 label 只按 sizeHint 排布不拉伸，
        # Ctrl+滚轮缩放只改 gallery 定宽、图片不随宽度变化（缩放看起来没反应）。
        # gallery 窄于视口时的水平居中由 scroll.setAlignment(AlignHCenter) 负责。
        self.scroll.setWidget(self.gallery)
        body.addWidget(self.scroll, stretch=1)
        layout.addLayout(body, stretch=1)

        self._apply_mode()
        # 读到 70% → 预渲染下一话（不自动翻话）
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll_prefetch)
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll_lazy)
        # 章内位置记忆：纵向/横向滚动节流存盘（精准到页）
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll_position)
        self.scroll.horizontalScrollBar().valueChanged.connect(self._on_scroll_position)
        # Ctrl+滚轮缩放：用事件过滤器抢在 scroll area / 图片子控件之前捕获
        self.scroll.viewport().installEventFilter(self)
        self.scroll.installEventFilter(self)
        # 鼠标侧键翻话（+ Ctrl+滚轮缩放）：应用级过滤器覆盖全部子控件事件
        # （图片 QLabel 会吞掉子控件级鼠标事件，装到视图自身覆盖不到图区）
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        # 键盘焦点（支持←→↑↓翻话/翻图）
        self.setFocusPolicy(Qt.StrongFocus)

    # ------------------------------------------------------------------ #
    def load(
        self,
        source,
        detail: Detail,
        start_chapter_url: str = "",
        restore_position: float | None = None,
    ) -> None:
        # 换书：代际自增 + 清空旧书状态。旧书的后台取流/预取任务仍可能后到，
        # 但代际过期会被回调丢弃——避免旧书结果覆盖新书（URL 相同的章节
        # 或旧预取污染新书缓存）。旧书图片/缓存/预取队列一并清掉，新书从
        # 干净状态加载（不被旧书拖慢）。
        self._gen += 1
        # 取消旧书仍在后台跑的取流/预取任务：代际校验只丢弃回调，任务本身
        # 还会白跑完一整话（dm5 一话几十页 ≈74s）占资源拖慢新书。置位旧
        # 取消令牌 → 旧任务的分页循环检查后立即退出；新书用新令牌。
        if self._cancel_evt is not None:
            self._cancel_evt.set()
        self._cancel_evt = threading.Event()
        self._prefetch_tasks = []  # 旧预取任务引用一并清空（任务经令牌自行退出）
        self._images = []
        self._prefetched = {}
        self._prefetch_queue = []
        self._prefetch_busy = False
        self._pending_swap = False
        # 换书直接关闭旧漫画画面：不再等新书首批图（打开新书时旧书画面立即消失，
        # 内存同步释放；同书内换话仍走 _pending_swap 保留旧画面避免闪屏）
        self._clear_images()
        self._source = source
        self._detail = detail
        self._chapters = detail.chapters
        self._populate_toc()
        if restore_position is not None:
            self._pending_position = restore_position
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
        # 切换话：重置增量渲染计数（gallery 将清空重建，防旧计数错乱）
        self._rendered_count = 0
        self._rendered_header = False
        # 换话清理已消费/过期的预取缓存：当前话之前的话不会再被预取命中，
        # 留着只占内存（长漫画每话几十张图 URL 列表持续累积）。保留当前话
        # 与向后 PREFETCH_COUNT 话（预取仍会用到），其余丢弃。
        keep = {self._chapters[idx].url}
        for j in range(idx + 1, min(idx + 1 + PREFETCH_COUNT, len(self._chapters))):
            keep.add(self._chapters[j].url)
        for j in range(max(idx - PREFETCH_BACK, 0), idx):  # 保留前缓存窗口（向前预取会再命中）
            keep.add(self._chapters[j].url)
        self._prefetched = {k: v for k, v in self._prefetched.items() if k in keep}
        ch = self._chapters[idx]
        self.toc_list.setCurrentRow(idx)  # 目录高亮当前话

        # 【速度优化】已缓存本话图片 → 直接显示，秒开不重爬
        if hasattr(ch, "_cached_images") and ch._cached_images:
            self._images = ch._cached_images
            self.progress_label.setText(f"第{idx+1}/{len(self._chapters)}话 · {len(self._images)}张")
            self._render_images()
            self._finish_episode_load(ch)
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
            self._finish_episode_load(ch)
            return

        if prefetch := prefetched:
            self.progress_label.setText(
                f"第{idx+1}/{len(self._chapters)}话 · 约{prefetch['count']}张 加载中"
            )
        else:
            self.progress_label.setText(f"第{idx+1}/{len(self._chapters)}话 · 加载中")
        # 换话保留旧画面：不立即清空（慢源换话 15-19s 空白+闪屏），
        # 等新话首批图就绪（_prepare_new_episode_render）再清空替换。
        self._pending_swap = True
        # 后台加载图片 URL
        from PySide6.QtCore import QThreadPool

        task = _LoadComicTask(
            self._content, self._source, ch, gen=self._gen,
            cancel_evt=self._cancel_evt,
        )
        task.signals.finished.connect(self._on_images_loaded)
        task.signals.partial.connect(self._on_images_partial)  # 边抓边显示
        self._comic_task = task  # 持有引用，防止被 GC
        QThreadPool.globalInstance().start(task)
        # 注：chapter_changed 由 _finish_episode_load 统一发出（缓存/预渲染命中与
        # 本次加载完成后都只发一次，未缓存路径这里不再提前发 → 避免换话双写进度）。
        # 预加载不在此发起——等当前话加载完成（_on_images_loaded）再预渲染后续话，
        # 避免预渲染抢资源拖慢当前话首屏。

    def _finish_episode_load(self, ch) -> None:
        """加载完成后统一收尾：定位顶部 + 续读信号。"""
        self.chapter_changed.emit((self._detail, ch.title, ch.url))
        vbar = self.scroll.verticalScrollBar()
        vbar.blockSignals(True)
        vbar.setValue(0)
        vbar.blockSignals(False)
        # 续读定位：打开书恢复到上次滚动位置（重试链随懒加载高度增长逐步到位）
        if self._pending_position is not None:
            pos = self._pending_position
            self._pending_position = None
            if pos > 0:
                self._restore_position_with_retry(pos)
        # 预渲染下一话不在加载后立即发起：等读到当前话 70%（_on_scroll_prefetch）
        # 再预渲染，避免提前占用 Playwright 资源拖慢当前话。
        # 向前缓存例外：本话已渲染完成，后台预渲染前 PREFETCH_BACK 话，
        # 向上翻话命中缓存秒开（与后续话共用串行队列，排在最后不抢资源）。
        self._prefetch_prev(self._current_idx)

    def _on_images_loaded(self, gen, ch, images, err) -> None:
        # 代际过期：换书后旧书取流任务后到 → 整单丢弃，避免旧书结果覆盖新书
        # （URL 相同的章节会误判当前，渲染旧书画面）。
        if gen != self._gen:
            return
        if err:
            self.progress_label.setText(f"加载失败：{err}")
            return
        if self._current_idx < 0 or ch.url != self._chapters[self._current_idx].url:
            ch._cached_images = images  # 过期回调：仅写缓存，不渲染当前画面
            return
        self._images = images
        ch._cached_images = images  # 缓存本话，避免重复爬
        self.progress_label.setText(f"第{self._current_idx+1}/{len(self._chapters)}话 · {len(images)}张")
        # 新话首批图就绪 → 清空旧画面并渲染新话（换话保留旧画面到此刻）
        self._prepare_new_episode_render()
        # 增量补全（partial 已渲染部分；无 on_page 的源这里渲染首屏批）。
        # 横向翻页模式/定位话尾需完整高度 → force_full 一次渲染全部
        self._render_incremental(force_full=self._should_render_full())
        # 当前话加载完成后再串行预渲染后续话（切话秒开，且不抢当前话资源）
        self._finish_episode_load(ch)

    def _on_images_partial(self, gen, ch, images, err) -> None:
        """fetch_comic_pages 分批回调：已就绪前缀 → 增量渲染（边抓边显示）。"""
        if gen != self._gen:
            return  # 换书后旧书取流分批回调 → 丢弃
        if self._current_idx < 0 or ch.url != self._chapters[self._current_idx].url:
            return  # 已切话/换章，丢弃旧批次
        if not images:
            return
        self._images = images
        self.progress_label.setText(
            f"第{self._current_idx+1}/{len(self._chapters)}话 · 已加载 {len(images)} 张"
        )
        # 换话等待中：旧画面还在时攒够一屏（INITIAL_RENDER_COUNT）再替换，避免
        # 首批只有 1-2 张就清空旧画面导致大半空白；partial 继续攒，finished 兜底。
        # 首次加载（gallery 空）无需攒屏，直接按批次渲染。
        if (
            self._pending_swap
            and self.gallery_layout.count() > 0
            and len(images) < INITIAL_RENDER_COUNT
        ):
            return
        # 新话首批图就绪 → 清空旧画面并渲染新话（换话保留旧画面到此刻）
        self._prepare_new_episode_render()
        self._render_incremental(force_full=self._should_render_full())

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
        task = _PrefetchRenderTask(
            self._content, self._source, ch, gen=self._gen,
            cancel_evt=self._cancel_evt,
        )
        task.signals.finished.connect(self._on_prefetch_done)
        self._prefetch_tasks = getattr(self, "_prefetch_tasks", [])
        self._prefetch_tasks.append(task)
        QThreadPool.globalInstance().start(task)

    def _on_prefetch_done(self, gen, chapter_url, images, err) -> None:
        """预渲染完成，记录该话图片列表，并继续队列下一个。

        代际过期（换书后旧书任务后到）→ 丢弃结果，不污染新书预取缓存。
        """
        if gen != self._gen:
            return
        self._prefetched[chapter_url] = {
            "images": images or [], "count": len(images or [])
        }
        # 串行：完成一个接着预渲染下一个
        self._start_next_prefetch()

    def _render_images(self) -> None:
        self._clear_images()
        self._pending_swap = False  # 命中缓存直接渲染，无换话等待
        self._rendered_count = 0
        self._rendered_header = False
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
            self._rendered_header = True
        # 首屏只渲染前 INITIAL_RENDER_COUNT 张，其余交给滚动懒加载分批补全；
        # 横向翻页模式无纵向滚动懒加载 → 一次全量渲染
        # 正文图 Referer 由 CoverLoader 按图床域名规则推导（_REFERER_RULES）：
        # 不再传章节 URL——dm5 图床（cdndm5.com）拒绝章节页 Referer（404 假图），
        # manben 无 Referer 也可访问；统一走域名规则最安全。
        images = self._images or []
        limit = len(images) if self._mode == "flip" else min(INITIAL_RENDER_COUNT, len(images))
        for url in images[:limit]:
            lbl = _ComicImageLabel(url)
            lbl.loaded.connect(self._relayout_gallery_queued)
            lbl.load()
            self.gallery_layout.addWidget(lbl)
        self._rendered_count = limit
        # 刷新 gallery 尺寸（widgetResizable=False 需手动定宽+按内容定高）
        self._apply_zoom()
        self._relayout_gallery()

    def _render_incremental(self, force_full: bool = False) -> None:
        """边抓边显示 / 滚动懒加载：渲染 self._images 中尚未渲染的连续前缀。

        每次调用最多渲染 LAZY_BATCH 张（避免一次性创建全部 label 导致卡顿/闪屏），
        其余由滚动懒加载（_on_scroll_lazy）逐批补全；force_full=True 时一次渲染
        全部（横向翻页模式/定位话尾需要完整高度）。首次创建话头。
        供 fetch_comic_pages 的 on_page 分批回调（解密型源先显示已就绪的）。
        """
        images = self._images or []
        if not self._rendered_header and 0 <= self._current_idx < len(self._chapters):
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
            self._rendered_header = True
        target = len(images) if force_full else min(self._rendered_count + LAZY_BATCH, len(images))
        while self._rendered_count < target:
            url = images[self._rendered_count]
            # 正文图 Referer 由 CoverLoader 域名规则推导（同 _render_images，
            # 不传章节 URL——dm5 图床拒绝章节 Referer 返回 404 假图）
            lbl = _ComicImageLabel(url)
            lbl.loaded.connect(self._relayout_gallery_queued)
            lbl.load()
            self.gallery_layout.addWidget(lbl)
            self._rendered_count += 1
        self._apply_zoom()
        self._relayout_gallery()

    def _should_render_full(self) -> bool:
        """是否需要一次性渲染全部图片（而非懒加载分批）。

        横向翻页模式无纵向滚动事件、或定位话尾需要完整高度时，懒加载会缺图，
        须全量渲染。
        """
        return self._mode == "flip"

    def _relayout_gallery_queued(self) -> None:
        """图片异步加载完/重绘后，排队重算 gallery 高度。

        单批多图同时加载完成会并发多次触发：用 _relayout_pending 标记合并，
        保证每个事件循环轮次最多重排一次，避免高度反复跳动拉跳阅读位置。
        """
        if getattr(self, "_relayout_pending", False):
            return
        self._relayout_pending = True
        QTimer.singleShot(0, self._relayout_gallery_pending)

    def _relayout_gallery_pending(self) -> None:
        self._relayout_pending = False
        self._relayout_gallery()

    def _relayout_gallery(self) -> None:
        """按内容重算 gallery 高度（widgetResizable=False 不会自动跟随）。"""
        if self.gallery.layout() is not None:
            self.gallery.adjustSize()
        # 刷新滚动范围（高度变化后滚动条最大值跟着更新）
        vbar = self.scroll.verticalScrollBar()
        vbar.setValue(min(vbar.value(), vbar.maximum()))
        # 懒加载安全网：内容不足一屏（maximum==0）时没有滚动事件可触发，
        # 主动补一批直至可滚动，避免短页/小图章节读到后面缺图。
        if (
            not self._pending_swap
            and self._mode == "gallery"
            and vbar.maximum() == 0
            and self._rendered_count < len(self._images or [])
        ):
            QTimer.singleShot(0, self._render_incremental)

    def _clear_images(self) -> None:
        while self.gallery_layout.count():
            child = self.gallery_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def _prepare_new_episode_render(self) -> None:
        """新话首批图就绪 → 清空旧画面并重建（换话保留旧画面到此刻）。

        _load_episode 未命中缓存时不立即 _clear_images，而是等 _pending_swap
        后首批新图到达再替换，避免慢源（manben 等）换话 15-19s 空白+闪屏。
        """
        if not self._pending_swap:
            return
        self._pending_swap = False
        self._clear_images()
        self._rendered_header = False  # 重建新话话头
        self._rendered_count = 0

    # ------------------------------------------------------------------ #
    def _toggle_mode(self) -> None:
        self._mode = "flip" if self._mode == "gallery" else "gallery"
        self.mode_btn.setText("切换横向模式" if self._mode == "gallery" else "切换画廊模式")
        self._apply_mode()
        if self._mode == "flip" and not self._pending_swap:
            # 横向翻页模式无纵向滚动懒加载 → 补齐剩余图片
            # （换话等待中 _images 仍是旧话，等新话就绪后按需全量渲染）
            self._render_incremental(force_full=True)

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

    def set_reading_style(self, bg: str = "", font_size: int = 0, fg: str = "") -> None:
        """设置阅读区独立背景色（ui-reader #12）：作用于滚动区（图间空隙）。

        font_size/fg：漫画以图为主，预留参数不生效（与 novel/epub 签名一致）。
        """
        self._reading_bg = bg or ""
        self._reading_fg = fg or ""
        self._apply_bg()

    def _apply_bg(self) -> None:
        """按 _reading_bg 设置滚动区/画廊背景（空 = 重置回跟随主题）。"""
        bg = self._reading_bg
        css = f"background-color: {bg};" if bg else ""
        if hasattr(self, "scroll"):
            self.scroll.viewport().setStyleSheet(css)
            self.gallery.setStyleSheet(css)

    def _on_scroll_prefetch(self, value: int) -> None:
        """读到当前话 70% → 预渲染下一话（不自动翻话）。

        预渲染走 _prefetch_future 的串行队列锁（同一时间只渲染 1 话），
        切话时 _load_episode 命中 _prefetched → 秒开；70% 以下不触发。
        """
        if self._current_idx < 0:
            return
        if self._pending_swap:
            return  # 换话加载中：_images 仍是旧话，勿用旧数据触发预渲染
        if self._mode != "gallery":
            return  # 横向翻页模式无纵向滚动（翻页由按钮/方向键驱动）
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() == 0:
            return
        if value >= vbar.maximum() * 0.7:
            self._prefetch_future(self._current_idx, PREFETCH_COUNT)

    def _prefetch_prev(self, idx: int, n: int = PREFETCH_BACK) -> None:
        """预加载前面 n 话（向前缓存）：向上翻话命中缓存秒开。

        与后续话共用同一串行队列（_prefetch_queue + _prefetch_busy），最多
        同时预渲染 1 话；在当前话渲染完成（_finish_episode_load）后入队执行。
        """
        if self._source is None or not self._chapters:
            return
        for k in range(idx - 1, max(idx - 1 - n, -1), -1):
            if not (0 <= k < len(self._chapters)):
                continue
            ch = self._chapters[k]
            if ch.url in self._prefetched:
                continue  # 已预渲染过
            if hasattr(ch, "_cached_images") and ch._cached_images:
                self._prefetched[ch.url] = {
                    "images": ch._cached_images, "count": len(ch._cached_images)
                }
                continue
            if ch.url in self._prefetch_queue:
                continue  # 已在队列
            self._prefetch_queue.append(ch.url)
        # 若空闲则启动第一个（正在渲染后续话时不打断，串行排到后面）
        if not self._prefetch_busy and self._prefetch_queue:
            self._start_next_prefetch()

    def _on_scroll_lazy(self, value: int) -> None:
        """滚动接近已渲染末端 → 增量渲染下一批图片（懒加载）。

        以「滚动进度越过已渲染区域 85%」为界（已渲染最后一张距视口底部约
        0.85 屏时触发），每次补 LAZY_BATCH 张，边滚边渲染不一次性全量。
        """
        if self._mode != "gallery":
            return
        if self._pending_swap:
            return  # 换话加载中：_images 仍是旧话，勿用旧数据增量渲染
        images = self._images or []
        total = len(images)
        if self._rendered_count >= total:
            return
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() <= 0:
            return  # 内容不足一屏由 _relayout_gallery 安全网补全
        # 滚动值 ≥ 已渲染区域 85% 高度 → 补下一批
        threshold = int(vbar.maximum() * (self._rendered_count / total) * 0.85)
        if value >= threshold:
            self._render_incremental()

    # ------------------------------------------------------------------ #
    def _on_scroll_position(self, value: int) -> None:
        """滚动节流：距上次存盘 ≥1.5s 才落盘章内位置（避免高频写盘）。"""
        if self._detail is None or self._current_idx < 0:
            return
        now = time.monotonic()
        if now - self._last_pos_save_ts < 1.5:
            return
        self._last_pos_save_ts = now
        self._emit_position()

    def position_snapshot(self):
        """当前章内位置：(0~1 滚动比例, None)。画廊用纵向、横向翻页用横向。"""
        if self._mode == "flip":
            hbar = self.scroll.horizontalScrollBar()
            return (hbar.value() / hbar.maximum() if hbar.maximum() > 0 else 0.0), None
        vbar = self.scroll.verticalScrollBar()
        return (vbar.value() / vbar.maximum() if vbar.maximum() > 0 else 0.0), None

    def current_context(self):
        """当前阅读上下文 (detail, 话标题, 话URL)；未打开/未加载则 None。"""
        if self._detail is None or not (0 <= self._current_idx < len(self._chapters)):
            return None
        ch = self._chapters[self._current_idx]
        return (self._detail, ch.title, ch.url)

    def _emit_position(self) -> None:
        if self._detail is None or not (0 <= self._current_idx < len(self._chapters)):
            return
        ch = self._chapters[self._current_idx]
        pos, _ = self.position_snapshot()
        try:
            self.position_changed.emit((self._detail, ch.title, ch.url, pos, None))
        except RuntimeError:
            pass

    def _restore_position_with_retry(self, pos: float, tries: int = 8) -> None:
        """按 0~1 比例恢复滚动位置，重试链随懒加载高度增长逐步到位。

        漫画懒加载：目标位置下方图片未渲染前 maximum 偏小，直接滚会停在
        中间。每隔 500ms 重设一次（setValue 触发懒加载渲染补全），最多 8 次
        （约 4s），图片通常已就绪，最终精准落在上次阅读的图。
        """
        vbar = self.scroll.verticalScrollBar()
        hbar = self.scroll.horizontalScrollBar()
        if self._mode == "flip":
            if hbar.maximum() > 0:
                hbar.setValue(int(pos * hbar.maximum()))
        elif vbar.maximum() > 0:
            vbar.setValue(int(pos * vbar.maximum()))
        if tries > 1:
            QTimer.singleShot(500, lambda: self._restore_position_with_retry(pos, tries - 1))

    # ------------------------------------------------------------------ #
    # Ctrl+滚轮缩放（事件过滤器，抢在子控件 wheelEvent 之前）
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
        # 应用级过滤器：先判定事件是否属于本视图（防误吞其他页面的 Ctrl+滚轮/侧键）
        if not self._is_descendant(obj):
            return super().eventFilter(obj, event)
        if event.type() == event.Type.Wheel:
            if event.modifiers() & Qt.ControlModifier:
                delta = 1.1 if event.angleDelta().y() > 0 else 0.9
                self._zoom *= delta
                self._zoom = max(0.25, min(4.0, self._zoom))
                self._apply_zoom()
                event.accept()
                return True
            return False  # 普通滚动交给滚动条
        if event.type() == event.Type.MouseButtonPress:
            btn = event.button()
            if btn == Qt.XButton2:
                self._jump_relative(1)  # 鼠标前侧键 → 下一话
                return True
            if btn == Qt.XButton1:
                self._jump_relative(-1)  # 鼠标后侧键 → 上一话
                return True
        return super().eventFilter(obj, event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        # Ctrl+滚轮缩放（焦点在 ComicView 本身时走这里）
        if event.modifiers() & Qt.ControlModifier:
            delta = 1.1 if event.angleDelta().y() > 0 else 0.9
            self._zoom *= delta
            self._zoom = max(0.25, min(4.0, self._zoom))
            self._apply_zoom()
            event.accept()
        else:
            super().wheelEvent(event)

    def _apply_zoom(self) -> None:
        """按 _zoom 调整 gallery 宽度，图片宽度随动。

        调整前记录当前阅读比例（滚动值/最大值），等图片重排稳定后按比例
        恢复——否则懒加载下内容高度短暂小于视口时 scrollbar 最大值归零、
        滚动值被强制清零，窗口 resize / Ctrl+滚轮缩放后跳回第一页。
        """
        ratio = self._scroll_ratio()
        base = self._current_base_width()
        target = max(200, int(base * self._zoom))
        self.gallery.setFixedWidth(target)
        # 通知每张图片按新宽度重新缩放
        self._update_zoom_indicator()
        QTimer.singleShot(0, self._relayout_gallery)
        # 图片重排（已加载图按新宽度 _fit）约一个事件循环内完成，160ms 后恢复
        QTimer.singleShot(160, lambda: self._restore_scroll_ratio(ratio))

    def _scroll_ratio(self) -> float:
        """当前阅读比例：滚动值 / 最大值（内容不足一屏返回 0）。"""
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() <= 0:
            return 0.0
        return vbar.value() / vbar.maximum()

    def _restore_scroll_ratio(self, ratio: float) -> None:
        """按阅读比例恢复滚动位置（resize/缩放后防跳回第一页）。"""
        if ratio <= 0:
            return
        vbar = self.scroll.verticalScrollBar()
        if vbar.maximum() > 0:
            vbar.setValue(int(ratio * vbar.maximum()))

    def _current_base_width(self) -> int:
        return max(300, self.scroll.viewport().width())

    def _update_zoom_indicator(self) -> None:
        """工具栏缩放百分比提示。"""
        if hasattr(self, "zoom_label"):
            self.zoom_label.setText(f"{int(self._zoom * 100)}%")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_zoom()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """键盘导航：上下键滚动，左右键切话（画廊模式）。"""
        key = event.key()
        if self._mode == "gallery":
            vbar = self.scroll.verticalScrollBar()
            if key in (Qt.Key_Down, Qt.Key_PageDown):
                vbar.setValue(vbar.value() + self.scroll.height() * 2 // 3)
            elif key in (Qt.Key_Up, Qt.Key_PageUp):
                vbar.setValue(vbar.value() - self.scroll.height() * 2 // 3)
            elif key == Qt.Key_Right:
                self._jump_relative(1)
            elif key == Qt.Key_Left:
                self._jump_relative(-1)
        else:
            # flip模式：左右键翻图/翻话
            if key == Qt.Key_Right:
                self._jump_relative(1)
            elif key == Qt.Key_Left:
                self._jump_relative(-1)
            elif key in (Qt.Key_Down, Qt.Key_Up):
                vbar = self.scroll.horizontalScrollBar()
                step = self.scroll.width() * 3 // 4
                vbar.setValue(vbar.value() + (step if key == Qt.Key_Down else -step))
        super().keyPressEvent(event)


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


class _ComicImageLabel(QLabel):
    """漫画单页图片（异步加载 + 跟随容器宽度自适应缩放）。

    loaded = Signal()：图片加载/重绘完成，通知宿主重排 gallery 高度。
    data URI / 本地文件字节的解压在后台线程进行（主线程仅 QPixmap.fromImage）。
    """

    loaded = Signal()

    def __init__(self, url, referer="", parent=None):
        super().__init__(parent)
        self.url = url
        self._referer = referer  # 防盗链 Referer（当前章节页 URL），传 CoverLoader
        self.setAlignment(Qt.AlignCenter)
        self._loading = True
        self._orig: QPixmap | None = None  # 原始像素图（缩放基准）
        self._fit_w = -1  # 上次缩放用的容器宽（容器未变则跳过重缩放）
        self.setText("加载中...")
        self.setMinimumWidth(200)
        # 懒加载占位：图片加载前先撑起估算高度，减少批量加载高度跳动/换话闪屏
        self.setMinimumHeight(600)
        self.setStyleSheet("border: 1px solid palette(mid); border-radius: 4px; padding: 4px;")
        self.setCursor(Qt.OpenHandCursor)

    def load(self) -> None:
        # data URI（Playwright canvas 提取的 base64 图）→ 后台解码
        if self.url and self.url.startswith("data:"):
            import base64 as _b64

            try:
                header, b64 = self.url.split(",", 1)
                data = _b64.b64decode(b64)
                self._decode_async(data)
                return
            except Exception:
                pass
            self._on_image(None)
            return
        # Playwright 返回的本地文件路径 → 后台读字节 + 解码
        if self.url and (self.url.startswith(("file://", "/", "\\")) or "\\" in self.url or self.url.startswith(".")):
            path = self.url.replace("file://", "")
            try:
                import pathlib

                data = pathlib.Path(path).read_bytes()
                self._decode_async(data)
                return
            except OSError:
                self._on_image(None)
                return
        from gui.components.cover_loader import CoverLoader

        # cache=False：漫画正文长图不进共享封面 LRU（流式阅读重看概率低，
        # 且一张长图几个 MB，会挤掉封面缓存；正文图内存由页面随滚动释放）
        CoverLoader.instance().load(
            self.url, self._on_image, referer=self._referer or None, cache=False
        )

    def _decode_async(self, data: bytes) -> None:
        """把图片字节交给后台线程解码，完成后主线程转 QPixmap。"""
        task = _PixmapDecodeTask(data)
        task.signals.done.connect(self._on_decoded)
        QThreadPool.globalInstance().start(task)

    def _on_decoded(self, img: "QImage") -> None:
        if img.isNull():
            self._on_image(None)
            return
        try:
            self._on_image(QPixmap.fromImage(img))
        except Exception:  # noqa: BLE001
            self._on_image(None)

    def _on_image(self, pixmap) -> None:
        if pixmap is None:
            self.setText("图片加载失败\n（可能需登录或已失效）")
            self.loaded.emit()
            return
        self._orig = pixmap
        self._fit()
        self.loaded.emit()  # 通知宿主：图片就绪，重排 gallery

    def _fit(self) -> None:
        """按当前容器宽度重绘（缩放/窗口变化时调用）。

        优化：容器宽未变（_fit_w 命中）跳过重缩放——滚动/resizeEvent 反复
        触发时不做全图 scaledToWidth（长图 1000×5000 每次重算极卡）。
        始终缩放到容器宽（含放大）：Ctrl+滚轮缩放要求图片双向跟随宽度，
        原图比容器窄时不放大会让放大方向无效果（用户感知"缩放没反应"）。
        """
        if self._orig is None:
            return
        # 容器未布局时宽度可能是 -1，兜底 600
        avail = self.width() if self.width() > 100 else 600
        if avail == self._fit_w:
            return  # 容器宽未变，无需重缩放
        pix = self._orig
        if pix.width() != avail:
            pix = pix.scaledToWidth(avail, Qt.SmoothTransformation)
        self._fit_w = avail
        self.setPixmap(pix)
        self.setMinimumHeight(min(pix.height() + 8, 4096))

    def resizeEvent(self, event) -> None:  # noqa: N802
        """容器宽度变化（窗口缩放 / Ctrl+滚轮）→ 重绘图片。"""
        super().resizeEvent(event)
        if self._orig is not None:
            self._fit()
            self.loaded.emit()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            # 点击放大查看（简化：切换为原始大小）
            pass
        super().mousePressEvent(event)


class _ComicSignals(QObject):
    """漫画加载信号。"""
    finished = Signal(object, object, object, object)  # (gen, chapter, images, err)
    partial = Signal(object, object, object, object)  # (gen, chapter, 已就绪前缀, None) 边抓边显示


class _LoadComicTask(QRunnable):
    """后台加载漫画话图片 URL（on_page 分批回调，边抓边显示）。"""

    def __init__(self, content, source, chapter, gen: int = 0, cancel_evt=None):
        super().__init__()
        self.signals = _ComicSignals()
        self._content = content
        self._source = source
        self._chapter = chapter
        self._gen = gen
        self._cancel_evt = cancel_evt  # 换书取消令牌：置位后分页抓取尽早退出

    def run(self) -> None:
        images, err = [], None
        try:
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

    def __init__(self, content, source, chapter, gen: int = 0, cancel_evt=None):
        super().__init__()
        self.signals = _PrefetchSignals()
        self._content = content
        self._source = source
        self._chapter = chapter
        self._gen = gen
        self._cancel_evt = cancel_evt  # 换书取消令牌：置位后分页抓取尽早退出

    def run(self) -> None:
        images, err = [], None
        try:
            images = self._content.fetch_comic_pages(
                self._source, self._chapter.url, cancel_evt=self._cancel_evt
            )
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(self._gen, self._chapter.url, images, err)
        except RuntimeError:
            pass
