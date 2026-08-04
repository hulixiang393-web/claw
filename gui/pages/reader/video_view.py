"""视频阅读视图（VideoView）。

对应 ui-reader.md 视频功能点：
- 分集列表，点击选看
- 播放地址提取（解密后展示）
- 在应用内观看：调 mpv 独立窗口实时播 dash 双流（B 站音视频分离）
- 复制地址 / 外部播放器打开
"""

from __future__ import annotations

import webbrowser

from PySide6.QtCore import Qt, Signal, QThreadPool, QRunnable, QObject
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from framework.content import Content, Detail


class VideoView(QWidget):
    """视频分集 + 播放地址视图。"""

    episode_changed = Signal(object)

    def __init__(self, content: Content, parent=None):
        super().__init__(parent)
        self._content = content
        self._source = None
        self._detail: Detail | None = None
        self._episodes = []
        self._current_idx = -1
        self._current_play = ""
        self._player = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # ---- 分集列表 ----
        self.ep_list = QListWidget()
        self.ep_list.itemClicked.connect(self._on_ep_clicked)
        layout.addWidget(self.ep_list, stretch=1)

        # ---- 播放地址区 ----
        self.play_label = QLabel("请选择一集")
        self.play_label.setWordWrap(True)
        self.play_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.play_label.setStyleSheet("padding: 8px; border: 1px solid palette(mid); border-radius: 6px;")
        layout.addWidget(self.play_label)

        # ---- 操作按钮 ----
        btns = QHBoxLayout()
        self.copy_btn = QPushButton("复制地址")
        self.open_btn = QPushButton("外部播放器打开")
        self.embed_btn = QPushButton("在应用内观看")
        self.copy_btn.clicked.connect(self._copy)
        self.open_btn.clicked.connect(self._open_external)
        self.embed_btn.clicked.connect(self._open_embed)
        btns.addWidget(self.copy_btn)
        btns.addWidget(self.open_btn)
        btns.addWidget(self.embed_btn)
        layout.addLayout(btns)

    # ------------------------------------------------------------------ #
    def load(self, source, detail: Detail, start_ep_url: str = "") -> None:
        self._source = source
        self._detail = detail
        self._episodes = detail.chapters
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
            # 无分集列表（如 B站番剧 season 页）→ 直接播放详情页
            self.play_label.setText("正在加载播放页...")
            self._open_embed(detail.url)
            return
        self._load_episode(idx)
        # 进入即自动播放当前集（mpv 独立窗口）
        self._open_embed(self._episodes[idx].url)

    def _load_episode(self, idx: int) -> None:
        if self._source is None or not (0 <= idx < len(self._episodes)):
            return
        self._current_idx = idx
        ep = self._episodes[idx]
        self.play_label.setText(f"正在获取播放地址：{ep.title}...")
        self._current_play = ""
        # 后台获取播放地址
        from PySide6.QtCore import QThreadPool

        task = _LoadVideoTask(self._content, self._source, ep)
        task.signals.finished.connect(self._on_play_loaded)
        self._video_task = task  # 持有引用，防止被 GC
        QThreadPool.globalInstance().start(task)
        self.episode_changed.emit((self._detail, ep.title, ep.url))

    def _on_ep_clicked(self, item) -> None:
        idx = item.data(Qt.UserRole)
        self._load_episode(idx)

    def _on_play_loaded(self, ep, play, err) -> None:
        if err:
            self.play_label.setText(f"获取失败：{err}")
            return
        self._current_play = play
        self.play_label.setText(f"播放地址（已解密）：\n{play}")

    # ------------------------------------------------------------------ #
    def _copy(self) -> None:
        if self._current_play:
            QApplication.clipboard().setText(self._current_play)

    def _open_external(self) -> None:
        if self._current_play:
            webbrowser.open(self._current_play)

    def _open_embed(self, url: str | None = None) -> None:
        """在应用内观看：调 mpv 独立窗口播放（B 站 dash 音视频双流）。"""
        if self._source is None:
            self.play_label.setText("尚未加载作品")
            return
        # 当前分集 URL
        target = url
        if target is None and self._current_idx >= 0 and self._episodes:
            target = self._episodes[self._current_idx].url
        if target is None and self._detail:
            target = self._detail.url
        if not target:
            self.play_label.setText("无可用播放地址")
            return

        self.play_label.setText("正在获取播放流...")
        # 后台取 dash 双流，再拉起 mpv（避免网络阻塞 UI）
        task = _LoadStreamTask(self._content, self._source, target)
        task.signals.finished.connect(self._on_stream_loaded)
        self._stream_task = task  # 持有引用防 GC
        QThreadPool.globalInstance().start(task)

    def _on_stream_loaded(self, video, audio, title, err) -> None:
        if err or not video:
            self.play_label.setText(f"获取播放流失败：{err or '无播放地址'}")
            return
        self._current_play = video
        self.play_label.setText(f"正在打开 mpv 播放：{title}")
        # 拉起 mpv（懒创建，复用实例）
        if self._player is None:
            from framework.mpv_player import MpvPlayer

            self._player = MpvPlayer()
        self._player.play(video, audio, title=title)


class _VideoSignals(QObject):
    """视频加载信号。"""
    finished = Signal(object, object, object)  # (episode, play, err)


class _LoadVideoTask(QRunnable):
    """后台获取视频播放地址。"""

    def __init__(self, content, source, episode):
        super().__init__()
        self.signals = _VideoSignals()
        self._content = content
        self._source = source
        self._episode = episode

    def run(self) -> None:
        play, err = "", None
        try:
            play = self._content.fetch_video_episode(self._source, self._episode.url)
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(self._episode, play, err)
        except RuntimeError:
            pass


class _StreamSignals(QObject):
    """双流播放信号。"""
    finished = Signal(object, object, object, object)  # (video, audio, title, err)


class _LoadStreamTask(QRunnable):
    """后台获取 dash 双流（视频轨+音频轨）供 mpv 播放。"""

    def __init__(self, content, source, episode_url, title=""):
        super().__init__()
        self.signals = _StreamSignals()
        self._content = content
        self._source = source
        self._url = episode_url
        self._title = title

    def run(self) -> None:
        video, audio, err = "", "", None
        try:
            video, audio = self._content.fetch_video_streams(self._source, self._url)
        except Exception as exc:
            err = str(exc)
        try:
            self.signals.finished.emit(video, audio, self._title, err)
        except RuntimeError:
            pass
