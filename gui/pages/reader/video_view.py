"""视频阅读视图（video_view.py）—— VLC 内嵌播放器 + 现代化交互。

- 分集列表，点击选看
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

from PySide6.QtCore import Qt, Signal, QThreadPool, QRunnable, QObject, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from framework.content import Content, Detail


class _VideoFrame(QWidget):
    """VLC 内嵌容器：showEvent 重挂 hwnd（切页/全屏后 HWND 变化）。

    同时负责视频区交互：单击播放/暂停、双击全屏（300ms 判定区分）。
    """

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self._view = view
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setStyleSheet("background: #000;")

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if self._view._player is not None:
            self._view._player.rehook()
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
            QTimer.singleShot(300, self._click_timer)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self._view._click_pending = False
        if event.button() == Qt.LeftButton:
            self._view._toggle_fullscreen()
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


class VideoView(QWidget):
    """视频分集 + VLC 内嵌播放视图（现代化交互，支持多播放源换源）。"""

    episode_changed = Signal(object)  # (detail, 集标题, 集URL) → 进度记忆
    position_changed = Signal(object)  # (detail, 标题, URL, 播放进度 0~1, None) 续读
    source_changed = Signal(object)  # (detail, new_sid) → ReaderPage 换源

    # 快捷键帮助内容（? 键浮层）
    _HELP_TEXT = (
        "快捷键\n"
        "──────────────\n"
        "空格    播放 / 暂停\n"
        "← →     快退 / 快进 5 秒\n"
        "↑ ↓     音量 + / -\n"
        "M       静音切换\n"
        "F       全屏 / 退出全屏\n"
        "单击    播放 / 暂停\n"
        "双击    全屏切换\n"
        "?       显示 / 隐藏本帮助"
    )

    def __init__(self, content: Content, parent=None):
        super().__init__(parent)
        self._content = content
        self._source = None
        self._detail: Detail | None = None
        self._episodes = []
        self._current_idx = -1
        self._current_play = ""  # 单流播放地址（展示/复制）
        self._source_list = []  # [{sid, name, ...}]
        self._current_sid = ""
        self._switching = False
        self._play_cache: dict = {}  # {ep_url: single_url}（展示/复制地址）
        self._stream_cache: dict = {}  # {(ep_url, quality): (video, audio)} 播放用
        self._prefetch_idx = -2  # 正在预拉下一集（<0 空闲）
        self._quality = "best"
        self._quality_options: list = []
        self._detail_url_for_play = ""  # 无分集时记录详情 URL
        self._player = None
        self._dragging = False
        self._fs_win = None  # 全屏顶层窗口
        self._cached_length = 0  # 缓存视频总时长（length_changed 更新，避免每次 get_length 阻塞）
        self._last_tick = 0.0  # time_changed 节流时间戳（长视频防信号堆积卡主线程）
        self._last_pos_save_ts = 0.0  # 上次播放进度存盘时间戳（节流 2s 存一次）
        self._pending_position = None  # 打开书续读播放位置（0~1），播放开始后 seek
        self._has_played = False  # 是否真正开始过播放（未播放不落盘，防覆盖恢复进度）
        self._pending_play = None  # 取流完成但视图不可见 → 暂存 (video, audio, title)，显示后再播
        self._muted = False  # M 键静音状态（恢复音量用）
        self._last_volume = 80  # 静音前的音量
        self._click_pending = False  # 单击/双击判定
        self._hide_timer = QTimer(self)  # 控制条自动隐藏
        self._hide_timer.setInterval(3000)
        self._hide_timer.timeout.connect(self._hide_controls)

        self.setFocusPolicy(Qt.StrongFocus)  # 视图级快捷键
        self._build_ui()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

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

        # ---- 主体：分集列表 + 播放区 ----
        body = QHBoxLayout()
        body.setSpacing(8)
        self.ep_list = QListWidget()
        self.ep_list.setFixedWidth(200)
        self.ep_list.itemClicked.connect(self._on_ep_clicked)
        body.addWidget(self.ep_list)

        right = QVBoxLayout()
        right.setSpacing(6)
        self._right_layout = right  # 全屏退出后把视频区插回

        # VLC 内嵌播放区（固定比例 16:9 高度由容器撑）
        self._video_frame = _VideoFrame(self)
        self._video_frame.setMinimumHeight(220)
        self._video_frame.setFocusPolicy(Qt.StrongFocus)
        right.addWidget(self._video_frame, stretch=1)

        # ---- 覆盖层：中央播放按钮 / 缓冲 spinner / 帮助浮层 ----
        self._build_overlays()

        # ---- 控制条（自动隐藏）----
        self.control_bar = QWidget()
        self.control_bar.setObjectName("videoControlBar")
        self.control_bar.setStyleSheet(
            "QWidget#videoControlBar { background: palette(base);"
            " border: 1px solid palette(mid); border-radius: 8px; }"
        )
        cb = QHBoxLayout(self.control_bar)
        cb.setContentsMargins(8, 4, 8, 4)
        cb.setSpacing(6)
        self._build_control_bar(cb)
        right.addWidget(self.control_bar)

        body.addLayout(right, stretch=1)
        layout.addLayout(body, stretch=1)

        # 状态行（play_label 契约保留）：仅加载/错误时显示，成功后清空隐藏
        self.play_label = QLabel("")
        self.play_label.setWordWrap(True)
        self.play_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.play_label.setStyleSheet(
            "padding: 4px 8px; border: 1px solid palette(mid); border-radius: 6px;"
            " color: palette(mid); font-size: 11px;"
        )
        self.play_label.hide()
        layout.addWidget(self.play_label)

    def _build_overlays(self) -> None:
        """视频区覆盖层：中央播放钮 / 缓冲 spinner / 帮助浮层。"""
        frame = self._video_frame
        self._overlay_root = QWidget(frame)
        self._overlay_root.setGeometry(frame.rect())
        self._overlay_root.setAttribute(Qt.WA_TransparentForMouseEvents, False)

        # 中央播放按钮（未播放时显示，点击播放）
        self.center_play_btn = QPushButton("▶", self._overlay_root)
        self.center_play_btn.setFixedSize(72, 72)
        self.center_play_btn.setCursor(Qt.PointingHandCursor)
        self.center_play_btn.setToolTip("播放（单击）")
        self.center_play_btn.setStyleSheet(
            "QPushButton { background: rgba(0,0,0,150); color: white;"
            " border: 2px solid rgba(255,255,255,120); border-radius: 36px;"
            " font-size: 30px; padding: 0px; }"
            "QPushButton:hover { background: rgba(40,40,40,180); }"
        )
        self.center_play_btn.clicked.connect(self._toggle_play_pause)

        # 缓冲 spinner（播放启动到首帧期间显示）
        self.buffer_spinner = QProgressBar(self._overlay_root)
        self.buffer_spinner.setRange(0, 0)  # 不定模式（忙碌动画）
        self.buffer_spinner.setFixedSize(56, 56)
        self.buffer_spinner.setTextVisible(False)
        self.buffer_spinner.hide()

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
        """控制条图标按钮：覆写全局 QSS 的 8px 18px padding（窄按钮会挤掉文字）。"""
        b = QPushButton(text)
        b.setFixedWidth(width)
        b.setToolTip(tip)
        b.clicked.connect(slot)
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
        self.progress.sliderPressed.connect(self._on_progress_pressed)
        self.progress.sliderReleased.connect(self._on_progress_released)
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

        self.vol_btn = self._icon_btn("🔊", "静音（M）", self._toggle_mute, width=32)
        cb.addWidget(self.vol_btn)

        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(80)
        self.vol_slider.setFixedWidth(80)
        self.vol_slider.valueChanged.connect(self._on_volume)
        cb.addWidget(self.vol_slider)

        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["1.0x", "0.5x", "0.75x", "1.25x", "1.5x", "2.0x"])
        self.speed_combo.setCurrentIndex(0)
        self.speed_combo.setFixedWidth(64)
        self.speed_combo.setToolTip("播放倍速")
        self.speed_combo.currentTextChanged.connect(self._on_speed_changed)
        cb.addWidget(self.speed_combo)

        # 设置菜单（⚙）：画质 + 复制地址 + 刷新 + 外部播放器 + 帮助
        self.settings_btn = QToolButton()
        self.settings_btn.setText("⚙")
        self.settings_btn.setPopupMode(QToolButton.InstantPopup)
        self.settings_btn.setToolTip("设置")
        self.settings_btn.setFixedWidth(32)
        self._settings_menu = QMenu(self)
        self._build_settings_menu()
        self.settings_btn.setMenu(self._settings_menu)
        cb.addWidget(self.settings_btn)

        self.fs_btn = QPushButton("⛶")
        self.fs_btn.setFixedWidth(32)
        self.fs_btn.setToolTip("全屏（F）")
        self.fs_btn.clicked.connect(self._toggle_fullscreen)
        self.fs_btn.setStyleSheet("padding: 0px;")
        cb.addWidget(self.fs_btn)

    def _build_settings_menu(self) -> None:
        menu = self._settings_menu
        menu.clear()

        # 画质子菜单（源无画质选项时隐藏）
        self.quality_label = QLabel("画质：")  # 兼容旧引用（不再显示于控制条）
        self.quality_combo = QComboBox()  # 兼容旧引用（选择在菜单内）
        self.quality_menu = menu.addMenu("画质")

        menu.addSeparator()
        menu.addAction("📋 复制播放地址").triggered.connect(self._copy)
        menu.addAction("🔄 刷新播放").triggered.connect(self._retry_play)
        menu.addAction("↗ 外部播放器").triggered.connect(self._open_external)
        menu.addSeparator()
        menu.addAction("? 快捷键帮助").triggered.connect(self._toggle_help)

        self._quality_actions = []  # 防止菜单项被 GC
        self._refresh_quality_menu()

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

    # ------------------------------------------------------------------ #
    def _toggle_mute(self) -> None:
        """M 键/喇叭按钮：静音切换（记住静音前音量用于恢复）。"""
        if self._player is None:
            return
        if self._muted:
            self._player.set_volume(self._last_volume)
            self.vol_slider.setValue(self._last_volume)
            self.vol_btn.setText("🔊")
            self._muted = False
        else:
            self._last_volume = self.vol_slider.value()
            self._player.set_volume(0)
            self.vol_btn.setText("🔇")
            self._muted = True

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
        """3s 无操作 → 隐藏控制条（仅播放中隐藏；拖动/菜单打开时除外）。"""
        if self._dragging or self.settings_btn.menu().isVisible():
            self._hide_timer.start()
            return
        if self._player is not None and self._player.is_playing():
            self.control_bar.hide()
            if self._fs_win is not None:
                self._fs_titlebar.hide()
            self.setCursor(Qt.BlankCursor)

    def _handle_key(self, event) -> None:
        """键盘快捷键（焦点在视图/视频区时）。"""
        key = event.key()
        if key == Qt.Key_Space:
            self._toggle_play_pause()
            event.accept()
            return
        if key == Qt.Key_F:
            self._toggle_fullscreen()
            event.accept()
            return
        if key == Qt.Key_M:
            self._toggle_mute()
            event.accept()
            return
        if key == Qt.Key_Question or key == Qt.Key_Slash:
            self._toggle_help()
            event.accept()
            return
        if key == Qt.Key_Left:
            self._seek_relative(-5)
            event.accept()
            return
        if key == Qt.Key_Right:
            self._seek_relative(5)
            event.accept()
            return
        if key == Qt.Key_Up:
            self._set_volume(min(100, self.vol_slider.value() + 10))
            event.accept()
            return
        if key == Qt.Key_Down:
            self._set_volume(max(0, self.vol_slider.value() - 10))
            event.accept()
            return
        event.ignore()

    def keyPressEvent(self, event):  # noqa: N802
        self._handle_key(event)

    def _set_volume(self, v: int) -> None:
        self.vol_slider.setValue(v)
        self._on_volume(v)
        if self._muted and v > 0:
            self._muted = False
            self.vol_btn.setText("🔊")

    def _seek_relative(self, seconds: float) -> None:
        if self._player is not None:
            self._player.seek_relative(seconds)
            self._wake_controls()

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
        self.center_play_btn.move((w - 72) // 2, (h - 72) // 2)
        self.buffer_spinner.move((w - 56) // 2, (h - 56) // 2)
        self.help_overlay.adjustSize()
        self.help_overlay.move((w - self.help_overlay.width()) // 2,
                               (h - self.help_overlay.height()) // 2)

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
    def load(self, source, detail: Detail, start_ep_url: str = "", restore_position: float | None = None) -> None:
        self._source = source
        self._detail = detail
        self._pending_play = None  # 换书清掉旧暂存播放
        if restore_position is not None:
            self._pending_position = restore_position
        # 换视频先停旧播放（不堆积缓存/后台占用），再预建播放器。
        self._stop_player()
        # 【播放加速】详情打开即预建 VLC 播放器：取流完成直接 play，
        # 不再现场 import vlc + 建 MediaPlayer（约几百 ms，首播更跟手）。
        self._ensure_player()
        self._episodes = detail.chapters
        self._play_cache.clear()
        self._stream_cache.clear()
        self._prefetch_idx = -2
        self._populate_source_combo(detail)
        self._populate_quality_combo(source)
        self.ep_list.clear()
        for i, ep in enumerate(detail.chapters):
            item = QListWidgetItem(ep.title or f"第{i+1}集")
            item.setData(Qt.UserRole, i)
            self.ep_list.addItem(item)
        idx = 0
        if start_ep_url:
            for i, ep in enumerate(detail.chapters):
                if ep.url == start_ep_url:
                    idx = i
                    break
        self.ep_list.setCurrentRow(idx)
        self._sync_overlay_state(playing=False)
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
        self._load_episode(idx)

    def reload_detail(self, new_detail: Detail) -> None:
        """换源重载分集后调用：重建列表 + 加载第 0 集（不自动播）。"""
        self._stop_player()  # 换源先停旧播放流
        self._detail = new_detail
        self._episodes = new_detail.chapters
        self._play_cache.clear()
        self._stream_cache.clear()
        self._prefetch_idx = -2
        self._detail_url_for_play = ""
        self.ep_list.clear()
        for i, ep in enumerate(new_detail.chapters):
            item = QListWidgetItem(ep.title or f"第{i+1}集")
            item.setData(Qt.UserRole, i)
            self.ep_list.addItem(item)
        self.ep_list.setCurrentRow(0)
        self._switching = False
        self._sync_overlay_state(playing=False)
        if new_detail.chapters:
            self._load_episode(0)

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
        self._play_cache.clear()
        self._stream_cache.clear()
        self._cached_length = 0
        self.progress.setValue(0)
        self.time_label.setText("00:00 / 00:00")
        self.play_label.hide()
        self.setCursor(Qt.ArrowCursor)

    # ------------------------------------------------------------------ #
    def _show_status(self, text: str) -> None:
        """状态行显示（加载/错误提示；播放成功后自动清空）。"""
        if text:
            self.play_label.setText(text)
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
        self._current_idx = idx
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

    def _on_ep_clicked(self, item) -> None:
        idx = item.data(Qt.UserRole)
        self._load_episode(idx)

    def _on_prev_ep(self) -> None:
        """上一集（全屏/非全屏均可用）。"""
        idx = self._current_idx - 1
        if 0 <= idx < len(self._episodes):
            self.ep_list.setCurrentRow(idx)
            self._load_episode(idx)

    def _on_next_ep(self) -> None:
        """下一集（全屏/非全屏均可用）。"""
        idx = self._current_idx + 1
        if 0 <= idx < len(self._episodes):
            self.ep_list.setCurrentRow(idx)
            self._load_episode(idx)

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
        # 单流地址写展示缓存
        if not audio:
            self._play_cache[ep_url] = video
        if not self._is_current_stream(ep_url, quality):
            return  # 旧任务/旧画质后到：不覆盖当前播放（B2）
        self._current_play = video
        title = ""
        if 0 <= self._current_idx < len(self._episodes):
            title = self._episodes[self._current_idx].title or ""
        self._request_play(video, audio, title)
        self._prefetch_next(self._current_idx)

    # ------------------------------------------------------------------ #
    def _ensure_player(self) -> None:
        """预建/复用 VLC 播放器（构造 MediaPlayer 是首播主要耗时之一）。

        详情 load 即调用 → 取流完成直接 play，免现场构造。
        构造失败（缺 vlc / 环境异常）保持 _player=None，_play 时仍会重试并报错，
        不阻断详情加载。
        """
        if self._player is not None:
            return
        try:
            from framework.vlc_player import VlcPlayer

            referer = ""
            ua = ""
            _rh = getattr(self._source, "request_headers", None)
            if callable(_rh):
                hdrs = _rh() or {}
                referer = hdrs.get("Referer", "") or ""
                ua = hdrs.get("User-Agent", "") or ""
            self._player = VlcPlayer(self._video_frame, referer=referer, user_agent=ua)
            self._player.ended.connect(self._on_ended)
            self._player.error.connect(self._on_play_error)
            self._player.time_changed.connect(self._on_time_changed)
            self._player.length_changed.connect(self._on_length_changed)
        except Exception:  # noqa: BLE001 —— VLC 不可用时降级：播放时再试并报错
            self._player = None

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
        """创建/复用 VLC 播放器并播放。

        播放器构造失败（未装 VLC / 环境异常）时 _player 为 None：
        不再调用 rehook 崩溃，提示用户走外部播放器（B1）。
        """
        self._ensure_player()
        if self._player is None:
            self._show_status(
                "播放器不可用：未安装 VLC 或加载失败，可用「设置 → 外部播放器」打开"
            )
            self.play_btn.setText("▶")
            self._sync_overlay_state(playing=False)
            return
        self._player.rehook()  # 确保 hwnd 已挂
        self._player.play(video, audio, title=title)
        self._has_played = True
        self.play_btn.setText("⏸")
        self._sync_overlay_state(playing=True)
        self._show_status("")  # 播放开始 → 清空状态行
        self.buffer_spinner.show()  # 缓冲指示：首帧（time_changed）后隐藏
        self._hide_timer.start()  # 开始自动隐藏计时
        # 续读定位：播放开始后等缓冲就绪再 seek 到上次进度（VLC 缓冲前 set_position 无效）
        if self._pending_position is not None:
            pos = self._pending_position
            self._pending_position = None
            if pos > 0:
                self._seek_with_retry(pos)

    def _on_ended(self) -> None:
        """播完自动续播下一集（缓存命中秒切）。"""
        self.play_btn.setText("▶")
        self._sync_overlay_state(playing=False)
        self.buffer_spinner.hide()
        nxt = self._current_idx + 1
        if 0 <= nxt < len(self._episodes):
            self.ep_list.setCurrentRow(nxt)
            self._load_episode(nxt)
        else:
            self._show_status("播放完毕")

    def _on_play_error(self, msg: str) -> None:
        # VLC 可能在播放成功期间发瞬时 Error 事件（demux 警告等），画面仍在
        # 播时不能覆盖标签（否则「VLC 播放出错」一直挂在屏幕）。只在确实没在
        # 播时才显示错误提示。
        if self._player is not None and self._player.is_playing():
            return
        self._show_status(f"{msg}（可复制地址用外部播放器）")
        self.play_btn.setText("▶")
        self._sync_overlay_state(playing=False)
        self.buffer_spinner.hide()

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
            self._play_cache.pop(ep.url, None)
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

    def _on_time_changed(self, ms: int) -> None:
        if self._dragging or not self.isVisible():
            return
        # 首帧到达 → 缓冲 spinner 隐藏
        if self.buffer_spinner.isVisible():
            self.buffer_spinner.hide()
        # 节流：VLC time_changed 约 250ms 一次，长视频（如 MissAV 1-2h）时
        # 信号 queued 到主线程若每次更新 UI 会堆积卡死；限 300ms 更新一次。
        import time

        now = time.monotonic()
        if now - self._last_tick < 0.3:
            return
        self._last_tick = now
        # 播放进度存盘：节流 2s 一次（视频恢复精准到秒）
        if now - self._last_pos_save_ts >= 2.0:
            self._last_pos_save_ts = now
            self._emit_position()
        # 用缓存时长，不在每次回调里 get_length()（网络流该调用会阻塞）
        length = self._cached_length
        if length > 0:
            self.progress.setValue(int(ms / length * 1000))
            self.time_label.setText(f"{_fmt_time(ms)} / {_fmt_time(length)}")
        else:
            self.progress.setValue(0)
            self.time_label.setText(f"{_fmt_time(ms)} / --:--")

    def _on_length_changed(self, ms: int) -> None:
        if ms > 0:
            self._cached_length = ms
            self.time_label.setText(
                f"{_fmt_time(self._player.get_time() if self._player else 0)} / {_fmt_time(ms)}"
            )

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

    def _seek_with_retry(self, pos: float, tries: int = 5) -> None:
        """播放开始后等缓冲就绪再 seek 到上次进度。

        VLC 缓冲完成前 set_position 无效；每 900ms 重试一次直到总时长
        已知（最多 5 次），此时再跳转才能精准落在上次播放位置。
        """
        if tries <= 0 or self._player is None:
            return
        if self._player.get_length() > 0:
            self._player.set_position(pos)
        else:
            QTimer.singleShot(900, lambda: self._seek_with_retry(pos, tries - 1))

    def _on_progress_pressed(self) -> None:
        self._dragging = True
        self._hide_timer.stop()  # 拖动期间禁止自动隐藏

    def _on_progress_released(self) -> None:
        self._dragging = False
        if self._player is not None:
            pos = self.progress.value() / 1000.0
            # VLC set_position 在网络流上会同步阻塞（等新位置缓冲）→ 主线程卡死。
            # 挪到后台线程执行，拖拽进度条不冻结 UI。
            import threading

            threading.Thread(
                target=lambda: self._player.set_position(pos), daemon=True
            ).start()
        self._hide_timer.start()  # 拖动结束恢复自动隐藏计时

    def _on_volume(self, v: int) -> None:
        if self._player is not None:
            self._player.set_volume(v)

    def _toggle_play_pause(self) -> None:
        if self._player is None:
            return
        if self._player.is_playing():
            self._player.pause()
            self.play_btn.setText("▶")
            self._sync_overlay_state(playing=False)
            self._emit_position()  # 暂停瞬间精确存盘当前进度
            self._hide_timer.stop()  # 暂停时控制条常驻
        else:
            self._player.resume()
            self.play_btn.setText("⏸")
            self._sync_overlay_state(playing=True)
            self._hide_timer.start()

    def _toggle_fullscreen(self) -> None:
        if self._fs_win is not None:
            self._exit_fullscreen()
            return
        from PySide6.QtWidgets import QWidget, QVBoxLayout

        fs = QWidget()
        fs.setAttribute(Qt.WA_NativeWindow, True)
        fs.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        fs.setStyleSheet("background: #000;")
        lay = QVBoxLayout(fs)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # 全屏顶栏：标题 + 退出按钮（沉浸增强，随控制条自动隐藏）
        self._fs_titlebar = QWidget(fs)
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
        tb.addWidget(fs_close)
        lay.addWidget(self._fs_titlebar)
        # 视频区 + 控制条一起进全屏：控制条带退出/进度/上下集/倍速，
        # 全屏状态下也能操作，不再被主窗口盖住
        self._video_frame.setParent(fs)
        lay.addWidget(self._video_frame, stretch=1)
        self.control_bar.setParent(fs)
        lay.addWidget(self.control_bar)
        self.fs_btn.setText("⛶")
        self._fs_win = fs
        # 退出途径：Esc / 双击 / 控制栏按钮 / 关闭窗口
        fs.keyPressEvent = self._on_fs_key
        fs.closeEvent = self._on_fs_close
        fs.mouseDoubleClickEvent = lambda e: self._exit_fullscreen()
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
        # 视频区 + 控制条 reparent 回主视图（关闭全屏窗后视频不丢失）
        self._video_frame.setParent(self)
        self._right_layout.insertWidget(0, self._video_frame)
        self.control_bar.setParent(self)
        self._right_layout.addWidget(self.control_bar)
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
        if not audio:
            self._play_cache[ep_url] = video

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
        if not self._current_play:
            return
        # Referer 保护的 CDN 直链（如 B 站 durl）：裸 URL 在浏览器直接打开会 403
        # （CDN 校验 Referer）。检测到「媒体域名 ≠ 源站域名 且 源配了 Referer」时，
        # 改为打开该集页面 URL，让浏览器正常播放；否则打开原始媒体地址。
        page_url = ""
        if 0 <= self._current_idx < len(self._episodes):
            page_url = self._episodes[self._current_idx].url or ""
        elif self._detail_url_for_play:
            page_url = self._detail_url_for_play
        if page_url and self._media_needs_referer():
            from urllib.parse import urljoin

            webbrowser.open(urljoin(self._source.base_url, page_url))
            return
        webbrowser.open(self._current_play)

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
