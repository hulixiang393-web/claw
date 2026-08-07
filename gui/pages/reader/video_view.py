"""视频阅读视图（video_view.py）—— VLC 内嵌播放器版。

- 分集列表，点击选看
- VLC 内嵌播放区（python-vlc set_hwnd），通用支持 HLS/DASH 双流/MP4
- 控制条：播放/暂停、进度、音量、全屏、画质选择
- 播放源换源、播放地址缓存、预拉下一集、播完自动续播
- 契约保持：load/reload_detail/set_source_sid/source_changed/episode_changed/play_label
"""

from __future__ import annotations

import webbrowser

from PySide6.QtCore import Qt, Signal, QThreadPool, QRunnable, QObject
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from framework.content import Content, Detail


class _VideoFrame(QWidget):
    """VLC 内嵌容器：showEvent 重挂 hwnd（切页/全屏后 HWND 变化）。"""

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self._view = view
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setStyleSheet("background: #000;")

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if self._view._player is not None:
            self._view._player.rehook()


class _VideoSignals(QObject):
    finished = Signal(object, object, object, object)  # (ep_url, video, audio, err)


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
            self.signals.finished.emit(self._url, video, audio, err)
        except RuntimeError:
            pass


class VideoView(QWidget):
    """视频分集 + VLC 内嵌播放视图（支持多播放源换源）。"""

    episode_changed = Signal(object)  # (detail, 集标题, 集URL) → 进度记忆
    source_changed = Signal(object)  # (detail, new_sid) → ReaderPage 换源

    def __init__(self, content: Content, parent=None):
        super().__init__(parent)
        self._content = content
        self._source = None
        self._detail: Detail | None = None
        self._episodes = []
        self._current_idx = -1
        self._current_play = ""  # 单流播放地址（展示/复制）
        self._stream_seq = 0  # 取流请求序号（防乱序覆盖）
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
        right.addWidget(self._video_frame, stretch=1)

        # 控制栏（全屏/非全屏共用同一 widget，reparent 切换）
        self.control_bar = QWidget()
        cb = QVBoxLayout(self.control_bar)
        cb.setContentsMargins(0, 0, 0, 0)
        cb.setSpacing(4)

        # 行1：播放 / 上集 / 下集 / 进度 / 时间 / 音量 / 倍速 / 全屏
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.play_btn = QPushButton("▶ 播放")
        self.play_btn.setFixedWidth(64)
        self.play_btn.clicked.connect(self._toggle_play_pause)
        row1.addWidget(self.play_btn)
        self.prev_btn = QPushButton("⏮ 上一集")
        self.prev_btn.clicked.connect(self._on_prev_ep)
        row1.addWidget(self.prev_btn)
        self.next_btn = QPushButton("下一集 ⏭")
        self.next_btn.clicked.connect(self._on_next_ep)
        row1.addWidget(self.next_btn)
        self.progress = QSlider(Qt.Horizontal)
        self.progress.setRange(0, 1000)
        self.progress.sliderPressed.connect(self._on_progress_pressed)
        self.progress.sliderReleased.connect(self._on_progress_released)
        row1.addWidget(self.progress, stretch=1)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet("color: palette(dark); font-size: 11px;")
        row1.addWidget(self.time_label)
        self.vol_btn = QLabel("🔊")
        row1.addWidget(self.vol_btn)
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(80)
        self.vol_slider.setFixedWidth(80)
        self.vol_slider.valueChanged.connect(self._on_volume)
        row1.addWidget(self.vol_slider)
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["1.0x", "0.5x", "0.75x", "1.25x", "1.5x", "2.0x"])
        self.speed_combo.setCurrentIndex(0)
        self.speed_combo.setFixedWidth(70)
        self.speed_combo.currentTextChanged.connect(self._on_speed_changed)
        row1.addWidget(self.speed_combo)
        self.fs_btn = QPushButton("⛶ 全屏")
        self.fs_btn.setFixedWidth(72)
        self.fs_btn.clicked.connect(self._toggle_fullscreen)
        row1.addWidget(self.fs_btn)
        cb.addLayout(row1)

        # 行2：画质 / 复制地址 / 外部打开
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.quality_label = QLabel("画质：")
        row2.addWidget(self.quality_label)
        self.quality_combo = QComboBox()
        self.quality_combo.currentTextChanged.connect(self._on_quality_changed)
        row2.addWidget(self.quality_combo)
        row2.addStretch(1)
        self.copy_btn = QPushButton("复制地址")
        self.copy_btn.clicked.connect(self._copy)
        row2.addWidget(self.copy_btn)
        self.open_btn = QPushButton("外部播放器")
        self.open_btn.clicked.connect(self._open_external)
        row2.addWidget(self.open_btn)
        cb.addLayout(row2)
        self.quality_label.setVisible(False)
        self.quality_combo.setVisible(False)
        self._control_row2 = row2  # 供画质显隐（全屏时也生效）

        right.addWidget(self.control_bar)

        body.addLayout(right, stretch=1)
        layout.addLayout(body, stretch=1)

        # 播放地址行
        self.play_label = QLabel("请选择一集")
        self.play_label.setWordWrap(True)
        self.play_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.play_label.setStyleSheet(
            "padding: 6px 8px; border: 1px solid palette(mid); border-radius: 6px;"
            " color: palette(mid); font-size: 11px;"
        )
        layout.addWidget(self.play_label)

    # ------------------------------------------------------------------ #
    def load(self, source, detail: Detail, start_ep_url: str = "") -> None:
        self._source = source
        self._detail = detail
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
        if not detail.chapters:
            # 无分集（season 页）→ 直接取详情页播放地址自动播放
            self.play_label.setText("正在获取播放流...")
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
        self.quality_combo.blockSignals(True)
        self.quality_combo.clear()
        self.quality_combo.addItems(self._quality_options)
        self.quality_combo.blockSignals(False)
        has = bool(self._quality_options)
        self.quality_label.setVisible(has)
        self.quality_combo.setVisible(has)

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
        self.play_label.setText(f"正在获取播放流：{ep.title}...")
        self._current_play = ""
        task = _FetchStreamTask(self._content, self._source, ep.url, self._quality)
        task.signals.finished.connect(self._on_stream_loaded)
        self._stream_task = task  # 防 GC
        QThreadPool.globalInstance().start(task)

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

    def _on_stream_loaded(self, ep_url, video, audio, err) -> None:
        """取流完成：写缓存 → 播放 → 预拉下一集。"""
        if self._source is None:
            return
        if err or not video:
            self.play_label.setText(f"获取播放流失败：{err or '无播放地址'}")
            # 自动降级换源（多源站）
            nxt = self._next_available_sid()
            if nxt:
                self.source_changed.emit((self._detail, nxt))
            return
        self._stream_cache[(ep_url, self._quality)] = (video, audio)
        self._current_play = video
        # 单流地址写展示缓存
        if not audio:
            self._play_cache[ep_url] = video
        self.play_label.setText(f"播放地址（已解密）：\n{video}")
        title = ""
        if 0 <= self._current_idx < len(self._episodes):
            title = self._episodes[self._current_idx].title or ""
        self._play(video, audio, title)
        self._prefetch_next(self._current_idx)

    # ------------------------------------------------------------------ #
    def _play(self, video: str, audio: str, title: str) -> None:
        """创建/复用 VLC 播放器并播放。"""
        if self._player is None:
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
        self._player.rehook()  # 确保 hwnd 已挂
        self._player.play(video, audio, title=title)
        self.play_btn.setText("⏸ 暂停")

    def _on_ended(self) -> None:
        """播完自动续播下一集（缓存命中秒切）。"""
        self.play_btn.setText("▶ 播放")
        nxt = self._current_idx + 1
        if 0 <= nxt < len(self._episodes):
            self.ep_list.setCurrentRow(nxt)
            self._load_episode(nxt)
        else:
            self.play_label.setText("播放完毕")

    def _on_play_error(self, msg: str) -> None:
        self.play_label.setText(f"{msg}（可复制地址用外部播放器）")
        self.play_btn.setText("▶ 播放")

    def _on_time_changed(self, ms: int) -> None:
        if self._dragging or not self.isVisible():
            return
        # 节流：VLC time_changed 约 250ms 一次，长视频（如 MissAV 1-2h）时
        # 信号 queued 到主线程若每次更新 UI 会堆积卡死；限 300ms 更新一次。
        import time

        now = time.monotonic()
        if now - self._last_tick < 0.3:
            return
        self._last_tick = now
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

    def _on_progress_pressed(self) -> None:
        self._dragging = True

    def _on_progress_released(self) -> None:
        self._dragging = False
        if self._player is not None:
            self._player.set_position(self.progress.value() / 1000.0)

    def _on_volume(self, v: int) -> None:
        if self._player is not None:
            self._player.set_volume(v)

    def _toggle_play_pause(self) -> None:
        if self._player is None:
            return
        if self._player.is_playing():
            self._player.pause()
            self.play_btn.setText("▶ 播放")
        else:
            self._player.resume()
            self.play_btn.setText("⏸ 暂停")

    def _toggle_fullscreen(self) -> None:
        if self._fs_win is not None:
            self._exit_fullscreen()
            return
        from PySide6.QtWidgets import QWidget, QVBoxLayout

        fs = QWidget()
        fs.setAttribute(Qt.WA_NativeWindow, True)
        fs.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        lay = QVBoxLayout(fs)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # 视频区 + 控制栏一起进全屏：控制条带退出/进度/上下集/倍速，
        # 全屏状态下也能操作，不再被主窗口盖住
        self._video_frame.setParent(fs)
        lay.addWidget(self._video_frame, stretch=1)
        self.control_bar.setParent(fs)
        lay.addWidget(self.control_bar)
        self.fs_btn.setText("⛶ 退出全屏")
        self._fs_win = fs
        # 退出途径：Esc / 双击 / 控制栏按钮 / 关闭窗口
        fs.keyPressEvent = self._on_fs_key
        fs.closeEvent = self._on_fs_close
        fs.mouseDoubleClickEvent = lambda e: self._exit_fullscreen()
        fs.showFullScreen()
        if self._player is not None:
            self._player.rehook()

    def _exit_fullscreen(self) -> None:
        if self._fs_win is None:
            return
        fs = self._fs_win
        self._fs_win = None
        # 视频区 + 控制栏 reparent 回主视图（关闭全屏窗后视频不丢失）
        self._video_frame.setParent(self)
        self._right_layout.insertWidget(0, self._video_frame)
        self.control_bar.setParent(self)
        self._right_layout.addWidget(self.control_bar)
        self.fs_btn.setText("⛶ 全屏")
        try:
            fs.close()
            fs.deleteLater()
        except Exception:  # noqa: BLE001
            pass
        if self._player is not None:
            self._player.rehook()

    def _on_fs_close(self, event):
        """全屏窗被关闭（Alt+F4 等）→ 自动回主视图，视频不丢失。"""
        self._exit_fullscreen()
        event.accept()

    def _on_fs_key(self, event):
        """全屏窗键盘：Esc 退出全屏。"""
        if event.key() == Qt.Key_Escape:
            self._exit_fullscreen()
            event.accept()
            return
        event.ignore()

    def _layout_right(self):
        # 定位右侧列布局（存引用更稳）
        return self._right_layout

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

    def _on_prefetch_done(self, ep_url, video, audio, err) -> None:
        self._prefetch_idx = -2
        if err or not video:
            return
        self._stream_cache[(ep_url, self._quality)] = (video, audio)
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
        if self._current_play:
            webbrowser.open(self._current_play)


def _fmt_time(ms: int) -> str:
    """毫秒 → mm:ss。"""
    ms = max(0, int(ms or 0))
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"
