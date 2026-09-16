"""视频阅读视图（video_view.py）—— VLC 内嵌播放器 + 现代化交互。

- 分集卡片网格：选集以块状卡片网格展示在播放面板下方（自适应列数），
  点卡切集、当前集高亮；无封面的分集显示纯文字块（palette midlight 圆角）
- 相关推荐：同源搜索当前作品（tag[0] / 标题），取前 8 条卡片同网格渲染，
  点击经 recommend_open_requested 打开新作品；无 Search/无结果自动隐藏
- VLC 内嵌播放区（python-vlc set_hwnd），通用支持 HLS/DASH 双流/MP4
- 现代化播放交互（refactor-shelf-player.md P1-P10）：
  - 控制条自动隐藏（3s 无操作隐藏 + 鼠标指针跟随隐藏）
  - 中央大播放按钮；单击视频区播放/暂停，双击全屏
  - 键盘快捷键：Space 播放暂停 / F 全屏 / M 静音 / ←→ ±5s / ↑↓ 音量 / ? 帮助
  - ⚙ 设置菜单收纳：画质 / 复制播放地址 / 刷新重试 / 外部播放器 / 快捷键帮助
  - 缓冲 spinner（播放启动到首帧期间显示）
  - 播放地址调试行不再常显（状态行仅加载/错误时可见）
- 播放源换源、播放地址缓存、预拉下一集、播完自动续播
- 契约保持：load/reload_detail/set_source_sid/source_changed/episode_changed/
  play_label/stop_playback/shutdown_video
"""

from __future__ import annotations

import webbrowser

from PySide6.QtCore import Qt, QEvent, Signal, QThreadPool, QRunnable, QObject, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from framework.content import Content, Detail

from gui.components.hover_title import HoverTitle


class _PlayPanel(QWidget):
    """外部播放器方案的播放面板（占位几何区，不做视觉占位）。

    独立播放器进程接管画面，本区不显示任何内容——播放入口 =
    覆盖层中央「▶ 播放」按钮。这里只承接交互：
    - 单击/双击面板 → 拉起外部播放器；移动鼠标 → 唤出控制条
    - 保持布局占位与键盘焦点（快捷键仍生效）
    """

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self._view = view
        # 无视觉占位：透明，画面区只显示中央播放按钮
        self.setStyleSheet("background: transparent;")
        # 无按键移动也触发 mouseMoveEvent → 控制条隐藏后滑动即唤出
        self.setMouseTracking(True)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        # 取流完成时用户在别的 Tab → 暂存的播放，回到阅读页补播
        self._view._flush_pending_play()

    def _click_timer(self) -> None:
        """单击/双击判定（双击后 300ms 无第二次点击才执行单击动作）。"""
        if self._view._click_pending:
            self._view._click_pending = False
            self._view._toggle_play_pause()

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._view._click_pending = True
            self._view._wake_controls()  # 点击面板 → 唤出控制条
            self.setFocus()  # 焦点收拢
            QTimer.singleShot(300, self._click_timer)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self._view._click_pending = False
        if event.button() == Qt.LeftButton:
            # 外部播放器方案：双击面板 = 拉起外部播放器打开当前集
            self._view._open_external()
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        self._view._wake_controls()
        super().mouseMoveEvent(event)

    def enterEvent(self, event):  # noqa: N802
        self._view._wake_controls()
        super().enterEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        self._view._handle_key(event)


class _VideoSignals(QObject):
    finished = Signal(object, object, object, object, object)  # (ep_url, video, audio, err, quality)


class _FetchStreamTask(QRunnable):
    """后台获取播放流（fetch_video_streams，覆盖 HLS/MP4/DASH 双流）。"""

    def __init__(self, content, source, ep_url, quality="best"):
        super().__init__()
        self.signals = _VideoSignals()
        self._content = content
        self._source = source
        self._url = ep_url
        self._quality = quality

    def run(self) -> None:
        video, audio, err = "", "", None
        try:
            # merged=True：取合并单流（含音视频）供 VLC 直接播——
            # 双流 input-slave 对 DASH/fMP4 不可靠（黑屏），单流最稳
            video, audio = self._content.fetch_video_streams(
                self._source, self._url, quality=self._quality, merged=True
            )
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        try:
            # quality 随任务回传：切画质后旧任务缓存 key 用发起时画质，
            # 不与当前画质错位（B3）
            self.signals.finished.emit(self._url, video, audio, err, self._quality)
        except RuntimeError:
            pass


class _RecommendSignals(QObject):
    finished = Signal(object, object, object, object)  # (source_id, keyword, results, err)


class _RecommendTask(QRunnable):
    """后台搜索当前作品的同源相关视频（推荐加载，失败静默）。"""

    def __init__(self, search, source, keyword):
        super().__init__()
        self.signals = _RecommendSignals()
        self._search = search
        self._source = source
        self._keyword = keyword

    def run(self) -> None:
        # 独立 HttpClient：requests.Session 非线程安全，后台线程不共享
        # 全局 Search 的实例（与 search_type 的 worker 同模式）
        http = None
        try:
            http = self._search._http.__class__(
                sleeper=getattr(self._search._http, "_sleeper", None),
                defaults=self._search._http.defaults,
            )
        except Exception:  # noqa: BLE001
            http = None
        results, err = [], None
        try:
            results = self._search.search_one(self._source, self._keyword, http=http)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        finally:
            if http is not None:
                try:
                    http.close()
                except Exception:  # noqa: BLE001
                    pass
        try:
            self.signals.finished.emit(
                getattr(self._source, "source_id", ""), self._keyword, results or [], err
            )
        except RuntimeError:
            pass


class _GridCard(QFrame):
    """块状卡片：封面（可选）在上 + 标题在下，点击触发。

    - 有封面：CoverLoader 异步回填缩略图（居中裁剪，尺寸与网格列对齐）
    - 无封面：隐藏封面区 → 纯文字块（palette midlight 圆角），观感如床头按钮
    - 有演员（detail.actor，源 detail.fields.actor 可选配置）：标题下增加
      一行小字号演员；无封面时演员行跟随标题居中
    - selected 属性：当前集高亮（亮色边框），供分集卡片选中态
    """

    clicked = Signal(object)  # 发射自身（host 按 p_data 区分分集/推荐）

    def __init__(self, title: str = "", cover_url: str = "", source_id: str = "",
                 actor: str = "", p_data=None, parent=None, source=None):
        super().__init__(parent)
        self.p_data = p_data
        self.cover_url = cover_url or ""
        self.source_id = source_id or ""
        self.actor = actor or ""
        self._source = source  # 所属源（供 CoverLoader 解密封面字节，私图床加密封面）
        self._selected = False
        self.setObjectName("mediaCard")
        self.setCursor(Qt.PointingHandCursor)
        card_h = 178 if self.actor else 158
        self.setFixedHeight(card_h)
        self.setStyleSheet(
            "QFrame#mediaCard { background: palette(midlight);"
            " border: 2px solid transparent; border-radius: 8px; }"
            "QFrame#mediaCard:hover { border-color: palette(highlight); }"
            "QFrame#mediaCard[mediaSelected=\"true\"] { border-color: palette(highlight); }"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(6)
        self._cover = QLabel(self)
        self._cover.setAlignment(Qt.AlignCenter)
        self._cover.setFixedHeight(100)
        self._cover.setStyleSheet(
            "background: palette(base); border-radius: 6px; color: palette(mid); font-size: 30px;"
        )
        self._cover.setText("▶")
        lay.addWidget(self._cover)

        self._title = HoverTitle(title or "", parent_card=self)
        self._title.setFixedHeight(20)
        self._title.setStyleSheet("font-size: 13px;")
        lay.addWidget(self._title, stretch=1)

        # 演员行（源 detail.fields.actor 配置；为空不显示，避免空行占位）
        self._actor_lbl = QLabel(self.actor, self)
        self._actor_lbl.setWordWrap(True)
        self._actor_lbl.setStyleSheet("color: palette(dark); font-size: 12px;")
        if self.actor:
            lay.addWidget(self._actor_lbl)
        else:
            self._actor_lbl.hide()

        if self.cover_url:
            self._load_cover()
        else:
            self._cover.hide()
            self._title.setAlignment(Qt.AlignCenter)
            self._actor_lbl.setAlignment(Qt.AlignCenter)

    def set_selected(self, on: bool) -> None:
        """刷新当前集选中态（属性驱动 QSS 边框）。"""
        on = bool(on)
        if on == self._selected:
            return
        self._selected = on
        self.setProperty("mediaSelected", on)
        style = self.style()
        style.unpolish(self)
        style.polish(self)

    def _load_cover(self) -> None:
        """异步加载卡片封面（CoverLoader 全局限流，居中裁剪显示）。"""
        from gui.components.cover_loader import CoverLoader

        def _on(pm):
            if pm is None or pm.isNull():
                return
            import shiboken6

            if not shiboken6.isValid(self._cover):
                return  # 卡片已销毁，跳过
            w = self._cover.width()
            h = self._cover.height()
            if w <= 0 or h <= 0:
                w, h = 160, 100  # 未布局兜底
            scaled = pm.scaled(
                w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            sx = max(0, (scaled.width() - w) // 2)
            sy = max(0, (scaled.height() - h) // 2)
            cropped = scaled.copy(sx, sy, min(w, scaled.width()), min(h, scaled.height()))
            self._cover.setPixmap(cropped)
            from gui.components.cover_loader import fade_in

            fade_in(self._cover)

        CoverLoader.instance().load(
            self.cover_url, _on, cache=True, persist=False,
            source_id=self.source_id, source=self._source,
        )

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self)
        super().mouseReleaseEvent(event)


class VideoView(QWidget):
    """视频分集 + VLC 内嵌播放视图（现代化交互，支持多播放源换源）。"""

    episode_changed = Signal(object)  # (detail, 集标题, 集URL) → 进度记忆
    position_changed = Signal(object)  # (detail, 标题, URL, 播放进度 0~1, None) 续读
    source_changed = Signal(object)  # (detail, new_sid) → ReaderPage 换源
    download_requested = Signal(object)  # (source_id, detail.url, content_type) → 下载当前作品
    recommend_open_requested = Signal(object)  # (source_id, url, content_type) → 打开推荐视频

    # 快捷键帮助内容（? 键浮层）
    _HELP_TEXT = (
        "操作指南\n"
        "──────────────\n"
        "播放\n"
        "  单击面板 / 中央播放键    在外部播放器中打开当前集\n"
        "  双击面板                  打开外部播放器（VLC）\n"
        "  设置 → 外部播放器         手动重新拉起播放器\n"
        "  ?                        显示 / 隐藏本帮助\n"
        "──────────────\n"
        "播放器内操作（VLC 桌面版）\n"
        "  空格 / ← → / ↑ ↓ / M / F  播放、快进快退、音量、静音、全屏\n"
        "  播放器自带「恢复播放位置」，下次打开自动续播\n"
    )

    def __init__(self, content: Content, parent=None, search=None):
        super().__init__(parent)
        self._content = content
        self._search = search  # 可选：同源搜索相关推荐（未注入则不显示推荐区）
        self._source = None
        self._detail: Detail | None = None
        self._episodes = []
        self._ep_cards = []  # 分集卡片（顺序 = 集序）
        self._rec_cards = []  # 相关推荐卡片
        self._last_cols = -1  # 网格上次列数（-1 强制重排）
        self._last_col_w = -1  # 网格上次单卡宽度
        self._col_w = 0  # 网格单卡宽度（随窗口宽度自适应）
        self._recommend_keyword = ""  # 推荐搜索去重关键词
        self._current_idx = -1
        self._selection_mode = False  # 多集源选集态（停选集等用户选，不自动播）
        self._current_play = ""  # 单流播放地址（展示/复制）
        self._current_audio = ""  # DASH 音频轨（外接播放器 input-slave 挂入）
        self._current_title = ""  # 当前集标题（重开播放器/浮层提示）
        self._source_list = []  # [{sid, name, ...}]（同站多线路）
        self._current_sid = ""
        self._switching = False
        self._stream_cache: dict = {}  # {(ep_url, quality): (video, audio)} 播放用
        self._prefetch_idx = -2  # 正在预拉下一集（<0 空闲）
        self._quality = "best"
        self._quality_options: list = []
        self._detail_url_for_play = ""  # 无分集时记录详情 URL
        self._player = None
        self._fs_win = None  # 全屏顶层窗口
        self._last_pos_save_ts = 0.0  # 上次播放进度存盘时间戳（节流 2s 存一次）
        self._pending_position = None  # 打开书续读播放位置（0~1），播放开始后 seek
        self._has_played = False  # 是否真正开始过播放（未播放不落盘，防覆盖恢复进度）
        self._pending_play = None  # 取流完成但视图不可见 → 暂存 (video, audio, title)，显示后再播
        self._click_pending = False  # 单击/双击判定
        from framework.media_tuner import MediaTuner

        self._tuner = MediaTuner()  # 卡顿统计/缓冲升级（每次 load 重置）
        self._hide_timer = QTimer(self)  # 控制条自动隐藏
        self._hide_timer.setInterval(3000)
        self._hide_timer.timeout.connect(self._hide_controls)

        self.setFocusPolicy(Qt.StrongFocus)  # 视图级快捷键
        self._build_ui()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # ---- 播放源选择（换源站显示，普通源隐藏）----
        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        self.source_label = QLabel("播放源：")
        source_row.addWidget(self.source_label)
        self.source_combo = QComboBox()
        self.source_combo.currentIndexChanged.connect(self._on_source_switch)
        source_row.addWidget(self.source_combo, stretch=1)
        layout.addLayout(source_row)
        self.source_label.setVisible(False)
        self.source_combo.setVisible(False)

        # ---- 主体：播放面板在上，分集/推荐卡片网格在下 ----
        body = QVBoxLayout()
        body.setSpacing(8)

        # 播放面板（外部播放器方案：不再内嵌视频渲染，只做播放入口占位；
        # 播放最终跳转外部播放器，无需大的画面区 → 高度压小，让网格区更多空间）
        self._video_frame = _PlayPanel(self)
        self._video_frame.setMinimumHeight(140)
        self._video_frame.setFocusPolicy(Qt.StrongFocus)
        body.addWidget(self._video_frame, stretch=1)

        # ---- 覆盖层：中央播放按钮 / 缓冲 spinner / 帮助浮层 ----
        self._build_overlays()

        # ---- 沉浸式控制条：悬浮叠加在视频画面内部（随视频走，不动自动隐藏）----
        self.control_bar = QWidget(self._overlay_root)
        self.control_bar.setObjectName("videoControlBar")
        self.control_bar.setStyleSheet(
            "QWidget#videoControlBar { background: rgba(0,0,0,150);"
            " border-radius: 8px; }"
        )
        cb = QHBoxLayout(self.control_bar)
        cb.setContentsMargins(8, 4, 8, 4)
        cb.setSpacing(6)
        self._build_control_bar(cb)

        # ---- 可滚动卡片网格区（分集在上，相关推荐在下）----
        self._cards_scroll = QScrollArea()
        self._cards_scroll.setWidgetResizable(True)
        self._cards_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._cards_container = QWidget()
        self._cards_vbox = QVBoxLayout(self._cards_container)
        self._cards_vbox.setContentsMargins(0, 0, 0, 0)
        self._cards_vbox.setSpacing(12)

        self.ep_section_label = QLabel("选集")
        self.ep_section_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        self._cards_vbox.addWidget(self.ep_section_label)
        self._ep_grid = QGridLayout()
        self._ep_grid.setSpacing(12)
        self._cards_vbox.addLayout(self._ep_grid)

        # 相关推荐：默认隐藏，后台搜索出结果再显示
        self.rec_section_label = QLabel("相关推荐")
        self.rec_section_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.rec_section_label.hide()
        self._cards_vbox.addWidget(self.rec_section_label)
        self._rec_grid = QGridLayout()
        self._rec_grid.setSpacing(12)
        self._cards_vbox.addLayout(self._rec_grid)

        self._cards_vbox.addStretch(1)
        self._cards_scroll.setWidget(self._cards_container)
        body.addWidget(self._cards_scroll, stretch=3)

        self._right_layout = body  # 全屏退出后把视频区插回（body 第 0 位）
        layout.addLayout(body, stretch=1)
        self._reposition_overlays()  # 初始定位控制条/覆盖层
        # 外部播放器方案：播放画面在独立播放器进程内，App 内不再内嵌渲染 →
        # 进度/音量/倍速/全屏等内嵌播放控件禁用（操作交给外部播放器自身）
        self._disable_embedded_controls()
        # 全局事件过滤器：鼠标唤出控制条 + 全屏窗键盘/关闭兜底
        # （class 方法才进 Qt 事件分发；实例属性赋值在 PySide6 永不回调）
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _build_overlays(self) -> None:
        """视频区覆盖层：中央播放钮 / 缓冲 spinner / 帮助浮层。"""
        frame = self._video_frame
        self._overlay_root = QWidget(frame)
        self._overlay_root.setGeometry(frame.rect())
        # 覆盖层开 mouseTracking（官方语义：无按键也持续收 MouseMove）→
        # 鼠标移动事件落在覆盖层上进入全局 eventFilter → 唤出控制条。
        # 注意：**不能开 WA_TransparentForMouseEvents**——官方文档明确它对
        # 子级同样禁用鼠标事件（"to the widget AND ITS CHILDREN"），会弄废
        # 控制条按钮/中央播放键交互。也不要用实例属性赋值挂事件——
        # PySide6 里不进 Qt 虚表永不回调（唤出失效根因之一）。
        self._overlay_root.setMouseTracking(True)

        # 中央播放按钮（未播放时显示，点击在外部播放器中打开当前集）
        self.center_play_btn = QPushButton("▶ 播放", self._overlay_root)
        self.center_play_btn.setFixedSize(96, 96)
        self.center_play_btn.setCursor(Qt.PointingHandCursor)
        self.center_play_btn.setToolTip("在外部播放器中播放当前集（双击面板亦可）")
        self.center_play_btn.setStyleSheet(
            "QPushButton { background: rgba(0,0,0,150); color: white;"
            " border: 2px solid rgba(255,255,255,120); border-radius: 48px;"
            " font-size: 22px; padding: 0px; }"
            "QPushButton:hover { background: rgba(40,40,40,200);"
            " border-color: rgba(255,255,255,200); }"
        )
        self.center_play_btn.clicked.connect(self._toggle_play_pause)

        # 缓冲 spinner（播放启动到首帧期间显示）
        self.buffer_spinner = QProgressBar(self._overlay_root)
        self.buffer_spinner.setRange(0, 0)  # 不定模式（忙碌动画）
        self.buffer_spinner.setFixedSize(56, 56)
        self.buffer_spinner.setTextVisible(False)
        self.buffer_spinner.hide()

        # 状态浮层（play_label 契约保留）：仅加载/错误时显示，覆盖在视频区内
        # —— 不占主布局，加载/错误不再引起控制条/分集列表布局跳动
        self.play_label = QLabel(self._overlay_root)
        self.play_label.setWordWrap(True)
        self.play_label.setAlignment(Qt.AlignCenter)
        self.play_label.setStyleSheet(
            "background: rgba(0,0,0,170); color: white;"
            " border-radius: 8px; padding: 8px 14px; font-size: 13px;"
        )
        self.play_label.hide()

        # 快捷键帮助浮层（? 键显示）
        self.help_overlay = QFrame(self._overlay_root)
        self.help_overlay.setStyleSheet(
            "QFrame { background: rgba(20,20,20,230); color: white;"
            " border-radius: 10px; font-size: 13px; padding: 14px; }"
        )
        help_lbl = QLabel(self._HELP_TEXT, self.help_overlay)
        help_lbl.setStyleSheet("background: transparent; color: white;")
        help_lbl.setAlignment(Qt.AlignCenter)
        help_lbl.adjustSize()
        self.help_overlay.hide()

    def _icon_btn(self, text: str, tip: str, slot, width: int = 36) -> QPushButton:
        """控制条图标按钮：覆写全局 QSS 的 8px 18px padding（窄按钮会挤掉文字）。

        点击后焦点归还视频区 → 快捷键（空格/F/M/方向键）始终可用。
        """
        b = QPushButton(text)
        b.setFixedWidth(width)
        b.setToolTip(tip)
        b.clicked.connect(slot)
        b.clicked.connect(self._video_frame.setFocus)
        b.setStyleSheet("padding: 0px;")
        return b

    def _build_control_bar(self, cb: QHBoxLayout) -> None:
        """控制条：播放 / 上下集 / 进度 / 时间 / 音量 / 倍速 / 设置 / 全屏。"""
        self.prev_btn = self._icon_btn("⏮", "上一集", self._on_prev_ep)
        cb.addWidget(self.prev_btn)

        self.play_btn = self._icon_btn("▶", "播放 / 暂停（空格）", self._toggle_play_pause)
        cb.addWidget(self.play_btn)

        self.next_btn = self._icon_btn("⏭", "下一集", self._on_next_ep)
        cb.addWidget(self.next_btn)

        self.progress = QSlider(Qt.Horizontal)
        self.progress.setRange(0, 1000)
        # 细进度条 hover 变粗（现代播放器模式 P6）
        self.progress.setStyleSheet(
            "QSlider::groove:horizontal { height: 4px; background: palette(mid);"
            " border-radius: 2px; }"
            "QSlider::sub-page:horizontal { background: palette(highlight);"
            " border-radius: 2px; }"
            "QSlider::handle:horizontal { width: 12px; margin: -4px 0;"
            " background: palette(highlight); border-radius: 6px; }"
            "QSlider:hover::groove:horizontal { height: 10px; border-radius: 5px; }"
            "QSlider:hover::handle:horizontal { width: 14px; margin: -2px 0; }"
        )
        cb.addWidget(self.progress, stretch=1)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet("color: palette(dark); font-size: 11px;")
        cb.addWidget(self.time_label)

        self.vol_btn = self._icon_btn("🔊", "静音由外部播放器接管", lambda: None, width=32)
        cb.addWidget(self.vol_btn)

        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(80)
        self.vol_slider.setFixedWidth(80)
        self.vol_slider.sliderReleased.connect(self._video_frame.setFocus)
        cb.addWidget(self.vol_slider)

        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["1.0x", "0.5x", "0.75x", "1.25x", "1.5x", "2.0x"])
        self.speed_combo.setCurrentIndex(0)
        self.speed_combo.setFixedWidth(64)
        self.speed_combo.setToolTip("播放倍速")
        self.speed_combo.currentTextChanged.connect(self._on_speed_changed)
        self.speed_combo.activated.connect(lambda _: self._video_frame.setFocus())
        cb.addWidget(self.speed_combo)

        # 换源按钮（⇄）：播放界面直接可见的换源入口，多源站显示、单源隐藏
        self.source_btn = QToolButton()
        self.source_btn.setText("⇄")
        self.source_btn.setPopupMode(QToolButton.InstantPopup)
        self.source_btn.setToolTip("切换播放源")
        self.source_btn.setFixedWidth(32)
        self.source_btn.setVisible(False)
        self.source_menu = QMenu(self)
        self.source_btn.setMenu(self.source_menu)
        self.source_menu.aboutToHide.connect(self._video_frame.setFocus)
        cb.addWidget(self.source_btn)

        # 设置菜单（⚙）：画质 + 复制地址 + 刷新 + 外部播放器 + 帮助
        self.settings_btn = QToolButton()
        self.settings_btn.setText("⚙")
        self.settings_btn.setPopupMode(QToolButton.InstantPopup)
        self.settings_btn.setToolTip("设置")
        self.settings_btn.setFixedWidth(32)
        self._settings_menu = QMenu(self)
        self._build_settings_menu()
        self.settings_btn.setMenu(self._settings_menu)
        self._settings_menu.aboutToHide.connect(self._video_frame.setFocus)
        cb.addWidget(self.settings_btn)

        self.fs_btn = QPushButton("⛶")
        self.fs_btn.setFixedWidth(32)
        self.fs_btn.setToolTip("全屏（F）")
        self.fs_btn.clicked.connect(self._toggle_fullscreen)
        self.fs_btn.clicked.connect(self._video_frame.setFocus)
        self.fs_btn.setStyleSheet("padding: 0px;")
        cb.addWidget(self.fs_btn)

        # 下载当前作品（⏬）：与顶部「下载」同链路，播放器内即可触发
        self.dl_btn = self._icon_btn("⏬", "下载当前作品", self._on_download_clicked, width=32)
        cb.addWidget(self.dl_btn)

    def _on_download_clicked(self) -> None:
        """播放器内「⏬ 下载」→ 转发 App 层弹章节范围（默认当前集→末集）。

        不再自带 QInputDialog「前 N 集」；由 App 统一弹 DownloadRangeDialog，
        默认从当前集下到最后一集，用户可改全部/任意范围，并保留画质选择。
        """
        if self._source is None or self._detail is None:
            return
        sid = getattr(self._source, "source_id", "") or self._current_sid
        idx = self._current_idx + 1 if 0 <= self._current_idx < len(self._episodes) else 1
        self.download_requested.emit(
            (sid, self._detail.url,
             getattr(self._detail, "content_type", ""), idx)
        )

    def _disable_embedded_controls(self) -> None:
        """禁用内嵌播放专属控件（外接方案下无内嵌状态可管）。

        播放/下一集/选集/换源/画质/复制/下载仍有效——播放入口 = 拉起外部
        播放器；进度/时间/音量/倍速/全屏由外部播放器接管，禁用并注明。
        """
        for w in (self.progress, self.time_label, self.vol_btn, self.vol_slider,
                  self.speed_combo, self.fs_btn):
            w.setEnabled(False)
            w.setToolTip("外部播放器播放（VLC 桌面版）")
        self.progress.setToolTip("进度/暂停由外部播放器接管")
        self.time_label.setText("外部播放器")
        self.play_btn.setToolTip("在外部播放器中打开当前集")
        self.next_btn.setToolTip("在外部播放器中打开下一集")

    def _build_settings_menu(self) -> None:
        menu = self._settings_menu
        menu.clear()

        # 画质子菜单（源无画质选项时隐藏）
        self.quality_label = QLabel("画质：")  # 兼容旧引用（不再显示于控制条）
        self.quality_combo = QComboBox()  # 兼容旧引用（选择在菜单内）
        self.quality_menu = menu.addMenu("画质")
        # 选集菜单（全屏/非全屏均可切集，无需回到左侧列表）
        self.ep_menu = menu.addMenu("选集")

        menu.addSeparator()
        menu.addAction("📋 复制播放地址").triggered.connect(self._copy)
        menu.addAction("🔄 刷新播放").triggered.connect(self._retry_play)
        menu.addAction("↗ 外部播放器").triggered.connect(self._open_external)
        menu.addSeparator()
        menu.addAction("? 快捷键帮助").triggered.connect(self._toggle_help)

        self._quality_actions = []  # 防止菜单项被 GC
        self._refresh_quality_menu()
        self._refresh_ep_menu()
        self._refresh_source_menu()

    def _refresh_quality_menu(self) -> None:
        """按源配置重建画质菜单项（无画质选项 → 菜单隐藏）。"""
        self.quality_menu.clear()
        self._quality_actions.clear()
        for q in self._quality_options:
            act = self.quality_menu.addAction(q)
            act.setCheckable(True)
            act.setChecked(q == self._quality)
            act.triggered.connect(lambda _=False, _q=q: self._on_quality_changed(_q))
            self._quality_actions.append(act)
        self.quality_menu.menuAction().setVisible(bool(self._quality_options))

    def _refresh_ep_menu(self) -> None:
        """重建选集菜单：当前集打勾，点击即切换（播放器内操作，全屏可用）。"""
        self.ep_menu.clear()
        self._ep_actions = []
        for i, ep in enumerate(self._episodes):
            act = self.ep_menu.addAction(ep.title or f"第{i + 1}集")
            act.setCheckable(True)
            act.setChecked(i == self._current_idx)
            act.triggered.connect(lambda _=False, _i=i: self._select_episode(_i))
            self._ep_actions.append(act)
        self.ep_menu.menuAction().setVisible(bool(self._episodes))

    def _select_episode(self, idx: int) -> None:
        """播放器内选集：加载该集并同步卡片选中态/选集菜单。

        选集态下点当前高亮集也视为选择 → 立即取流播放；
        播放中点同一集不重载。
        """
        if not (0 <= idx < len(self._episodes)):
            return
        if idx == self._current_idx and not self._selection_mode:
            return
        self._load_episode(idx)
        self._refresh_ep_menu()

    def _refresh_source_menu(self) -> None:
        """重建播放源菜单：站内多线路（⇄）。

        按钮在有作品时显示。
        """
        self.source_menu.clear()
        self._source_actions = []
        for i, item in enumerate(self._source_list):
            name = item.get("name") if isinstance(item, dict) else str(item)
            sid = item.get("sid") if isinstance(item, dict) else str(item)
            act = self.source_menu.addAction(str(name))
            act.setCheckable(True)
            act.setChecked(str(sid) == self._current_sid)
            act.triggered.connect(
                lambda _=False, _i=i: self._select_source(_i))
            self._source_actions.append(act)
        self.source_menu.menuAction().setVisible(True)
        self.source_btn.setVisible(self._detail is not None)
        self.source_btn.setToolTip(
            "切换播放源" if self._detail is not None else "")

    def _select_source(self, idx: int) -> None:
        """播放器内换源：同步顶部下拉框触发换源流程。"""
        if not (0 <= idx < self.source_combo.count()):
            return
            self.source_combo.setCurrentIndex(idx)  # → currentIndexChanged → _on_source_switch

    # ------------------------------------------------------------------ #
    # 控制条自动隐藏
    # ------------------------------------------------------------------ #
    def _wake_controls(self) -> None:
        """鼠标移动/触碰 → 显示控制条 + 重置隐藏定时器。"""
        if not self.control_bar.isVisible():
            self.control_bar.show()
            if self._fs_win is not None:
                self._fs_titlebar.show()
        self._hide_timer.start()
        if self._player is not None and self._player.is_playing():
            self.setCursor(Qt.ArrowCursor)

    def _hide_controls(self) -> None:
        """3s 无操作 → 隐藏控制条（仅播放中隐藏；菜单打开时除外）。"""
        if self.settings_btn.menu().isVisible():
            self._hide_timer.start()
            return
        if self._player is not None and self._player.is_playing():
            self.control_bar.hide()
            if self._fs_win is not None:
                self._fs_titlebar.hide()
            self.setCursor(Qt.BlankCursor)

    def _handle_key(self, event) -> None:
        """键盘快捷键（外部播放器方案：仅保留帮助；播放/暂停/快进等
        由外部播放器自身快捷键接管，App 内不再重复绑定）。"""
        key = event.key()
        if key == Qt.Key_Question or key == Qt.Key_Slash:
            self._toggle_help()
            event.accept()
            return
        event.ignore()

    def keyPressEvent(self, event):  # noqa: N802
        self._handle_key(event)

    def eventFilter(self, obj, event):  # noqa: N802
        """全局兜底事件：鼠标唤出控制条 + 全屏窗键盘/关闭/双击。"""
        try:
            return self._event_filter_impl(obj, event)
        except Exception:  # noqa: BLE001
            return False

    def _event_filter_impl(self, obj, event) -> bool:
        """（实现）见 eventFilter 注释。

        此前用实例属性赋值（fs.mouseMoveEvent = lambda ...）挂事件——
        PySide6 里这种赋值不进 Qt 虚表，事件分发永不回调（鼠标唤出失效
        根因）。全部收敛到这里（class 方法，可靠）。
        注意：鼠标事件可能直接发给原生 QWindow（非 QWidget），此时跳过
        widget 链判断（isAncestorOf 不接受 QWindow，异常会被 eventFilter
        吞掉导致整个分支失效——真机唤出失效的第二个根因）。
        - MouseMove/Enter 落在视频区或全屏窗内 → 唤出控制条
          （视频区 MouseMove 是高频事件，仅两次 isAncestorOf 判定，开销可忽略）
        - 全屏窗内 KeyPress：Esc 退出全屏，其余转 _handle_key 快捷键
        - 全屏窗被关闭（Alt+F4）→ 回主视图
        - 全屏窗非视频区双击 → 退出全屏（视频区双击由 _VideoFrame 处理 toggle）
        """
        if not isinstance(obj, QWidget):
            return False
        # 视图不可见且未全屏时（如切到其他页/弹窗创建控件）直接放行，
        # 避免对无关控件的每个事件做父链/isAncestorOf 判定卡顿弹窗。
        if not self.isVisible() and self._fs_win is None:
            return False
        if event.type() in (QEvent.MouseMove, QEvent.Enter):
            if obj is self._video_frame or self._video_frame.isAncestorOf(obj):
                self._wake_controls()
                return False
            fs = self._fs_win
            if fs is not None and (obj is fs or fs.isAncestorOf(obj)):
                self._wake_controls()
                return False
            return False
        fs = self._fs_win
        if fs is None:
            return False
        if event.type() == QEvent.KeyPress and (obj is fs or fs.isAncestorOf(obj)):
            if event.key() == Qt.Key_Escape:
                self._exit_fullscreen()
            else:
                self._handle_key(event)
            return True
        if event.type() == QEvent.Close and obj is fs:
            self._exit_fullscreen()
            return True
        if (event.type() == QEvent.MouseButtonDblClick and obj is not self._video_frame
                and not self._video_frame.isAncestorOf(obj)
                and (obj is fs or fs.isAncestorOf(obj))):
            self._exit_fullscreen()
            return True
        return False

    # ------------------------------------------------------------------ #
    # 覆盖层（中央按钮 / spinner / 帮助）
    # ------------------------------------------------------------------ #
    def _sync_overlay_state(self, playing: bool) -> None:
        """根据播放状态切换中央播放按钮/缓冲 spinner 显隐。"""
        self.center_play_btn.setVisible(not playing)
        self._reposition_overlays()

    def _reposition_overlays(self) -> None:
        """覆盖层随视频区尺寸变化居中（resizeEvent 兜底 + 全屏切换后）。"""
        frame = self._video_frame
        self._overlay_root.setGeometry(frame.rect())
        w, h = frame.width(), frame.height()
        if w <= 0 or h <= 0:
            return
        # 沉浸控制条：悬浮在视频画面底部中央（YouTube/B站 风格）。
        # 先量尺寸：矮播放框（<196）时中央按钮与它重叠 → 上移按钮避让
        self.control_bar.adjustSize()
        cb_w = min(w - 32, 920)
        self.control_bar.resize(cb_w, self.control_bar.height())
        self.control_bar.move((w - cb_w) // 2, h - self.control_bar.height() - 12)
        # 控制条上方安全区（留 12 间距）
        cb_vtop = h - self.control_bar.height() - 24
        cy = (h - 96) // 2
        if cy + 96 > cb_vtop:
            cy = max(0, (cb_vtop - 96) // 2)
        self.center_play_btn.move((w - 96) // 2, cy)
        self.buffer_spinner.move((w - 56) // 2,
                                 max(0, (min(cb_vtop, h) - 56) // 2))
        self.help_overlay.adjustSize()
        self.help_overlay.move((w - self.help_overlay.width()) // 2,
                               (h - self.help_overlay.height()) // 2)
        if self.play_label.isVisible():
            self.play_label.adjustSize()
            self.play_label.move((w - self.play_label.width()) // 2,
                                 h - self.play_label.height() - 24)

    def _toggle_help(self) -> None:
        """? 键：显示/隐藏快捷键帮助浮层。"""
        if self.help_overlay.isVisible():
            self.help_overlay.hide()
            return
        self._reposition_overlays()
        self.help_overlay.show()
        # 3s 后自动消失
        QTimer.singleShot(3000, lambda: self.help_overlay.hide()
                          if self.help_overlay.isVisible() else None)

    # ------------------------------------------------------------------ #
    def _populate_ep_cards(self, chapters) -> None:
        """重建分集卡片网格：每集一张卡，封面用本集缩略图、缺省回退作品封面。

        无封面 → 纯文字块；卡片按序入 _ep_cards，点击切集。
        """
        for c in self._ep_cards:
            c.deleteLater()
        self._ep_cards.clear()
        self._wipe_grid(self._ep_grid)
        sid = getattr(self._source, "source_id", "") or self._current_sid
        detail_cover = (getattr(self._detail, "cover", "") or "").strip()
        detail_actor = (getattr(self._detail, "actor", "") or "").strip()
        for i, ep in enumerate(chapters):
            cover = (getattr(ep, "cover", "") or "").strip()
            if not cover:
                cover = detail_cover
            card = _GridCard(
                ep.title or f"第{i + 1}集", cover_url=cover, source_id=sid,
                actor=detail_actor, p_data=i, parent=self._cards_container,
                source=self._source,
            )
            card.clicked.connect(self._on_ep_card)
            self._ep_cards.append(card)
        self.ep_section_label.setVisible(bool(self._ep_cards))
        self._last_cols = -1  # 强制按当前宽度重排
        self._reflow_cards()
        self._paint_card_selection()

    def _paint_card_selection(self) -> None:
        """按当前集刷新分集卡片选中态高亮。"""
        for i, card in enumerate(self._ep_cards):
            card.set_selected(i == self._current_idx)

    def _wipe_grid(self, grid) -> None:
        """清空网格布局项（卡片对象由持有列表管理，不在此销毁）。"""
        while grid.count():
            grid.takeAt(0)

    def _grid_columns(self) -> int:
        """按可视宽度算列数（每列约 190px，2~8 列；窄窗口回退保最小卡宽）。"""
        view_w = self._cards_scroll.viewport().width() or self.width() or 900
        cols = min(8, max(2, view_w // 190))
        while cols > 2 and (view_w - 12 * (cols - 1)) // cols < 120:
            cols -= 1
        self._col_w = max(120, (view_w - 12 * (cols - 1)) // cols)
        return cols

    def _reflow_cards(self) -> None:
        """把分集/推荐卡片按当前列数重排（等宽均匀铺开，响应窗口 resize）。"""
        cols = self._grid_columns()
        if cols == self._last_cols and self._col_w == self._last_col_w:
            return
        self._last_cols = cols
        self._last_col_w = self._col_w
        for grid, cards in ((self._ep_grid, self._ep_cards),
                            (self._rec_grid, self._rec_cards)):
            self._wipe_grid(grid)
            for i, card in enumerate(cards):
                if card.width() != self._col_w and self._col_w > 0:
                    card.setFixedWidth(self._col_w)
                row, col = divmod(i, cols)
                grid.addWidget(card, row, col)
            for c in range(grid.columnCount()):
                grid.setColumnStretch(c, 0)
            for c in range(cols):
                grid.setColumnStretch(c, 1)

    # ------------------------------------------------------------------ #
    # 相关推荐（同源搜索，未注入 Search/无结果自动隐藏）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _recommend_keyword_of(detail) -> str:
        """推荐关键词：优先详情 tags[0]，回退标题短前缀（前 3 字）。

        tags 缺失/为空（如 18mh-video 详情只有 title）时用完整标题搜索只会命中
        自身，剔除后推荐区为空；短前缀能命中同系列/同题材的其它作品。
        """
        for t in (getattr(detail, "tags", None) or []):
            t = str(t or "").strip()
            if t:
                return t
        return (getattr(detail, "title", "") or "").strip()[:3]

    def _maybe_load_recommendations(self) -> None:
        """后台搜索当前作品同源相关视频（换源/换作品后重搜）。"""
        if self._search is None or self._source is None or self._detail is None:
            return
        kw = self._recommend_keyword_of(self._detail)
        if not kw or kw == self._recommend_keyword:
            return
        self._recommend_keyword = kw
        task = _RecommendTask(self._search, self._source, kw)
        task.signals.finished.connect(self._on_recommend_done)
        self._recommend_task = task  # 防 GC
        QThreadPool.globalInstance().start(task)

    def _on_recommend_done(self, source_id, keyword, results, err) -> None:
        """推荐搜索结果落地：剔除自身 + URL 去重，取前 8 条渲染；空则隐藏推荐区。"""
        if self._detail is None or keyword != self._recommend_keyword:
            return  # 换作品后旧搜索后到，丢弃
        picks = []
        seen_urls = set()
        for r in results or []:
            url = getattr(r, "url", "") or ""
            if not url or url == self._detail.url or url in seen_urls:
                continue
            seen_urls.add(url)
            picks.append(r)
            if len(picks) >= 8:
                break
        self._render_rec_cards(picks)

    def _render_rec_cards(self, results) -> None:
        for c in self._rec_cards:
            c.deleteLater()
        self._rec_cards.clear()
        self._wipe_grid(self._rec_grid)
        self.rec_section_label.hide()
        if not results:
            self._last_cols = -1
            self._reflow_cards()
            return
        sid = getattr(self._source, "source_id", "") or self._current_sid
        for r in results:
            card = _GridCard(
                (r.title or "")[:60], cover_url=getattr(r, "cover", "") or "",
                source_id=sid, p_data=r, parent=self._cards_container,
                source=self._source,
            )
            card.clicked.connect(self._on_rec_card)
            self._rec_cards.append(card)
        self.rec_section_label.show()
        self._last_cols = -1  # 强制重排（新增推荐卡）
        self._reflow_cards()

    def _on_rec_card(self, card) -> None:
        """点推荐卡 → 通知 ReaderPage 打开该作品（走现有 open 流程）。"""
        if self._detail is None:
            return
        r = card.p_data
        url = getattr(r, "url", "") or ""
        if not url or url == self._detail.url:
            return
        sid = getattr(r, "source_id", "") or (
            getattr(self._source, "source_id", "") or self._current_sid)
        ctype = getattr(self._detail, "content_type", "video") or "video"
        self.recommend_open_requested.emit((sid, url, ctype))

    def load(self, source, detail: Detail, start_ep_url: str = "", restore_position: float | None = None) -> None:
        self._source = source
        self._detail = detail
        self._pending_play = None  # 换书清掉旧暂存播放
        self._has_played = False  # 未真正播放不把选集态误存为续读进度
        self._selection_mode = False  # 换书重置选集态
        self._tuner.reset()  # 新播放会话重置卡顿统计
        # 换书：无条件清零续读位置（旧书残留的 _pending_position 会被新书的
        # 首个选集/播放回调消费，把新书错 seek 到旧书位置）——与 novel/comic
        # 同模式修复跨书串位置 bug。
        self._pending_position = None
        if restore_position is not None:
            self._pending_position = restore_position
        # 换视频先停旧播放（不堆积缓存/后台占用）。
        self._stop_player()
        self._episodes = detail.chapters
        self._stream_cache.clear()
        self._prefetch_idx = -2
        self._recommend_keyword = ""  # 换作品强制重搜推荐
        self._populate_source_combo(detail)
        self._populate_quality_combo(source)
        self._populate_ep_cards(detail.chapters)
        idx = 0
        if start_ep_url:
            for i, ep in enumerate(detail.chapters):
                if ep.url == start_ep_url:
                    idx = i
                    break
        self._current_idx = idx if detail.chapters else -1
        self._sync_overlay_state(playing=False)
        self._refresh_ep_menu()
        self._maybe_load_recommendations()
        if not detail.chapters:
            # 无分集（season 页）→ 直接取详情页播放地址自动播放
            self._show_status("正在获取播放流...")
            self._current_idx = -1
            self._detail_url_for_play = detail.url
            task = _FetchStreamTask(self._content, self._source, detail.url, self._quality)
            task.signals.finished.connect(self._on_stream_loaded)
            self._stream_task = task
            QThreadPool.globalInstance().start(task)
            return
        if len(detail.chapters) >= 2:
            # 多集源：停在选集态，不自动取流/播放，由用户点选集数
            self._selection_mode = True
            self._paint_card_selection()  # 高亮当前应选中的集（默认第 0 或续读集）
            self._show_status("请选择要播放的集数")
            return
        self._load_episode(idx)

    def reload_detail(self, new_detail: Detail) -> None:
        """换源重载分集后调用：多集源停选集态等用户选择，单集/无分集自动取流播放。"""
        self._stop_player()  # 换源先停旧播放流
        self._detail = new_detail
        self._episodes = new_detail.chapters
        self._stream_cache.clear()
        self._prefetch_idx = -2
        self._detail_url_for_play = ""
        self._recommend_keyword = ""  # 换源后按新源重搜推荐
        self._has_played = False  # 新源尚未播放，不把选集态误存为续读进度
        self._selection_mode = False  # 重置选集态
        self._populate_ep_cards(new_detail.chapters)
        self._switching = False
        self._sync_overlay_state(playing=False)
        self._current_idx = 0 if new_detail.chapters else -1
        self._refresh_ep_menu()
        self._maybe_load_recommendations()
        if not new_detail.chapters:
            # 无分集（season 页）→ 直接取详情页播放地址自动播放
            self._detail_url_for_play = new_detail.url
            self._show_status("正在获取播放流...")
            task = _FetchStreamTask(self._content, self._source, new_detail.url, self._quality)
            task.signals.finished.connect(self._on_stream_loaded)
            self._stream_task = task
            QThreadPool.globalInstance().start(task)
            return
        if len(new_detail.chapters) >= 2:
            # 多集源：停在选集态，不自动取流/播放，由用户点选集数
            self._selection_mode = True
            self._paint_card_selection()  # 高亮当前应选中的集（默认第 0 集）
            self._show_status("请选择要播放的集数")
            return
        self._load_episode(0)

    def current_episode_no(self) -> int:
        """当前集序号（0 基）；未加载/无分集（season 页）返回 0。

        供 ReaderPage 跨源换源前捕获，换源后在新源分集中保留同序号。
        """
        if 0 <= self._current_idx < len(self._episodes):
            return self._current_idx
        return 0

    def set_source_sid(self, sid: str) -> None:
        """外部设置当前播放源（ReaderPage 换源后回填）。"""
        self._current_sid = str(sid)
        self._switching = False
        for i in range(self.source_combo.count()):
            if self.source_combo.itemData(i) == str(sid):
                try:
                    self.source_combo.currentIndexChanged.disconnect(self._on_source_switch)
                except (RuntimeError, TypeError):
                    pass
                self.source_combo.setCurrentIndex(i)
                self.source_combo.currentIndexChanged.connect(self._on_source_switch)
                break
        self._refresh_source_menu()  # 播放源菜单打勾跟随

    def shutdown_video(self) -> None:
        """App 退出释放 VLC 播放器。"""
        if self._player is not None:
            self._player.close()
            self._player = None

    # ------------------------------------------------------------------ #
    def _stop_player(self) -> None:
        """停止当前视频播放并释放媒体/代理（保留播放器实例复用）。

        换视频/换源时调用，避免旧视频在后台继续播放占用网络/CPU。
        """
        if self._player is not None:
            self._player.release()
            self.play_btn.setText("▶")
        self._sync_overlay_state(playing=False)
        self.buffer_spinner.hide()
        self.control_bar.show()  # 停播后未播放状态 → 控制条常驻
        self._hide_timer.stop()

    def stop_playback(self) -> None:
        """离开视频视图时释放资源：停播放 + 清空播放缓存（不堆积）。

        换小说/漫画/换另一部视频前由 ReaderPage 调用；全屏状态先退出，
        播放缓存与取流预拉全部清空，下次进入按需重新拉取。
        """
        if self._fs_win is not None:
            self._exit_fullscreen()
        self._stop_player()
        self._current_play = ""
        self._detail_url_for_play = ""
        self._prefetch_idx = -2
        self._stream_cache.clear()
        self.progress.setValue(0)
        self.time_label.setText("00:00 / 00:00")
        self.play_label.hide()
        self.setCursor(Qt.ArrowCursor)

    # ------------------------------------------------------------------ #
    def _show_status(self, text: str) -> None:
        """状态浮层显示（加载/错误提示，覆盖在视频区内不占布局；成功后自动清空）。"""
        if text:
            self.play_label.setText(text)
            self.play_label.adjustSize()
            self._reposition_overlays()
            self.play_label.show()
        else:
            self.play_label.setText("")
            self.play_label.hide()

    def _populate_source_combo(self, detail: Detail) -> None:
        self._source_list = detail.source_list or []
        self._switching = False
        try:
            self.source_combo.currentIndexChanged.disconnect(self._on_source_switch)
        except (RuntimeError, TypeError):
            pass
        self.source_combo.clear()
        for s in self._source_list:
            name = s.get("name") or f"源{s.get('sid')}"
            self.source_combo.addItem(name, s.get("sid"))
        has = bool(self._source_list)
        self.source_label.setVisible(has)
        self.source_combo.setVisible(has)
        if has:
            cur_sid = self._current_sid
            idx = 0
            for i, s in enumerate(self._source_list):
                if s.get("sid") == cur_sid:
                    idx = i
                    break
            self.source_combo.setCurrentIndex(idx)
        self.source_combo.currentIndexChanged.connect(self._on_source_switch)
        self._refresh_source_menu()  # 播放器内换源菜单同步

    def _populate_quality_combo(self, source) -> None:
        """画质选项：读 api_endpoints.episode.quality.options，无则隐藏。"""
        self._quality_options = []
        try:
            qcfg = (source.raw.get("api_endpoints") or {}).get("episode", {}).get("quality", {})
            self._quality_options = list(qcfg.get("options") or [])
        except Exception:  # noqa: BLE001
            self._quality_options = []
        self._refresh_quality_menu()

    def _on_source_switch(self, idx: int) -> None:
        if self._switching or idx < 0 or not self._source_list:
            return
        sid = self.source_combo.itemData(idx)
        if not sid or sid == self._current_sid:
            return
        self._current_sid = str(sid)
        self._switching = True
        self.source_changed.emit((self._detail, str(sid)))
        self._video_frame.setFocus()  # 换源后快捷键继续可用

    def _on_quality_changed(self, text: str) -> None:
        if not text or text == self._quality or self._current_idx < 0:
            self._quality = text or "best"
            return
        self._quality = text
        # 清当前集播放缓存 → 新画质重拉
        ep = self._episodes[self._current_idx]
        key = (ep.url, text)
        self._stream_cache.pop(key, None)
        for act in self._quality_actions:
            act.setChecked(act.text() == text)
        self._load_episode(self._current_idx)

    # ------------------------------------------------------------------ #
    def _load_episode(self, idx: int) -> None:
        if self._source is None or not (0 <= idx < len(self._episodes)):
            return
        self._selection_mode = False  # 真实选集 → 退出选集态
        self._current_idx = idx
        self._paint_card_selection()  # 当前集卡片选中态跟随
        self._refresh_ep_menu()  # 选集菜单当前集打勾跟随
        ep = self._episodes[idx]
        self.episode_changed.emit((self._detail, ep.title, ep.url))  # 进度记忆
        key = (ep.url, self._quality)
        cached = self._stream_cache.get(key)
        if cached:
            video, audio = cached
            self._current_play = video
            self._play(video, audio, ep.title)
            self._prefetch_next(idx)
            return
        self._show_status(f"正在获取播放流：{ep.title}...")
        self._current_play = ""
        task = _FetchStreamTask(self._content, self._source, ep.url, self._quality)
        task.signals.finished.connect(self._on_stream_loaded)
        self._stream_task = task  # 防 GC
        QThreadPool.globalInstance().start(task)
        # 取流慢（yt-dlp 签名/慢站反爬）时，本集加载期间即预拉下一集——
        # 预拉是后台串行，本集完成后下一集大概率已缓存，连播/点下一集秒切。
        self._prefetch_next(idx)

    def _on_ep_card(self, card) -> None:
        """点分集卡片 → 选集加载。"""
        idx = card.p_data if isinstance(card.p_data, int) else -1
        if not (0 <= idx < len(self._episodes)):
            return
        self._select_episode(idx)

    def _on_prev_ep(self) -> None:
        """上一集（全屏/非全屏均可用）。"""
        idx = self._current_idx - 1
        if 0 <= idx < len(self._episodes):
            self._select_episode(idx)

    def _on_next_ep(self) -> None:
        """下一集（全屏/非全屏均可用）。"""
        idx = self._current_idx + 1
        if 0 <= idx < len(self._episodes):
            self._select_episode(idx)

    def _on_speed_changed(self, text: str) -> None:
        """倍速（0.5x~2.0x，VLC set_rate）。"""
        try:
            rate = float(text.rstrip("x"))
        except Exception:  # noqa: BLE001
            return
        if self._player is not None:
            self._player.set_rate(rate)

    def _is_current_stream(self, ep_url: str, quality: str) -> bool:
        """回调结果是否仍对应当前选中的集 + 画质。

        快速连点集/切画质时旧取流任务后到，其结果只应写缓存、
        不应覆盖当前播放（否则播错集）。无分集（season 页）用详情 URL 校验。
        """
        if self._current_idx < 0:
            return self._detail_url_for_play == ep_url and quality == self._quality
        if 0 <= self._current_idx < len(self._episodes):
            return (
                self._episodes[self._current_idx].url == ep_url
                and quality == self._quality
            )
        return False

    def _on_stream_loaded(self, ep_url, video, audio, err, quality) -> None:
        """取流完成：写缓存 →（仍为当前集/画质则）播放 → 预拉下一集。

        旧任务后到（ep_url/quality 已非当前）：只写缓存，不覆盖播放。
        缓存 key 用发起时 quality，避免切画质后 key 错位（B3）。
        """
        if self._source is None:
            return
        if err or not video:
            if self._is_current_stream(ep_url, quality):
                self._show_status(f"获取播放流失败：{err or '无播放地址'}")
                # 自动降级换源（多源站）
                nxt = self._next_available_sid()
                if nxt:
                    self.source_changed.emit((self._detail, nxt))
            return
        self._stream_cache[(ep_url, quality)] = (video, audio)
        if not self._is_current_stream(ep_url, quality):
            return  # 旧任务/旧画质后到：不覆盖当前播放（B2）
        self._current_play = video
        title = ""
        if 0 <= self._current_idx < len(self._episodes):
            title = self._episodes[self._current_idx].title or ""
        self._request_play(video, audio, title)
        self._prefetch_next(self._current_idx)

    # ------------------------------------------------------------------ #
    def _request_play(self, video: str, audio: str, title: str) -> None:
        """请求播放：视图可见立即播；不可见（用户在别的 Tab）暂存，显示后再播。

        取流完成时若用户已切走（视频页隐藏），主线程直接 _play 会在隐藏控件上
        做 VLC/winId 等操作 → 切页卡死。暂存后由 _VideoFrame.showEvent 在回到
        阅读页时补播（同时避免在别的页面后台出声）。
        """
        if self.isVisible():
            self._play(video, audio, title)
        else:
            self._pending_play = (video, audio, title)

    def _flush_pending_play(self) -> None:
        """视图重新可见时补播暂存的取流结果。"""
        if self._pending_play is not None:
            video, audio, title = self._pending_play
            self._pending_play = None
            self._play(video, audio, title)

    def _play(self, video: str, audio: str, title: str) -> None:
        """播放 = 调用外部播放器（VLC 桌面版优先，浏览器兜底）。

        外接方案：播放逻辑不再内嵌 libvlc（内嵌受 Qt 主线程/软解影响易卡），
        交给独立播放器进程渲染；续读由 VLC 桌面版自带"恢复播放位置"接管，
        分集/换源/画质/下载仍在 App 内操作，取流完成后打开播放器即播。
        """
        self._has_played = True
        self._current_play = video
        self._current_audio = audio  # DASH 音频轨（重开播放器时 input-slave 挂入）
        self._current_title = title  # 状态浮层/重开提示用
        hdrs = {}
        _rh = getattr(self._source, "request_headers", None)
        if callable(_rh):
            hdrs = _rh() or {}
        from framework.external_player import open_with_player

        # 源 ad_block 配置：播放代理转发 m3u8 时过滤广告段（下载已有过滤）
        ad_block = {}
        try:
            ad_block = (self._source.raw or {}).get("ad_block") or {}
        except Exception:  # noqa: BLE001
            pass
        msg = open_with_player(
            video, audio=audio,
            referer=hdrs.get("Referer", ""), user_agent=hdrs.get("User-Agent", ""),
            headers=hdrs, ad_block=ad_block,
        )
        self._show_status(f"{msg}：{title or video}")
        self.play_btn.setText("▶")
        self._sync_overlay_state(playing=False)
        self.control_bar.show()  # 外部播放器接管画面 → 控制条常驻

    def _retry_play(self) -> None:
        """刷新播放：清当前集取流缓存后重新取流播放（播放失败后的重试入口）。

        分集源重拉当前集；无分集（season 页）重拉详情 URL。
        """
        if self._source is None:
            return
        if 0 <= self._current_idx < len(self._episodes):
            ep = self._episodes[self._current_idx]
            key = (ep.url, self._quality)
            self._stream_cache.pop(key, None)
            self._current_play = ""
            self._show_status(f"正在重新获取播放流：{ep.title}...")
            self._load_episode(self._current_idx)
        elif self._detail_url_for_play:
            # season 页：直接重拉详情 URL 的播放流
            self._current_play = ""
            self._show_status("正在重新获取播放流...")
            task = _FetchStreamTask(
                self._content, self._source, self._detail_url_for_play, self._quality
            )
            task.signals.finished.connect(self._on_stream_loaded)
            self._stream_task = task
            QThreadPool.globalInstance().start(task)

    # ------------------------------------------------------------------ #
    def position_snapshot(self):
        """当前播放进度：(0~1 比例, None)。未播放/未就绪返回 0。"""
        if self._player is None:
            return 0.0, None
        return max(0.0, self._player.get_position()), None

    def current_context(self):
        """当前播放上下文 (detail, 集标题, 集URL)；未打开/未开始播放则 None。"""
        if self._detail is None or not self._has_played:
            return None  # 未真正播放不落盘，防覆盖恢复进度
        title, url = "", ""
        if 0 <= self._current_idx < len(self._episodes):
            ep = self._episodes[self._current_idx]
            title, url = ep.title, ep.url
        else:
            url = self._detail_url_for_play
        return (self._detail, title, url)

    def _emit_position(self) -> None:
        if self._detail is None or not self._has_played:
            return
        ctx = self.current_context()
        if ctx is None:
            return
        pos, _ = self.position_snapshot()
        try:
            self.position_changed.emit((ctx[0], ctx[1], ctx[2], pos, None))
        except RuntimeError:
            pass

    def _toggle_play_pause(self) -> None:
        """播放/暂停（外部播放器方案）：播放按钮 = 在外部播放器中打开当前集。

        选集态（多集源未选集）下无当前流 → 先按当前高亮集取流播放。
        暂停/进度/音量由外部播放器自身接管（VLC 桌面版）；这里再次点击
        重新拉起播放器播放当前集（VLC 单实例会复用已有窗口播同 URL）。
        """
        if not self._current_play:
            if self._selection_mode and 0 <= self._current_idx < len(self._episodes):
                self._load_episode(self._current_idx)
            return
        self._play(self._current_play, self._current_audio, self._current_title)

    def _toggle_fullscreen(self) -> None:
        if self._fs_win is not None:
            self._exit_fullscreen()
            return
        from PySide6.QtWidgets import QWidget, QVBoxLayout

        fs = QWidget()
        fs.setAttribute(Qt.WA_NativeWindow, True)
        fs.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        fs.setStyleSheet("background: #000;")
        # 鼠标唤出由全局事件过滤器兜底（fs 内任意位置移动/进入视频区即唤出）
        lay = QVBoxLayout(fs)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # 全屏顶栏：标题 + 退出按钮（沉浸增强，随控制条自动隐藏）
        self._fs_titlebar = QWidget(fs)
        # 顶栏开 mouseTracking：鼠标在顶栏移动 → 全局 eventFilter 唤出控制条
        self._fs_titlebar.setMouseTracking(True)
        tb = QHBoxLayout(self._fs_titlebar)
        tb.setContentsMargins(16, 10, 16, 10)
        fs_title = QLabel(self._fs_title_text())
        fs_title.setStyleSheet("color: white; font-size: 15px; font-weight: bold;")
        fs_title.setWordWrap(True)
        tb.addWidget(fs_title, stretch=1)
        fs_close = QPushButton("✕ 退出全屏")
        fs_close.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,40); color: white;"
            " border: none; border-radius: 6px; padding: 6px 14px; }"
            "QPushButton:hover { background: rgba(255,255,255,80); }"
        )
        fs_close.clicked.connect(self._exit_fullscreen)
        fs_close.clicked.connect(self._video_frame.setFocus)
        tb.addWidget(fs_close)
        lay.addWidget(self._fs_titlebar)
        # 视频区进全屏（覆盖层/沉浸控制条是其子控件，自动跟随）；
        # 全屏状态下也能操作，不再被主窗口盖住
        self._video_frame.setParent(fs)
        lay.addWidget(self._video_frame, stretch=1)
        self.fs_btn.setText("⛶")
        self._fs_win = fs
        # 退出途径：Esc / 双击 / 控制栏按钮 / 关闭窗口
        # （键盘/关闭/双击均在全局 eventFilter 里兜底——实例属性赋值在
        #   PySide6 不进 Qt 虚表永不回调，属无效代码，见 eventFilter 注释）
        fs.showFullScreen()
        self._reposition_overlays()
        if self._player is not None:
            self._player.rehook()
        self._wake_controls()

    def _fs_title_text(self) -> str:
        """全屏顶栏标题：作品名 + 当前集。"""
        if self._detail is not None:
            base = self._detail.title or ""
            ep_title = ""
            if 0 <= self._current_idx < len(self._episodes):
                ep_title = self._episodes[self._current_idx].title or ""
            if ep_title:
                return f"{base} · {ep_title}"
            return base
        return ""

    def _exit_fullscreen(self) -> None:
        if self._fs_win is None:
            return
        fs = self._fs_win
        self._fs_win = None
        # 视频区 reparent 回主视图（覆盖层/沉浸控制条是其子控件，自动跟随）
        self._video_frame.setParent(self)
        self._right_layout.insertWidget(0, self._video_frame)
        try:
            fs.close()
            fs.deleteLater()
        except Exception:  # noqa: BLE001
            pass
        if self._player is not None:
            self._player.rehook()
        self._reposition_overlays()
        self.setCursor(Qt.ArrowCursor)

    def _on_fs_close(self, event):
        """全屏窗被关闭（Alt+F4 等）→ 自动回主视图，视频不丢失。"""
        self._exit_fullscreen()
        event.accept()

    def _on_fs_key(self, event):
        """全屏窗键盘：Esc 退出全屏，其余转发快捷键。"""
        if event.key() == Qt.Key_Escape:
            self._exit_fullscreen()
            event.accept()
            return
        self._handle_key(event)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._reposition_overlays()
        self._reflow_cards()

    # ------------------------------------------------------------------ #
    def _prefetch_next(self, idx: int = -1) -> None:
        """后台预拉下一集播放流（串行，命中缓存/无下一集/正在预拉则跳过）。"""
        if self._source is None:
            return
        if idx < 0:
            idx = self._current_idx
        nxt = idx + 1
        if not (0 <= nxt < len(self._episodes)):
            return
        nxt_ep = self._episodes[nxt]
        key = (nxt_ep.url, self._quality)
        if key in self._stream_cache:
            return
        if self._prefetch_idx == nxt:
            return
        if self._prefetch_idx >= 0 and self._prefetch_idx != nxt:
            return  # 已有其他集在预拉（串行）
        self._prefetch_idx = nxt
        task = _FetchStreamTask(self._content, self._source, nxt_ep.url, self._quality)
        task.signals.finished.connect(self._on_prefetch_done)
        self._prefetch_task = task
        QThreadPool.globalInstance().start(task)

    def _on_prefetch_done(self, ep_url, video, audio, err, quality) -> None:
        self._prefetch_idx = -2
        if err or not video:
            return
        # 预拉缓存 key 用发起时 quality（与任务一致，防切画质错位）
        self._stream_cache[(ep_url, quality)] = (video, audio)

    def _next_available_sid(self) -> str:
        """返回当前源之后的第一个可用源 sid（无则空）。"""
        if not self._source_list or not self._current_sid:
            return ""
        found = False
        for s in self._source_list:
            if s.get("sid") == self._current_sid:
                found = True
                continue
            if found:
                return str(s.get("sid"))
        return ""

    # ------------------------------------------------------------------ #
    def _copy(self) -> None:
        if self._current_play:
            from PySide6.QtWidgets import QApplication

            QApplication.clipboard().setText(self._current_play)

    def _open_external(self) -> None:
        """「设置 → 外部播放器」：把当前集交外部播放器（VLC 桌面版优先）。

        Referer 保护的 CDN 直链（如 B 站 durl）：VLC 带 --http-referrer 播放
        媒体直链；VLC 不可用时回退浏览器打开集页面（页面播放绕开防盗链）。
        """
        if not self._current_play:
            return
        audio = getattr(self, "_current_audio", "")
        hdrs = {}
        _rh = getattr(self._source, "request_headers", None)
        if callable(_rh):
            hdrs = _rh() or {}
        from framework.external_player import _locate_vlc, open_with_player

        if not _locate_vlc():
            # 无 VLC：Referer 保护的媒体直链改开集页面（浏览器播放绕防盗链）
            page_url = ""
            if 0 <= self._current_idx < len(self._episodes):
                page_url = self._episodes[self._current_idx].url or ""
            elif self._detail_url_for_play:
                page_url = self._detail_url_for_play
            if page_url and self._media_needs_referer():
                from urllib.parse import urljoin

                webbrowser.open(urljoin(self._source.base_url, page_url))
                return
        msg = open_with_player(
            self._current_play, audio=audio,
            referer=hdrs.get("Referer", ""), user_agent=hdrs.get("User-Agent", ""),
            headers=hdrs,
        )
        self._show_status(msg)

    def _media_needs_referer(self) -> bool:
        """媒体直链是否被 Referer 保护：源配了 Referer 且媒体域名 ≠ 源站域名。"""
        try:
            from urllib.parse import urlparse

            hdrs = (
                self._source.request_headers()
                if callable(getattr(self._source, "request_headers", None))
                else {}
            )
            if not (hdrs or {}).get("Referer"):
                return False
            media_host = urlparse(self._current_play).netloc.lower()
            src_host = urlparse(self._source.base_url).netloc.lower()
            return media_host != src_host
        except Exception:  # noqa: BLE001
            return False


def _fmt_time(ms: int) -> str:
    """毫秒 → mm:ss。"""
    ms = max(0, int(ms or 0))
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"
