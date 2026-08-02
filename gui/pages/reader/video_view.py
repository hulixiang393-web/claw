"""视频阅读视图（VideoView）。

对应 ui-reader.md 视频功能点：
- 分集列表，点击选看
- 播放地址提取（解密后展示）
- 复制地址 / 外部播放器打开（方案A，主）
- 在应用内观看（方案B，增强，QWebEngineView 可选）
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
        self._load_episode(idx)

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
        self.episode_changed.emit((self._detail, ep.title))

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

    def _open_embed(self) -> None:
        """在应用内观看（方案B 增强）：尝试用源播放页 URL。"""
        if self._detail and self._current_idx >= 0:
            ep = self._episodes[self._current_idx]
            # 尝试加载源详情页（站点自带播放器）
            try:
                from PySide6.QtWebEngineWidgets import QWebEngineView
                from PySide6.QtCore import QUrl

                self._embed_view = QWebEngineView(self)
                self._embed_view.setUrl(QUrl(ep.url))
                self._embed_view.show()
            except ImportError:
                self.play_label.setText("内嵌播放需要 QtWebEngine，未安装。可用外部播放器打开。")


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
