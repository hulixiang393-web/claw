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
    QComboBox,
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
    """视频分集 + 播放地址视图（支持多播放源换源）。"""

    episode_changed = Signal(object)
    source_changed = Signal(object)  # (detail, new_sid) → ReaderPage 重载分集

    def __init__(self, content: Content, parent=None):
        super().__init__(parent)
        self._content = content
        self._source = None
        self._detail: Detail | None = None
        self._episodes = []
        self._current_idx = -1
        self._current_play = ""
        self._player = None
        self._stream_seq = 0  # 播放流请求序号（防并发任务回调乱序）
        self._source_list = []       # [{sid, name, from_, ps, parse}]
        self._current_sid = ""       # 当前选中播放源 sid
        self._switching = False      # 换源进行中锁（防止重复触发）
        self._play_cache: dict = {}  # 播放地址缓存 {episode_url: play}（预加载/命中秒开）
        self._prefetch_idx = -2      # 正在预拉下一集的索引（<0 空闲）
        self._detail_url_for_play = ""  # 无分集时记录详情 URL（供自动播放）

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
        # 默认隐藏，load() 时按 source_list 决定
        self.source_label.setVisible(False)
        self.source_combo.setVisible(False)

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
        self._play_cache.clear()  # 打开新作品 → 清空旧播放地址缓存
        self._prefetch_idx = -2
        self._populate_source_combo(detail)
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
            # 无分集列表（如 B站番剧 season 页）→ 直接取详情页播放地址，自动播放
            self.play_label.setText("正在获取播放地址...")
            self._current_idx = -1
            self._detail_url_for_play = detail.url  # 供 _on_play_loaded 自动播放
            from PySide6.QtCore import QThreadPool

            task = _LoadVideoTask(self._content, self._source, None, detail_url=detail.url)
            task.signals.finished.connect(self._on_play_loaded)
            self._video_task = task
            QThreadPool.globalInstance().start(task)
            return
        self._load_episode(idx)

    def _populate_source_combo(self, detail: Detail) -> None:
        """填充播放源下拉框；无换源配置则隐藏。"""
        self._source_list = detail.source_list or []
        self._switching = False
        # 断开信号防止填充时误触发
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
            # 默认选第一个源（或当前详情 URL 里 sid 对应的）
            cur_sid = self._current_sid
            idx = 0
            for i, s in enumerate(self._source_list):
                if s.get("sid") == cur_sid:
                    idx = i
                    break
            self.source_combo.setCurrentIndex(idx)
        self.source_combo.currentIndexChanged.connect(self._on_source_switch)

    def _on_source_switch(self, idx: int) -> None:
        """下拉框切换播放源 → 通知 ReaderPage 重新抓分集。"""
        if self._switching or idx < 0 or not self._source_list:
            return
        sid = self.source_combo.itemData(idx)
        if not sid or sid == self._current_sid:
            return
        self._current_sid = str(sid)
        self._switching = True
        self.source_changed.emit((self._detail, str(sid)))

    def set_source_sid(self, sid: str) -> None:
        """外部设置当前播放源（ReaderPage 重载分集后回填）。"""
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

    def _load_episode(self, idx: int) -> None:
        if self._source is None or not (0 <= idx < len(self._episodes)):
            return
        self._current_idx = idx
        ep = self._episodes[idx]
        # 已预加载播放地址 → 直接显示并自动播放，秒开不白屏
        cached = self._play_cache.get(ep.url)
        if cached:
            self._current_play = cached
            self.play_label.setText(f"播放地址（已解密）：\n{cached}")
            self.episode_changed.emit((self._detail, ep.title, ep.url))
            self._prefetch_next(idx)
            self._open_embed(ep.url)  # 自动播放
            return
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
            # 自动降级：当前源失败 → 尝试下一个可用源（仅在多源时）
            nxt = self._next_available_sid()
            if nxt:
                self.play_label.setText(f"当前源不可用，尝试切换到源 {nxt}...")
                self.source_changed.emit((self._detail, nxt))
                return
            self.play_label.setText(f"获取失败：{err}")
            return
        self._current_play = play
        if ep is not None and getattr(ep, "url", ""):
            self._play_cache[ep.url] = play  # 写缓存，切集命中秒开
            # 预加载下一集播放地址（切集不白屏）
            self._prefetch_next(self._current_idx)
            # 自动播放当前集（地址已解密 → 取流 → mpv）
            self._open_embed(ep.url)
        else:
            # 无分集（详情页单视频）→ 用详情 URL 自动播放
            detail_url = getattr(self, "_detail_url_for_play", "") or (self._detail.url if self._detail else "")
            if detail_url:
                self._open_embed(detail_url)

    # ------------------------------------------------------------------ #
    def _prefetch_next(self, idx: int = -1) -> None:
        """后台预拉下一集播放地址（fetch_video_episode 轻量，串行）。

        当前集加载完成后触发；命中缓存/无下一集/正在预拉 → 跳过。
        预拉结果写 _play_cache，_load_episode 命中秒开，切换不白屏。
        """
        if self._source is None:
            return
        if idx < 0:
            idx = self._current_idx
        nxt = idx + 1
        if not (0 <= nxt < len(self._episodes)):
            return
        nxt_ep = self._episodes[nxt]
        if nxt_ep.url in self._play_cache:
            return  # 已缓存
        if self._prefetch_idx == nxt:
            return  # 正在预拉该集
        if self._prefetch_idx >= 0 and self._prefetch_idx != nxt:
            return  # 已有其他集在预拉（串行）
        self._prefetch_idx = nxt
        from PySide6.QtCore import QThreadPool

        task = _LoadVideoTask(self._content, self._source, nxt_ep)
        task.signals.finished.connect(self._on_prefetch_done)
        self._prefetch_task = task  # 持引用防 GC
        QThreadPool.globalInstance().start(task)

    def _on_prefetch_done(self, ep, play, err) -> None:
        """预拉完成：写缓存，允许下一个预拉。"""
        self._prefetch_idx = -2  # 清锁，允许下一个
        if err or not play:
            return
        self._play_cache[ep.url] = play

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

    def reload_detail(self, new_detail: Detail) -> None:
        """ReaderPage 换源重载分集后调用：刷新分集列表 + 播放第一集。"""
        self._detail = new_detail
        self._episodes = new_detail.chapters
        self._play_cache.clear()  # 换源 → 旧源播放地址缓存作废
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
            self._load_episode(0)  # 只显示地址，不自动弹 mpv

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
        self._stream_seq += 1  # 递增序号，确保只接受最新请求结果
        seq = self._stream_seq
        # 后台取 dash 双流，再拉起 mpv（避免网络阻塞 UI）
        task = _LoadStreamTask(self._content, self._source, target)
        task.signals.finished.connect(
            lambda v, a, t, e, s=seq: self._on_stream_loaded(v, a, t, e, s)
        )
        self._stream_task = task  # 持有引用防 GC
        QThreadPool.globalInstance().start(task)

    def _on_stream_loaded(self, video, audio, title, err, seq) -> None:
        # 只处理最新一次请求（旧请求结果丢弃，避免 mpv 被乱序覆盖）
        if seq != self._stream_seq:
            return
        if err or not video:
            self.play_label.setText(f"获取播放流失败：{err or '无播放地址'}")
            return
        self._current_play = video
        self.play_label.setText(f"正在打开 mpv 播放：{title}")
        # 拉起 mpv（懒创建，复用实例）；referer 从源配置请求头取
        if self._player is None:
            from framework.mpv_player import MpvPlayer

            referer = ""
            _rh = getattr(self._source, "request_headers", None)
            if callable(_rh):
                referer = (_rh() or {}).get("Referer", "") or ""
            self._player = MpvPlayer(referer=referer)
        self._player.play(video, audio, title=title)


class _VideoSignals(QObject):
    """视频加载信号。"""
    finished = Signal(object, object, object)  # (episode, play, err)


class _LoadVideoTask(QRunnable):
    """后台获取视频播放地址。episode 可为 None（无分集时用 detail_url）。"""

    def __init__(self, content, source, episode, detail_url: str = ""):
        super().__init__()
        self.signals = _VideoSignals()
        self._content = content
        self._source = source
        self._episode = episode
        self._detail_url = detail_url or ""

    def run(self) -> None:
        play, err = "", None
        try:
            url = self._episode.url if self._episode is not None else self._detail_url
            play = self._content.fetch_video_episode(self._source, url)
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
