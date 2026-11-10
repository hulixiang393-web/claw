"""视频阅读视图（video_view.py）—— mpv（JSON IPC）播放后端 + 现代化交互。

- 分集卡片网格：选集以块状卡片网格展示在播放面板下方（自适应列数），
  点卡切集、当前集高亮；无封面的分集显示纯文字块（palette midlight 圆角）
- 相关推荐：同源搜索当前作品（tag[0] / 标题），取前 8 条卡片同网格渲染，
  点击经 recommend_open_requested 打开新作品；无 Search/无结果自动隐藏
- 播放后端为外部 mpv（--input-ipc-server），进度/暂停/seek/音量/倍速经 IPC 回流
- 现代化播放交互（refactor-shelf-player.md P1-P10）：
  - 控制条自动隐藏（3s 无操作隐藏 + 鼠标指针跟随隐藏）
  - 中央大播放按钮；单击视频区播放/暂停，双击全屏
  - 键盘快捷键：Space 播放暂停 / F 全屏 / M 静音 / ←→ ±5s / ↑↓ 音量 / ? 帮助
  - ⚙ 设置菜单收纳：画质 / 复制播放地址 / 刷新重试 / 重新播放 / 快捷键帮助
  - 缓冲 spinner（播放启动到首帧期间显示）
  - 播放地址调试行不再常显（状态行仅加载/错误时可见）
- 播放源换源、播放地址缓存、预拉下一集、播完自动续播
- 契约保持：load/reload_detail/set_source_sid/source_changed/episode_changed/
  play_label/stop_playback/shutdown_video
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
import time
import weakref
import webbrowser
from types import SimpleNamespace
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

from PySide6.QtGui import QCursor

from PySide6.QtCore import (
    Qt, QEvent, Signal, QThreadPool, QRunnable, QObject, QTimer, QPoint,
)
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
    QStyle,
    QStyleOptionSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from framework.content import Content, Detail
from framework.stream_resolver import StreamBudget, StreamCancelledError
from framework.transcode_session import TranscodeSession

from gui.components.hover_title import HoverTitle

log = logging.getLogger(__name__)


class _ViewEventRelay(QObject):
    """QApplication 级事件中继：把全局事件转发给**仍然存活**的 VideoView。

    为什么不让 VideoView 自己 `app.installEventFilter(self)`：过滤器挂在
    全局 app 上，视图一旦被 Python 回收（换作品、切换阅读页、测试里反复
    新建），app 的过滤器链里就留下**悬空指针**，之后任意一次
    `processEvents()` 都可能访问越界直接崩进程（实测：连跑多个测试文件时
    Windows fatal exception: access violation，栈在 processEvents 里）。

    这里由**常驻单例**持有过滤器，视图只以弱引用登记：视图消失后回调自动
    失效（weakref 取到 None 就跳过），永不产生悬空指针。
    """

    def __init__(self):
        super().__init__()
        self._views: list = []

    def register(self, view) -> None:
        self._views = [ref for ref in self._views if ref() is not None]
        if all(ref() is not view for ref in self._views):
            self._views.append(weakref.ref(view))

    def eventFilter(self, obj, event):  # noqa: N802 —— Qt 接口
        live = []
        handled = False
        for ref in list(self._views):
            view = ref()
            if view is None:
                continue
            live.append(ref)
            try:
                if view._event_filter_impl(obj, event):
                    handled = True
            except RuntimeError:
                continue  # 视图底层对象已失效：跳过
        self._views = live
        return handled


_RELAY: "_ViewEventRelay | None" = None


def _event_relay() -> "_ViewEventRelay | None":
    """取（必要时创建并安装）全局事件中继单例。"""
    global _RELAY
    if _RELAY is not None:
        return _RELAY
    app = QApplication.instance()
    if app is None:
        return None
    _RELAY = _ViewEventRelay()
    app.installEventFilter(_RELAY)
    return _RELAY


from gui.pages.reader.video_common import (
    _safe_error_text,
    _cast_history_path,
    _fmt_time,
    _fmt_hms,
)
from gui.pages.reader.video_tasks import (
    _VideoSignals,
    _FetchStreamTask,
    _ProbeSignals,
    _ProbeSourcesTask,
    _FetchLineMenuTask,
    _CastSignals,
    _CastPollTask,
    _CastStartSignals,
    _CastStartTask,
    _CastTranscodeSignals,
    _CastTranscodeTask,
    _RecommendSignals,
    _RecommendTask,
)
from gui.pages.reader.video_widgets import _PlayPanel, _GridCard


class _SeekSlider(QSlider):
    """进度条：hover 时显示当前指针位置对应的时间（拖动预览）。

    原生 QSlider 没有「悬停提示时间」能力。这里不用 QToolTip（全屏 frameless
    顶层窗口上它常被压在后面/不显示），改为挂一个悬浮 QLabel 气泡到当前窗口
    并 raise_() 到最前，窗口化与全屏都能稳定显示。
    """

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._total_ms = 0
        self._bubble = None
        self.setMouseTracking(True)

    def _ratio_at(self, pos: "QPoint | None" = None) -> float:
        if pos is None:
            pos = self.mapFromGlobal(QCursor.pos())
        try:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            groove = self.style().subControlRect(
                QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self
            )
            if groove.width() > 0:
                x = pos.x() - groove.x()
                return max(0.0, min(1.0, x / float(groove.width())))
        except Exception:  # noqa: BLE001 —— 样式不可用时退回整控件宽度
            pass
        w = self.width()
        return max(0.0, min(1.0, pos.x() / float(w))) if w > 0 else 0.0

    def _ensure_bubble(self):
        """气泡标签挂在当前窗口上（窗口化/全屏切换后自动重建父级）。"""
        win = self.window()
        b = self._bubble
        if b is None or b.parent() is not win:
            if b is not None:
                b.deleteLater()
            b = QLabel(win)
            b.setStyleSheet(
                "background: rgba(20,20,20,215); color: #fff;"
                " padding: 2px 8px; border-radius: 6px; font-size: 12px;"
            )
            b.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            b.setAttribute(Qt.WA_ShowWithoutActivating, True)
            b.hide()
            self._bubble = b
        return b

    def _show_bubble(self, text: str, x_in_widget: int) -> None:
        b = self._ensure_bubble()
        # 只有文本变化才重算尺寸（adjustSize 会触发布局）；否则只做廉价 move。
        if b.text() != text:
            b.setText(text)
            b.adjustSize()
        win = self.window()
        gp = self.mapTo(win, QPoint(int(x_in_widget), 0))
        x = max(0, min(win.width() - b.width(), gp.x() - b.width() // 2))
        y = gp.y() - b.height() - 6
        if y < 0:  # 靠近窗口顶部则翻到进度条下方
            y = gp.y() + self.height() + 6
        b.move(x, y)
        if not b.isVisible():
            b.show()       # 仅从隐藏变显示时 raise 一次（高频 raise 会重绘整窗）
            b.raise_()

    def _hide_bubble(self) -> None:
        if self._bubble is not None:
            try:
                self._bubble.hide()
            except RuntimeError:
                self._bubble = None

    def mouseMoveEvent(self, event):  # noqa: N802
        super().mouseMoveEvent(event)
        now = time.monotonic()
        # 16ms 节流：高频 MouseMove 不必每帧都重算文本/移动气泡
        if now - getattr(self, "_last_move_ts", 0.0) < 0.016:
            return
        self._last_move_ts = now
        ratio = self._ratio_at(event.position().toPoint())
        total_ms = self._total_ms
        cur_ms = int(ratio * total_ms)
        if total_ms > 0:
            tip = f"{_fmt_hms(cur_ms / 1000.0)} / {_fmt_hms(total_ms / 1000.0)}"
        else:
            tip = f"{int(ratio * 100)}%"
        self._show_bubble(tip, event.position().toPoint().x())

    def leaveEvent(self, event):  # noqa: N802
        self._hide_bubble()
        super().leaveEvent(event)

    def hideEvent(self, event):  # noqa: N802
        self._hide_bubble()
        super().hideEvent(event)


# 时效签名 URL 的过期参数名（与 StreamResolver.SIGNED_QUERY_NAMES 对齐）
_EXPIRY_QUERY_NAMES = (
    "expire",
    "expires",
    "expires_at",
    "x-amz-expires",
    "x-expires",
)


def _signed_url_remaining_ttl(values) -> float:
    """从带时效签名的播放 URL 推断剩余可用时长（秒）。

    返回 0 表示无法判断过期时间：调用方按不可缓存处理，避免把过期地址
    交给播放器。支持秒/毫秒时间戳（expire/expires/expires_at 等）。
    """
    expiries = []
    for value in values:
        if not value:
            continue
        try:
            query = parse_qs(urlsplit(value).query)
        except Exception:  # noqa: BLE001
            continue
        for name in _EXPIRY_QUERY_NAMES:
            raw = (query.get(name) or [""])[0]
            if not raw:
                continue
            try:
                stamp = float(raw)
            except (TypeError, ValueError):
                continue
            if stamp > 1e12:  # 毫秒时间戳
                stamp /= 1000.0
            if stamp > 0:
                expiries.append(stamp)
    if not expiries:
        return 0.0
    # 提前 5 秒失效，避免播放器拿到临界过期地址
    return max(0.0, min(expiries) - time.time() - 5.0)


# ---------------------------------------------------------------------- #
# 「同一集」识别（续播对位）
#
# 续播位置**不能按 URL 全等定位**：同一集换线路（omofuna 的 /play/1/11.html →
# /play/3/11.html）、CDN 换域名、m3u8 每次新签 auth_key，URL 都会变样。旧的
# `record["chapter_url"] == 当前集 URL` 判定在换线路后恒为假 → 位置被丢弃，
# 从头重播（用户看到的就是「明明看过还从头放」）。
#
# 这里按两级归一化比对：
#   1. 集号签名 (第几集, 集名后缀)：标题「第01集」/「11集」都归到 (11, "")，
#      「第1集 上」/「第1集 下」归到 (1, "上") / (1, "下")，不会互相串。
#   2. 取不到集号时，比「去掉易变查询参数后的 URL」：单集视频的签名 m3u8
#      路径稳定、只有 query 变，这一级就能命中。
# ---------------------------------------------------------------------- #
_EPISODE_NUMBER_RES = (
    re.compile(r"第\s*(\d{1,4})\s*[集话話期回]"),
    re.compile(r"(\d{1,4})\s*[集话話期回]"),
    re.compile(r"(?i)\bEP?\s*[._\-]?\s*(\d{1,4})\b"),
    re.compile(r"^\s*(\d{1,4})\s*$"),
)
_URL_TAIL_NUMBER_RE = re.compile(r"(?<!\d)(\d{1,4})$")
# 易变查询参数：防盗链签名、时间戳、线路号。比对身份时一律剔除。
_VOLATILE_QUERY_KEYS = frozenset({
    "auth_key", "authkey", "expire", "expires", "expires_at", "key", "rand",
    "sign", "signature", "sid", "t", "time", "token", "ts", "v",
})
# 集名后缀去噪：标点、序数外壳与数字（数字已单独抽成集号，留着比不出区别，
# 只会让「11集」和「第11集」这类同一集的两种写法对不上）
_TITLE_NOISE_RE = re.compile(r"[\s\-_—·.、,，:：()（）\[\]【】第集话話期回\d]+")


def _episode_signature(url: str = "", title: str = "") -> tuple:
    """返回 (集号 | None, 集名后缀)：同一集的稳定签名。"""
    label = (title or "").strip()
    number = None
    rest = label
    for pattern in _EPISODE_NUMBER_RES:
        match = pattern.search(label)
        if match:
            number = int(match.group(1))
            rest = label[:match.start()] + label[match.end():]
            break
    if number is None:
        # URL 尾段的集号：先去扩展名再取末尾数字，"b.m3u8" 里的 8 不能当集号
        tail = urlsplit(url or "").path.rsplit("/", 1)[-1]
        stem = tail.rsplit(".", 1)[0] if "." in tail else tail
        match = _URL_TAIL_NUMBER_RE.search(stem)
        if match:
            number = int(match.group(1))
    return number, _TITLE_NOISE_RE.sub("", rest)


def _url_identity(url: str) -> str:
    """去掉易变查询参数后的 URL 身份（路径 + 稳定参数）。"""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ""
    if not parts.path:
        return ""
    kept = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _VOLATILE_QUERY_KEYS
    )
    suffix = f"?{urlencode(kept)}" if kept else ""
    return (parts.path.rstrip("/") or "/") + suffix


def _same_episode(a_url: str = "", a_title: str = "", b_url: str = "",
                  b_title: str = "", single: bool = False) -> bool:
    """两个 URL/标题是否指向同一集（容忍换线路/换 CDN/换签名）。

    single：该作品只有一集——此时「哪一集」无需判断，URL 怎么变都是同一集。
    """
    if a_url and a_url == b_url:
        return True
    signature_a = _episode_signature(a_url, a_title)
    signature_b = _episode_signature(b_url, b_title)
    # 集号冲突 → 一定不是同一集。这条**先于** single 生效：连载型单集作品换集后
    # 集号会变，不能因为「只有一集」就把上一集的秒数套到新集上。
    if (signature_a[0] is not None and signature_b[0] is not None
            and signature_a[0] != signature_b[0]):
        return False
    if single:
        return True
    # 签名比对必须有实义信息（集号或集名后缀）：两边都空 = 无从判断，不能当同一集，
    # 否则「无标题、URL 又不像」的两个不同分集会被误判成同一集而错位续播。
    if signature_a == signature_b and (signature_a[0] is not None or signature_a[1]):
        return True
    identity_a = _url_identity(a_url)
    return bool(identity_a) and identity_a == _url_identity(b_url)


class VideoView(QWidget):
    """视频分集 + 播放后端视图（现代化交互，支持多播放源换源）。"""

    episode_changed = Signal(object)  # (detail, 集标题, 集URL) → 进度记忆
    position_changed = Signal(object)  # (detail, 标题, URL, 播放进度 0~1, None) 续读
    source_changed = Signal(object)  # (detail, new_sid, target_url) → ReaderPage 换源
    download_requested = Signal(object)  # (source_id, detail.url, content_type) → 下载当前作品
    recommend_open_requested = Signal(object)  # (source_id, url, content_type) → 打开推荐视频

    # CastSession 的 on_local_player_stop 回调在**轮询线程**里被调用 → 这里
    # emit 后队列投递到主线程才真正停本地播放器（跨线程绝不碰 Qt 对象）。
    # 顺序保证：start() 不停本地播放器，只有电视报 PLAYING 才走到这条信号。
    _cast_local_player_stop = Signal()
    _progress_position = Signal(int, float, float)
    # 外部播放器切到播放列表的某一集时，媒体代理 /e/ 的 on_play 回调在
    # HTTP 线程里触发；这里统一 emit 回主线程，让 App 的当前集/进度记忆
    # 跟随播放器实际播放的那一集（播放器自己的「下一集」也能被感知）。
    _series_played = Signal(int)

    # spec §6.4 步骤 6：投屏启动后轮询到电视 PLAYING 的超时（毫秒）
    CAST_PLAYING_TIMEOUT_MS = 8000
    PREFETCH_BEFORE_END_S = 4 * 60
    # 预取链接缓存比 4 分钟窗口多留 2 分钟：用户可能暂停在结尾附近，
    # 30s 缓冲太紧会让刚预取到的链接在点下一集前过期。
    PREFETCH_CACHE_MARGIN_S = 120.0
    # 预取失败后的重试间隔（秒）：4 分钟窗口内允许少量重试，避免一次
    # 网络抖动就把该集的预取永久锁死；也避免每 5 秒进度回调都重试压站。
    PREFETCH_RETRY_S = 30.0
    # 同一集同一位置的重试上限与位置容差：进度回写本来就是 5s 一条，容差
    # 小于它才会真的挡住「暂停在窗口里反复触发」。
    PREFETCH_MAX_ATTEMPTS = 3
    PREFETCH_LATCH_TOLERANCE_S = 2.0
    # 兜底巡检周期（毫秒）：播放器在「缓冲/暂停」时可能整段不发进度，
    # 只靠 on_progress 驱动会让 4 分钟窗口整段空转。巡检只读一次播放器状态，
    # 已锁存或已缓存时不做任何网络请求。
    PREFETCH_TICK_MS = 20000
    # 预取成功后「预热」下一集的磁盘分片数：切集时播放器的第一个分片
    # 直接命中本地文件，省掉 master/variant 清单与首片的上游往返。
    PREFETCH_PRIME_SEGMENTS = 3
    # 内嵌播放：进度回写节流（秒）。播放器 的 TimeChanged 约 4Hz，直接落盘
    # 会把 ReadingProgress 的锁与 JSON 写放大十几倍；2s 既能保证「切走不丢
    # 最后几秒」，又不会被写盘拖慢播放。
    POSITION_SAVE_INTERVAL_S = 2.0
    # 进度兜底轮询（毫秒）：个别容器/流不发 TimeChanged 事件，靠定时读兜底。
    PROGRESS_POLL_MS = 1000
    # 每集只显示一次「已提前准备好下一集」提示（避免抖动刷屏）
    PREFETCH_NOTICE_ONCE = True

    # 快捷键帮助内容（? 键浮层）
    _HELP_TEXT = (
        "操作指南\n"
        "──────────────\n"
        "播放（画面在 App 内，内嵌播放器）\n"
        "  单击面板 / 中央播放键    播放 / 暂停当前集\n"
        "  双击面板                  全屏 / 退出全屏\n"
        "  设置 → 重新播放当前集     画面或声音异常时重建播放器\n"
        "  空格                     播放 / 暂停\n"
        "  ← →                      快退 / 快进 10 秒\n"
        "  ↑ ↓                      音量 +10 / -10\n"
        "  M                        静音切换\n"
        "  F                        全屏 / 退出全屏\n"
        "  N / P                     下一集 / 上一集\n"
        "  ?                        显示 / 隐藏本帮助\n"
        "──────────────\n"
        "  进度自动记忆：下次打开从上次位置继续（换线路也不丢）\n"
        "  播完自动进入下一集；提前 4 分钟预取下一集\n"
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
        self._locked_source_id = ""
        self._switching = False
        self._stream_cache: dict = {}
        self._prefetch_cache: dict = {}
        self._stream_cache_ttl = 30.0
        self._stream_task = None
        self._probe_task = None
        self._probe_generation = 0
        self._source_probe_settings = None
        self._probe_results = {}
        self._probe_names = {}
        self._probe_reasons = {}
        self._prefetch_task = None
        self._prefetch_idx = -2  # 正在预拉下一集（<0 空闲）
        self._prefetch_attempted: set = set()
        self._prefetch_retry_at: dict = {}  # key → 允许下次预取的时间点
        self._prefetch_hopeless: set = set()  # 冷却期外仍应跳过的 key（见 _mark_hopeless）
        # 预取锁存：{(集下标, 画质): (触发时的 (seconds, duration), 发起次数)}。
        # 同一集在同一个播放位置只触发一次，进度在窗口内来回晃（暂停/回拖）
        # 不会反复取流；位置显著变化或冷却到期才会再发起。
        self._prefetch_state: dict = {}
        self._quality = "best"
        self._quality_options: list = []
        self._detail_url_for_play = ""  # 无分集时记录详情 URL
        self._player = None
        self.player_menu = None       # 「播放器」子菜单（_build_settings_menu 里建）
        self._fs_win = None  # 全屏顶层窗口
        self._last_pos_save_ts = 0.0  # 上次播放进度存盘时间戳（节流 2s 存一次）
        self._play_started_at = 0.0   # 起播剖面：_play 进入时刻（单调时钟）
        self._first_frame_at = None   # 起播剖面：首帧到达时刻（None = 还没到）
        self._pending_position = None  # 打开书续读播放位置（0~1），播放开始后 seek
        self._has_played = False  # 是否真正开始过播放（未播放不落盘，防覆盖恢复进度）
        # 内嵌播放：进度的两个驱动源与续播锚点
        self._progress_cb = None          # _progress_options 产出的回写回调
        self._pending_seek_s = None       # 待执行的续播秒数（拿到时长后消费）
        self._pending_seek_ratio = None   # 无 seconds 时按比例续播
        self._player_length_ms = 0        # 当前媒体总长（播放器 报告）
        self._progress_timer = None       # 进度兜底轮询
        # 线路菜单延迟加载：代数用于丢弃迟到结果（换作品/换源后旧结果作废）
        self._line_menu_task = None
        self._line_menu_generation = 0
        self._pending_play = None  # 取流完成但视图不可见 → 暂存 (video, audio, title)，显示后再播
        self._click_pending = False  # 单击/双击判定
        # 惰性系列（media_proxy /e/）承载「外部播放器的下一集」：播放列表里
        # 后续集都指向 /e/<key>/<idx>，切集时由代理按需解析或直接命中预取 memo。
        self._series_key = ""
        self._series_urls: list[str] = []
        # 注册时的画质：换画质后 memo 里是旧画质的地址，必须重建系列
        self._series_quality = ""
        # 播放代数：换作品/换源自增，用于丢弃在途的旧取流/预取回调
        self._play_gen = 0
        self._stream_gen = 0
        self._progress_position.connect(self._on_progress_position)
        self._series_played.connect(self._on_series_played)
        self._last_ep_toast_ts = 0.0  # 「已是最后一集」提示去抖时间戳
        from framework.media_tuner import MediaTuner

        self._tuner = MediaTuner()  # 卡顿统计/缓冲升级（每次 load 重置）
        # 卡顿后自动加缓冲并重播（本会话内最多 3 级）。此前 MediaTuner 只被
        # 创建和 reset，没人把 buffering 事件喂进来、也没人触发升级，
        # 于是「自适应缓冲」从头到尾没生效过——多源卡顿的共性原因之一。
        self._tuner_busy = False      # 正在升级重播（防重入）
        self._buffer_upgrade_ms = 0   # 本会话已升到的缓冲值（0=用默认画像）
        self._hide_timer = QTimer(self)  # 控制条自动隐藏（空闲 5s）
        self._hide_timer.setInterval(5000)
        self._hide_timer.timeout.connect(self._hide_controls)

        # ---- 投屏（DLNA）状态：idle / preparing_stream / connecting /
        #      playing / paused / stopped / error ----
        self._cast_session = None       # framework.cast_session.CastSession
        self._cast_renderer = None      # 当前投屏的 DlnaRenderer
        self._cast_device = ""          # 电视 friendly_name（按钮悬停展示）
        self._cast_active = False       # 电视端接管中（控制路由切到会话）
        self._cast_state = "idle"
        self._cast_error = ""           # 最近一次投屏失败原因（清空 = 干净 idle）
        self._cast_duration = 0         # 电视端总时长（秒，轮询回填）
        self._cast_volume = 50          # 电视端主音量（0-100，音量键相对调整基准）
        self._cast_poll_task = None     # _CastPollTask（防 GC + 供 stop()）
        self._cast_stream_task = None   # 投屏取流任务（防 GC）
        self._cast_stream_pending = None  # (目标集下标, "start"|"episode")
        self._cast_status_text = ""     # 最近一次投屏状态文案（轮询去抖）
        # 投屏异步代数：每次新取流/新启动自增，_stop_cast 再自增作废在途操作
        # （迟到的取流/启动回调按代数整条丢弃，不许消费/覆盖新的 pending）
        self._cast_gen = 0
        self._cast_start_task = None    # _CastStartTask（防 GC + 陈旧启动守卫）
        self._cast_transcode_task = None
        self._cast_transcoding = False
        self._cast_transcode_attempted = False
        self._cast_video = ""
        self._cast_local_stop_pending = False
        self._cast_startup_retry_attempted = False
        # 投屏设备历史（data/cast_history.json，惰性打开）：
        # 有历史 → 取流完成直接重连上次设备；start 失败才回落选设备对话框
        self._cast_history_store = None  # framework.cast_history.CastHistory
        self._cast_direct = False        # 正在直投上次设备（失败需回落对话框）
        # spec 6.4 步骤 6：启动后 CAST_PLAYING_TIMEOUT_MS 内电视未进 PLAYING → 判投屏失败
        self._cast_playing_timer = QTimer(self)
        self._cast_playing_timer.setSingleShot(True)
        self._cast_playing_timer.setInterval(self.CAST_PLAYING_TIMEOUT_MS)
        self._cast_playing_timer.timeout.connect(self._on_cast_playing_timeout)
        self._cast_local_player_stop.connect(self._on_cast_local_player_stop)

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
        self.probe_btn = QPushButton("检测播放源")
        self.probe_btn.setFixedWidth(96)
        self.probe_btn.clicked.connect(self._probe_sources)
        source_row.addWidget(self.probe_btn)
        layout.addLayout(source_row)
        self.source_label.setVisible(False)
        self.source_combo.setVisible(False)
        self.probe_btn.setVisible(False)
        self.probe_btn.setEnabled(False)

        # ---- 主体：播放面板在上，分集/推荐卡片网格在下 ----
        body = QVBoxLayout()
        body.setSpacing(8)

        # 播放器整体容器：**画面占位 + 控制条**上下两行，一起进全屏。
        # 画面在 mpv 自己的窗口里渲染（VLC 的 set_hwnd 内嵌方案已下线），
        # 这块面板只做占位与提示；控制条留在下方，全屏时与本面板一起搬进
        # 全屏窗口，快捷键与控件位置保持不变。
        self._player_box = QWidget(self)
        pbox = QVBoxLayout(self._player_box)
        pbox.setContentsMargins(0, 0, 0, 0)
        pbox.setSpacing(0)

        # 画面占位区：不再承载任何原生视频 surface，只显示「在播放器窗口中
        # 播放」的提示。保留控件是因为全屏、事件过滤、焦点与覆盖层都挂在它
        # 上面。
        self._video_frame = _PlayPanel(self, parent=self._player_box)
        self._video_frame.setMinimumHeight(320)
        self._video_frame.setFocusPolicy(Qt.StrongFocus)
        pbox.addWidget(self._video_frame, stretch=1)

        # ---- 覆盖层：中央播放按钮 / 缓冲 spinner / 帮助浮层 ----
        self._build_overlays()

        # ---- 控制条：停靠在画面下方（不叠进原生视频窗口）----
        self.control_bar = QWidget(self._player_box)
        self.control_bar.setObjectName("videoControlBar")
        self.control_bar.setStyleSheet(
            "QWidget#videoControlBar { background: rgba(20,20,20,235); }"
        )
        cb = QHBoxLayout(self.control_bar)
        cb.setContentsMargins(10, 6, 10, 6)
        cb.setSpacing(6)
        self._build_control_bar(cb)
        pbox.addWidget(self.control_bar)
        body.addWidget(self._player_box, stretch=5)

        # ---- 可滚动卡片网格区（分集在上，相关推荐在下）----
        self._cards_scroll = QScrollArea()
        self._cards_scroll.setWidgetResizable(True)
        self._cards_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 竖直滚动条常显：分集/相关推荐卡片含大量封面，异步加载使高度越过视口时
        # 滚动条出现→视口变窄→卡片重排晃动。常显固定宽度。
        self._cards_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
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
        self._reposition_overlays()  # 初始定位覆盖层
        # 控制条一律**可用**（不再有外播时代那套"禁用并注明"）：没开播时点
        # 进度/音量/倍速只是空操作，比灰掉更不容易让人以为坏了。开播时
        # _enable_embedded_controls 会把当前音量下发给播放器。
        self._enable_embedded_controls()
        # 全局事件过滤器：鼠标唤出控制条 + 全屏窗键盘/关闭兜底。
        # 由常驻中继单例安装、本视图以弱引用登记——视图被回收后回调自动
        # 失效，不会在 app 的过滤器链里留下悬空指针（见 _ViewEventRelay）。
        relay = _event_relay()
        if relay is not None:
            relay.register(self)

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

        # 中央播放按钮（未播放时显示，点击开始播放当前集）
        self.center_play_btn = QPushButton("▶ 播放", self._overlay_root)
        self.center_play_btn.setFixedSize(96, 96)
        self.center_play_btn.setCursor(Qt.PointingHandCursor)
        self.center_play_btn.setToolTip("播放当前集（双击画面全屏）")
        self.center_play_btn.setStyleSheet(
            "QPushButton { background: rgba(0,0,0,150); color: white;"
            " border: 2px solid rgba(255,255,255,120); border-radius: 48px;"
            " font-size: 22px; padding: 0px; }"
            "QPushButton:hover { background: rgba(40,40,40,200);"
            " border-color: rgba(255,255,255,200); }"
        )
        self.center_play_btn.clicked.connect(self._toggle_play_pause)

        # 缓冲进度条（播放启动到首帧期间显示）
        # 用**确定进度**而不是不定动画：mpv 的 demuxer-cache-state 经 IPC
        # 回传缓存填充百分比（cache-buffering-state），之前只拿它做 show/hide、
        # 控件又是 setRange(0,0) 的忙碌动画，所以「加载进度」在界面上完全
        # 读不出来（用户看到的就是一个转圈小方块，不知道加载到哪一步）。
        self.buffer_spinner = QProgressBar(self._overlay_root)
        self.buffer_spinner.setRange(0, 100)
        self.buffer_spinner.setValue(0)
        self.buffer_spinner.setFixedSize(240, 22)
        self.buffer_spinner.setTextVisible(True)
        self.buffer_spinner.setFormat("载入中…")
        self.buffer_spinner.setAlignment(Qt.AlignCenter)
        self.buffer_spinner.setStyleSheet(
            "QProgressBar { background: rgba(0,0,0,170); color: white;"
            " border: 1px solid rgba(255,255,255,90); border-radius: 11px;"
            " font-size: 12px; text-align: center; }"
            "QProgressBar::chunk { background: rgba(255,255,255,170);"
            " border-radius: 10px; }"
        )
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

        self.progress = _SeekSlider()
        self.progress.setRange(0, 1000)
        # 固定粗细进度条：不再用 QSlider:hover 改 groove/handle 尺寸——
        # 那种 hover 几何变化会让 Qt 每次鼠标进出都重解析样式并重排重绘，
        # 正是「显示/隐藏进度条一下明显卡顿」的来源。hover 时间由气泡提供。
        self.progress.setStyleSheet(
            "QSlider::groove:horizontal { height: 8px; background: palette(mid);"
            " border-radius: 4px; }"
            "QSlider::sub-page:horizontal { background: palette(highlight);"
            " border-radius: 4px; }"
            "QSlider::handle:horizontal { width: 14px; margin: -3px 0;"
            " background: palette(highlight); border-radius: 7px; }"
        )
        self.progress.setToolTip("")
        cb.addWidget(self.progress, stretch=1)

        # 进度条：拖动 = seek 到对应比例（内嵌播放器直接 set_position）
        self.progress.sliderReleased.connect(self._on_progress_released)
        self.progress.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        # 投屏进度：仅 cast_active 下生效（槽内自校验）
        self.progress.sliderReleased.connect(self._on_cast_progress_released)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet("color: palette(dark); font-size: 11px;")
        cb.addWidget(self.time_label)

        self.vol_btn = self._icon_btn("🔊", "静音（M）", self._toggle_mute, width=32)
        cb.addWidget(self.vol_btn)

        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(80)
        self.vol_slider.setFixedWidth(80)
        self.vol_slider.valueChanged.connect(self._on_volume_changed)
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

        # 投屏（📺）：换源按钮旁的 DLNA 入口。未投屏显示「投屏」，投屏中
        # 显示「停止投屏 - <设备名>」；状态/可用性由 _update_cast_button 统一刷。
        self.cast_btn = self._icon_btn("📺", "投屏", self._on_cast_clicked, width=32)
        cb.addWidget(self.cast_btn)

        # 设置菜单（⚙）：画质 + 复制地址 + 刷新 + 重新播放 + 帮助
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

        # 初始可用性：无源/无当前集 → 禁用（_disable_embedded_controls 不碰它，
        # 外部播放器模式下投屏入口依旧可用）
        self._update_cast_button()

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
        """无当前流时禁用播放控件（内嵌播放器接管进度/音量/倍速/全屏）。

        与旧外部播放器方案相反：这些控件现在是**可用**的，只是无流时先禁用，
        真正开播后由 _play 解禁。
        """
        for w in (self.progress, self.time_label, self.vol_btn, self.vol_slider,
                  self.speed_combo, self.fs_btn):
            w.setEnabled(False)
        self.time_label.setText("00:00 / 00:00")

    def _enable_embedded_controls(self) -> None:
        """开播后解禁内嵌播放控件并接线到播放器。"""
        for w in (self.progress, self.time_label, self.vol_btn, self.vol_slider,
                  self.speed_combo, self.fs_btn):
            w.setEnabled(True)
        self._apply_volume_from_slider()

    def _apply_volume_from_slider(self) -> None:
        """把当前音量滑杆值下发给播放器（开播/切集后调用）。"""
        if self._player is None:
            return
        try:
            self._player.set_volume(int(self.vol_slider.value()))
        except Exception:  # noqa: BLE001 —— 播放器未就绪：下一次再试
            pass

    def _on_volume_changed(self, value: int) -> None:
        if self._player is not None:
            try:
                self._player.set_volume(int(value))
            except Exception:  # noqa: BLE001
                pass
        self.vol_btn.setText("🔇" if int(value) == 0 else "🔊")

    def _toggle_mute(self) -> None:
        if self._player is None:
            return
        try:
            self._player.toggle_mute()
            self.vol_btn.setText("🔇" if self._player.is_muted() else "🔊")
        except Exception:  # noqa: BLE001
            pass

    def _on_progress_released(self) -> None:
        """进度条拖动 → seek（内嵌播放器按比例定位）。"""
        self._seeking = False
        if self._cast_active or self._player is None:
            return
        if self._player_length_ms <= 0:
            return
        ratio = max(0.0, min(1.0, self.progress.value() / 1000.0))
        try:
            self._player.set_position(ratio)
        except Exception:  # noqa: BLE001 —— seek 失败保留在原位
            log.warning("进度拖动 seek 失败（ratio=%.3f）", ratio, exc_info=True)

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
        menu.addAction("↗ 重新播放当前集").triggered.connect(self._open_external)
        self.player_menu = menu.addMenu("播放器")
        menu.addSeparator()
        menu.addAction("? 快捷键帮助").triggered.connect(self._toggle_help)

        self._quality_actions = []  # 防止菜单项被 GC
        self._player_actions = []
        self._refresh_quality_menu()
        self._refresh_ep_menu()
        self._refresh_source_menu()
        self._refresh_player_menu()

    def _refresh_player_menu(self) -> None:
        """播放器菜单：列出本机检测到的播放器，勾选即为默认后端。

        只有 mpv 支持 JSON IPC（界面能读进度、发控制命令）；选其它播放器时
        播放照常，但 App 里的进度/暂停/seek 会退化为不可用，菜单里会标注。
        """
        menu = getattr(self, "player_menu", None)
        if menu is None:
            return
        menu.clear()
        self._player_actions = []
        found, current = {}, None
        try:
            from framework import desktop_player

            prefs = desktop_player.load_prefs()
            found = desktop_player.detect()
            current = desktop_player.resolve(prefs.player, prefs.player_path)[0]
        except Exception:  # noqa: BLE001 —— 探测失败只影响菜单内容
            log.warning("播放器探测失败", exc_info=True)
        if not found:
            act = menu.addAction("（未检测到播放器，推荐 mpv）")
            act.setEnabled(False)
        for key in found:
            try:
                from framework.desktop_player import player_display_name

                label = player_display_name(key)
            except Exception:  # noqa: BLE001
                label = key
            suffix = "" if key == "mpv" else "  ·  无进度同步"
            act = menu.addAction(f"{label}{suffix}  ·  {found[key]}")
            act.setCheckable(True)
            act.setChecked(key == current)
            act.triggered.connect(
                lambda _=False, _k=key: self._set_player_backend(_k))
            self._player_actions.append(act)

    def _set_player_backend(self, key: str) -> None:
        """把某个播放器设为默认（写入 app_config.json 的 video.player）。"""
        self._save_video_config(player=key, player_path="")
        self._refresh_player_menu()
        try:
            from framework.desktop_player import player_display_name

            name = player_display_name(key)
        except Exception:  # noqa: BLE001
            name = key
        extra = "" if key == "mpv" else "（该播放器不同步进度）"
        self._show_status(f"播放器已设为 {name}{extra}")

    @staticmethod
    def _save_video_config(**values) -> None:
        """写 app_config.json 的 video 段（失败只记日志，不影响播放）。"""
        try:
            from pathlib import Path

            from framework.settings_manager import SettingsManager

            root = Path(__file__).resolve().parents[3]
            sm = SettingsManager(root / "app_config.json")
            for key, value in values.items():
                sm.set("video", key, value)
            sm.save()
        except Exception:  # noqa: BLE001
            log.warning("保存外部播放器配置失败", exc_info=True)

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
        """首次构建选集菜单：每集一项，点击即切换（播放器内操作，全屏可用）。

        仅在有作品/换集数时调用一次；切集**不**重建菜单（见 _update_ep_check），
        否则上百集每次切集都 clear+addAction 会明显卡顿、破坏秒级响应。
        """
        self.ep_menu.clear()
        self._ep_actions = []
        for i, ep in enumerate(self._episodes):
            act = self.ep_menu.addAction(ep.title or f"第{i + 1}集")
            act.setCheckable(True)
            act.setChecked(i == self._current_idx)
            act.triggered.connect(lambda _=False, _i=i: self._select_episode(_i))
            self._ep_actions.append(act)
        self.ep_menu.menuAction().setVisible(bool(self._episodes))

    def _update_ep_check(self) -> None:
        """切集后只刷新勾选态（O(集数) 轻量，不重建菜单/信号）。"""
        for i, act in enumerate(self._ep_actions):
            act.setChecked(i == self._current_idx)

    def _select_episode(self, idx: int) -> None:
        """播放器内选集：优先命令播放器切集，否则加载该集。

        选集态下点当前高亮集也视为选择 → 立即取流播放；
        播放中点同一集不重载。
        """
        if not (0 <= idx < len(self._episodes)):
            return
        if idx == self._current_idx and not self._selection_mode:
            return
        # 投屏中：选集走 CastSession.set_episode（电视端切集），不碰本地播放路径
        if self._cast_active:
            self._cast_set_episode(idx)
            return
        # 内嵌播放器：切集 = 取流后换 media（播放器实例复用，不重开进程、
        # 不重建 播放器 实例），进度由 _stop_player 的 release 收尾。
        self._load_episode(idx)
        self._update_ep_check()  # 仅刷新勾选，不重建菜单（避免卡顿）

    def _refresh_source_menu(self) -> None:
        """重建播放源菜单：站内多线路（⇄）。

        按钮在有作品时显示。
        """
        self.source_menu.clear()
        self._source_actions = []
        current_url = str(getattr(self._detail, "url", "") or "")
        for i, item in enumerate(self._source_list):
            name = item.get("name") if isinstance(item, dict) else str(item)
            sid = (item.get("source_id") or item.get("sid")) if isinstance(item, dict) else str(item)
            act = self.source_menu.addAction(str(name))
            act.setCheckable(True)
            # 同名同 sid 的多条线路靠 detail_url 区分勾选态
            item_url = str(item.get("detail_url") or "") if isinstance(item, dict) else ""
            act.setChecked(
                item_url == current_url if item_url
                else str(sid) == self._current_sid
            )
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
        self.source_combo.setCurrentIndex(idx)

    # ------------------------------------------------------------------ #
    # 控制条自动隐藏
    # ------------------------------------------------------------------ #
    def _wake_controls(self) -> None:
        """鼠标移动/触碰 → 恢复指针，并重启全屏空闲隐藏计时。

        - 窗口化：控制条常驻、指针永不隐藏——直接修掉「缩小窗口里操作时鼠标
          会消失」的 bug。
        - 全屏：控制条与进度条/时间条**始终可见**（这里刚修掉「全屏时间条
          不显示」），只有鼠标指针在空闲 5s 后隐藏，移动即恢复。
        """
        if (self._fs_win is not None and self._has_played
                and self._player is not None and self._player.is_playing()):
            self._hide_timer.start()  # 重新计时 5s 指针隐藏窗口
        # 任何状态鼠标一动就恢复箭头；仅当前不是箭头时才设，避免高频 move 抖动
        if self.cursor().shape() != Qt.ArrowCursor:
            self.setCursor(Qt.ArrowCursor)

    def _hide_controls(self) -> None:
        """空闲定时器：仅「全屏 + 播放中」隐藏鼠标指针。

        控制条/进度条/时间条在窗口化与全屏都常驻不隐藏（保证全屏也能看到并
        hover 时间条）；暂停或未播放时不隐藏指针。退出全屏/暂停时
        _sync_overlay_state 会停表，避免指针卡在隐藏态。
        """
        if self._fs_win is None:
            return  # 窗口化：指针常驻
        if self._player is None or not self._has_played or not self._player.is_playing():
            return  # 暂停/未播放：指针常驻
        self.setCursor(Qt.BlankCursor)

    def _adjust_cast_volume(self, delta: int) -> None:
        """投屏态音量键 → 电视端 SetVolume（CI）。

        本地音量命令只作用于本机播放器，对电视无效；投屏时改为把绝对
        音量下发到会话。失败不静默：记录可观察日志且不更新本地基准（保持与
        电视端一致，避免下一次相对调整跳变）。
        """
        level = max(0, min(100, self._cast_volume + delta))
        session = self._cast_session
        if session is None:
            log.warning("投屏音量失败：无可用的投屏会话")
            return
        try:
            session.set_volume(level)
        except Exception as exc:  # noqa: BLE001 —— 设备不支持/网络失败均为受控失败
            log.warning("投屏音量下发失败（目标 %d）：%s", level, exc)
            return
        self._cast_volume = level
        log.debug("投屏音量已下发：%d", level)

    def _handle_key(self, event) -> None:
        """键盘快捷键（内嵌播放器）。

        画面在 App 内，按键直接驱动 播放器，不再需要转发给外部进程的
        HTTP 控制接口。投屏中上下键调电视音量；无播放器时只保留 ? 帮助。
        """
        key = event.key()
        if self._cast_active and key in (Qt.Key_Up, Qt.Key_Down):
            self._adjust_cast_volume(10 if key == Qt.Key_Up else -10)
            event.accept()
            return
        player = self._player if self._has_played else None
        if player is not None:
            handled = True
            try:
                if key == Qt.Key_Space:
                    self._toggle_play_pause()
                elif key == Qt.Key_Left:
                    player.seek_relative(-10)
                elif key == Qt.Key_Right:
                    player.seek_relative(10)
                elif key == Qt.Key_Up:
                    player.set_volume(int(self.volume_slider.value()) + 10)
                elif key == Qt.Key_Down:
                    player.set_volume(int(self.volume_slider.value()) - 10)
                elif key == Qt.Key_M:
                    player.toggle_mute()
                elif key == Qt.Key_F:
                    self._toggle_fullscreen()
                elif key == Qt.Key_N:
                    self._on_next_ep()
                elif key == Qt.Key_P:
                    self._on_prev_ep()
                else:
                    handled = False
            except Exception:  # noqa: BLE001 —— 播放器释放竞态：忽略该键
                handled = False
            if handled:
                event.accept()
                return
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
        """根据播放状态切换中央播放按钮/缓冲 spinner 显隐，并联控制条。

        - 暂停/未播放：控制条常驻可见，停止空闲隐藏计时（否则暂停后仍会
          5s 藏条，无法操作）——这同时修掉「暂停/切集按钮卡顿像没响应」。
        - 全屏播放中：启动 5s 空闲隐藏计时（沉浸态）。
        - 窗口化：控制条始终可见、指针不隐藏（见 _hide_controls）。

        只在**显隐真的变了**的时候才重排覆盖层：`_reposition_overlays()` 里有
        `adjustSize()`，状态事件连发时反复重排会把 UI 线程拖死（按键卡顿）。
        """
        want_visible = not playing
        # 判定用 isHidden()（控件自己的显隐标志）：isVisible() 还受父窗口是否
        # 显示影响，未显示的父窗口会让它恒为 False，于是每次都判成「变了」。
        changed = self.center_play_btn.isHidden() == want_visible
        self.center_play_btn.setVisible(want_visible)
        if not playing:
            if self.control_bar.isHidden():
                self.control_bar.show()
                changed = True
            self._hide_timer.stop()
        elif self._fs_win is not None and self._has_played:
            self._hide_timer.start()
        if changed:
            self._reposition_overlays()

    def _reposition_overlays(self) -> None:
        """覆盖层随画面区尺寸变化居中（resizeEvent 兜底 + 全屏切换后）。

        控制条已停靠在画面下方（见 _build_ui），这里不再给它算几何。
        """
        frame = self._video_frame
        self._overlay_root.setGeometry(frame.rect())
        w, h = frame.width(), frame.height()
        if w <= 0 or h <= 0:
            return
        cy = (h - 96) // 2
        self.center_play_btn.move((w - 96) // 2, max(0, cy))
        bar = self.buffer_spinner
        self.buffer_spinner.move((w - bar.width()) // 2,
                                 max(0, (h - bar.height()) // 2))
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
        self._update_cast_button()  # 当前集变化 → 投屏按钮可用性跟随

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
        """装载相关推荐：优先详情页内嵌 related（如 hanime1/pornhub），
        缺失或过滤为空才回退同源关键词搜索（换源/换作品后重搜）。"""
        if self._source is None or self._detail is None:
            return
        rel = getattr(self._detail, "related", None) or []
        if rel:
            picks = self._picks_from_related(rel)
            if picks:
                self._render_rec_cards(picks)
                return  # related 有货即渲染；keyword 去重只属于搜索分支
        if self._search is None:
            return
        kw = self._recommend_keyword_of(self._detail)
        if not kw or kw == self._recommend_keyword:
            return
        self._recommend_keyword = kw
        task = _RecommendTask(self._search, self._source, kw)
        task.signals.finished.connect(self._on_recommend_done)
        self._recommend_task = task  # 防 GC
        QThreadPool.globalInstance().start(task)

    def _picks_from_related(self, rel: list) -> list:
        """detail.related → 推荐卡 mini 对象（剔除自身 + URL 去重，取前 8）。

        产物为 SimpleNamespace（title/url/cover/source_id），与搜索分支的
        _render_rec_cards/_on_rec_card 完全兼容；过滤后为空返回 []（走搜索）。
        """
        picks = []
        seen_urls = set()
        cur = (self._detail.url or "").rstrip("/").lower()
        sid = getattr(self._source, "source_id", "") or ""
        for item in rel:
            url = (item.get("url") or "").strip()
            if not url or url.rstrip("/").lower() == cur or url in seen_urls:
                continue
            seen_urls.add(url)
            picks.append(SimpleNamespace(
                title=item.get("title") or "",
                url=url,
                cover=item.get("cover") or "",
                source_id=sid,
            ))
            if len(picks) >= 8:
                break
        return picks

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

    def _probe_config(self) -> dict:
        if isinstance(self._source_probe_settings, dict):
            return self._source_probe_settings
        settings = getattr(self._content, "_settings", None)
        if settings is not None:
            try:
                from framework.settings_manager import media_source_probe_settings

                return media_source_probe_settings(settings)
            except Exception:
                pass
        return {
            "enabled": True,
            "max_sources": 5,
            "concurrency": 1,
            "timeout_ms": 30000,
        }

    @staticmethod
    def _probe_key(item) -> str:
        """线路/probe 结果的主键：有 detail_url 用它，否则用 sid。

        同一源的采集源线路（洛雪TV 的 jl 会有「自营·全22集」「自营·HD中字」
        等多条）sid 完全相同，只用 sid 当主键会把它们并成一条，检测结果互相
        覆盖、下拉框也切不过去。
        """
        if isinstance(item, dict):
            detail_url = str(item.get("detail_url") or "").strip()
            if detail_url:
                return detail_url
            return str(item.get("source_id") or item.get("sid") or "")
        return str(getattr(item, "source_id", "") or "")

    def _update_probe_button(self) -> None:
        enabled = bool(self._probe_config().get("enabled", True))
        # 两类线路都能检测：
        # - 无 detail_url 的（其它源的搜索结果/related_episodes 并入线路）
        # - 有 detail_url 的（同源采集源线路），检测方式是真取该线路详情再探
        #   一次分集地址——只看详情有没有数据不够，分集可能是死 CDN。
        available = bool(self._detail is not None and self._source_list)
        running = self._probe_task is not None
        self.probe_btn.setVisible(available)
        self.probe_btn.setEnabled(enabled and available and not running)


    # ------------------------------------------------------------------ #
    # 线路菜单延迟加载（不阻塞详情）
    # ------------------------------------------------------------------ #
    def _maybe_start_line_menu(self, detail) -> None:
        """详情没有线路菜单但标记了可拉取 → 后台补，不阻塞当前界面。"""
        self._cancel_line_menu_task()
        if detail is None or getattr(detail, "source_list", None):
            return
        if not getattr(detail, "line_menu_available", False):
            return
        if self._source is None or not callable(
            getattr(self._content, "fetch_line_menu", None)
        ):
            return
        generation = self._line_menu_generation + 1
        self._line_menu_generation = generation
        task = _FetchLineMenuTask(self._content, self._source, detail, generation)
        task.signals.finished.connect(self._on_line_menu_ready)
        self._line_menu_task = task  # 持引用，防止被 GC
        QThreadPool.globalInstance().start(task)

    def _cancel_line_menu_task(self) -> None:
        self._line_menu_generation += 1
        task = getattr(self, "_line_menu_task", None)
        self._line_menu_task = None
        if task is not None and hasattr(task, "cancel"):
            task.cancel()

    def _on_line_menu_ready(self, detail, chapters, generation) -> None:
        """后台线路菜单返回：仍是当前作品才应用（迟到结果直接丢弃）。"""
        if generation != self._line_menu_generation:
            return
        self._line_menu_task = None
        if detail is None or not chapters or detail is not self._detail:
            return
        if getattr(detail, "source_list", None):
            return  # 期间已经拿到别的线路列表（如搜索分组），不覆盖
        build = getattr(type(self._content), "line_menu_from_chapters", None)
        lines = build(chapters) if callable(build) else []
        if not lines:
            return
        detail.source_list = lines
        self._populate_source_combo(detail)

    def _line_episode_url(self, item: dict, current_episode_url: str) -> str:
        """在线路自带的分集里找「当前这一集」，返回该集 URL（找不到返回空）。

        先按标题匹配当前集（源站同一作品各线路的集名通常一致），回退按位置
        （当前集在第几集就取线路第几集）。线路没有自带分集时返回空，由调用方
        走原来的 sid= 推导。
        """
        episodes = item.get("episodes") or []
        if not isinstance(episodes, list) or not episodes:
            return ""
        cur_title = ""
        for i, ep in enumerate(getattr(self, "_episodes", None) or []):
            if getattr(ep, "url", "") == current_episode_url:
                cur_title = (getattr(ep, "title", "") or "").strip()
                for line_ep in episodes:
                    if isinstance(line_ep, dict) and cur_title and \
                            (line_ep.get("title") or "").strip() == cur_title:
                        return str(line_ep.get("url") or "")
                if 0 <= i < len(episodes) and isinstance(episodes[i], dict):
                    return str(episodes[i].get("url") or "")
                break
        first = episodes[0]
        return str(first.get("url") or "") if isinstance(first, dict) else ""

    def _probe_sources(self) -> None:
        config = self._probe_config()
        if not config.get("enabled", True) or self._detail is None:
            self._update_probe_button()
            return
        episode_url = getattr(self._detail, "url", "") or ""
        if 0 <= self._current_idx < len(self._episodes):
            episode_url = getattr(self._episodes[self._current_idx], "url", "") or episode_url
        episode_title = ""
        if 0 <= self._current_idx < len(self._episodes):
            episode_title = (getattr(self._episodes[self._current_idx], "title", "") or "").strip()
        candidates = []
        seen = set()
        for item in getattr(self._detail, "source_list", None) or []:
            source_id = str(
                (item.get("source_id") or item.get("sid"))
                if isinstance(item, dict) else getattr(item, "source_id", "")
            )
            if not source_id:
                continue
            key = self._probe_key(item)
            if not key or key in seen:
                continue
            seen.add(key)
            if isinstance(item, dict) and item.get("detail_url"):
                # 同源采集源线路：换线路＝用该线路自己的坐标重取详情，所以
                # 检测＝取它的详情 + 走播放同一条链路验一遍分集地址
                # （framework.probe_line_playable → 代理/清单/变体/首个分片）。
                # episode_index 与播放的映射口径一致：换线路后 App 按序号取同一集，
                # 所以探测也必须测同一集，只测首集会漏掉「这一集是死的」。
                candidates.append({
                    "source_id": source_id,
                    "key": key,
                    "source": self._source,
                    "detail_url": str(item.get("detail_url")),
                    "episode_title": episode_title,
                    "episode_index": int(self._current_idx),
                    "mode": "line",
                })
                continue
            source = item.get("source", self._source) if isinstance(item, dict) else self._source
            url = ""
            if isinstance(item, dict):
                url = item.get("url") or item.get("episode_url") or ""
                if not url:
                    # 线路自带的集（related_episodes 并入的其它线路）：用与当前集
                    # 同位置/同标题的那一集，比拼接 sid= 更贴近真实线路坐标
                    url = self._line_episode_url(item, episode_url)
            if not url:
                url = re.sub(r"([?&]sid=)[^&#]+", rf"\g<1>{source_id}", episode_url)
                if url == episode_url and source_id:
                    url = f"{episode_url}{'&' if '?' in episode_url else '?'}sid={source_id}"
            candidates.append({
                "source_id": source_id, "key": key, "source": source, "url": url,
            })
        if not candidates:
            self._update_probe_button()
            return
        limit = max(1, min(12, int(config.get("max_sources", 5) or 5)))
        # 当前正在看的线路先测：用户最关心「我现在这条能不能播」。
        current_key = ""
        for item in getattr(self._detail, "source_list", None) or []:
            if isinstance(item, dict) and str(item.get("detail_url") or "") == str(
                getattr(self._detail, "url", "") or ""
            ):
                current_key = self._probe_key(item)
                break
        if current_key:
            candidates.sort(key=lambda c: 0 if c.get("key") == current_key else 1)
        candidates = candidates[:limit]
        self._cancel_probe_task()
        self._probe_candidates = candidates
        self._probe_task = object()
        self._update_probe_button()
        self._start_probe_task(config, self._probe_generation)

    def _start_probe_task(self, config=None, generation=None) -> None:
        config = config or self._probe_config()
        generation = self._probe_generation if generation is None else generation
        task = _ProbeSourcesTask(
            self._content,
            getattr(self, "_probe_candidates", []),
            generation,
            max_sources=config.get("max_sources", 5),
            timeout_ms=config.get("timeout_ms", 30000),
            quality=self._quality,
        )
        task.signals.progress.connect(self._on_probe_progress)
        task.signals.finished.connect(self._on_sources_probed)
        self._probe_task = task
        QThreadPool.globalInstance().start(task)

    def _restore_probe_names(self) -> None:
        for i, item in enumerate(self._source_list):
            key = self._probe_key(item)
            sid = str(item.get("source_id") or item.get("sid") or "")
            name = self._probe_names.get(key) or self._probe_names.get(sid) or str(
                item.get("name") or f"源{sid}"
            )
            if i < self.source_combo.count():
                self.source_combo.setItemText(i, name)
                self.source_combo.setItemData(i, "", Qt.ToolTipRole)
            if i < len(self._source_actions):
                self._source_actions[i].setText(name)
                self._source_actions[i].setToolTip("")

    def _clear_probe_state(self) -> None:
        self._probe_results = {}
        self._probe_reasons = {}
        self.probe_btn.setText("检测播放源")
        self._restore_probe_names()

    def _on_probe_progress(self, number, total, source_id, task) -> None:
        if task is not self._probe_task:
            return
        name = self._probe_names.get(str(source_id), str(source_id))
        self.probe_btn.setText(f"第 {number}/{total}，当前源：{name}")

    def _on_sources_probed(self, results, generation, task) -> None:
        if task is None or task is not self._probe_task:
            return
        if generation != self._probe_generation:
            self._probe_task = None
            self._clear_probe_state()
            self.probe_btn.setText("检测播放源")
            self._update_probe_button()
            return
        self._probe_results = dict(results or {})
        self._probe_task = None
        reasons = dict(getattr(task, "reasons", None) or {})
        self._probe_reasons = reasons
        self.probe_btn.setText("检测播放源")
        ok_count = self._apply_probe_marks(reasons)
        if self._probe_results:
            # 检测完给一句总结：用户一眼能看出「有没有能播的线路」
            total = len(self._probe_results)
            self.probe_btn.setText(
                f"可播放 {ok_count}/{total}" if ok_count else "全部不可用"
            )
        self._update_probe_button()

    def _apply_probe_marks(self, reasons: dict | None = None) -> int:
        """把检测结果写进下拉框/播放器内菜单（文案 + 失败原因 tooltip）。

        返回可播放的线路数。`_mark_current_line_unplayable` 也用它——真实播放
        失败时把「可播放」改判成「不可用」，探测结论错了就让现实纠正它。
        """
        reasons = dict(self._probe_reasons if reasons is None else reasons)
        ok_count = 0
        for i, item in enumerate(self._source_list):
            key = self._probe_key(item)
            sid = str(item.get("source_id") or item.get("sid") or "")
            status = self._probe_results.get(key)
            if status is None and key != sid:
                status = self._probe_results.get(sid)
            base = self._probe_names.get(key) or self._probe_names.get(sid) or str(
                item.get("name") or f"源{sid}"
            )
            tip = ""
            if status == "ok":
                ok_count += 1
                text = f"{base}（可播放）"
                tip = "播放链路验证通过（清单 + 首个分片可达）"
            elif status == "timeout":
                text = f"{base}（超时）"
                tip = "检测超时：站点响应慢或该线路不可用"
            elif status in ("fail", "failed"):
                text = f"{base}（不可用）"
                reason = str(reasons.get(key) or reasons.get(sid) or "")
                tip = f"不可播放：{reason}" if reason else "不可播放"
            else:
                text = base
            if i < self.source_combo.count():
                self.source_combo.setItemText(i, text)
                self.source_combo.setItemData(i, tip, Qt.ToolTipRole)
            if i < len(self._source_actions):
                self._source_actions[i].setText(text)
                self._source_actions[i].setToolTip(tip)
        return ok_count

    def _mark_current_line_unplayable(self, reason: str = "") -> None:
        """真实播放失败 → 把当前线路从「可播放」改判「不可用」。

        探测只能证明「这一刻清单和分片都取得到」，播不出还有播放器/解码/代理
        重写等层。用户已经在播，这一层的失败是**最可信的判据**，所以把标记
        翻掉并留下原因，避免界面继续骗人。
        """
        if not self._source_list or not self._probe_results:
            return
        cur_url = str(getattr(self._detail, "url", "") or "")
        item = None
        for cand in self._source_list:
            if isinstance(cand, dict) and str(cand.get("detail_url") or "") == cur_url:
                item = cand
                break
        if item is None:
            return
        key = self._probe_key(item)
        if self._probe_results.get(key) != "ok":
            return
        self._probe_results[key] = "failed"
        text = str(reason or "播放失败")
        self._probe_reasons = dict(getattr(self, "_probe_reasons", None) or {})
        self._probe_reasons[key] = text
        ok_count = self._apply_probe_marks()
        total = len(self._probe_results)
        self.probe_btn.setText(
            f"可播放 {ok_count}/{total}" if ok_count else "全部不可用"
        )

    def _cancel_probe_task(self) -> None:
        self._probe_generation += 1
        task = self._probe_task
        self._probe_task = None
        if task is not None and hasattr(task, "cancel"):
            task.cancel()
        self._update_probe_button()

    def load(self, source, detail: Detail, start_ep_url: str = "", restore_position: float | None = None, restore_location: dict | None = None) -> None:
        self._cancel_probe_task()
        self._clear_probe_state()
        self._cancel_stream_tasks()
        self._invalidate_stream_cache()
        self._stop_cast()
        self._stream_gen += 1
        self._source = source
        self._detail = detail
        self._locked_source_id = ""
        self._pending_play = None  # 换书清掉旧暂存播放
        # 换作品：代数自增，旧系列（若有）的回调全部作废
        self._play_gen += 1
        self._has_played = False  # 未真正播放不把选集态误存为续读进度
        self._selection_mode = False  # 换书重置选集态
        self._tuner.reset()  # 新播放会话重置卡顿统计
        # 换书：无条件清零续读位置（旧书残留的 _pending_position 会被新书的
        # 首个选集/播放回调消费，把新书错 seek 到旧书位置）——与 novel/comic
        # 同模式修复跨书串位置 bug。
        self._pending_position = None
        self._pending_location = dict(restore_location or {})
        self._restore_episode_url = start_ep_url or detail.url
        if restore_position is not None:
            self._pending_position = restore_position
        # 换视频先停旧播放（不堆积缓存/后台占用）。
        self._stop_player()
        self._invalidate_current_stream()  # 与 reload_detail 同一不变量，防两处分叉
        self._episodes = detail.chapters
        self._stream_cache.clear()
        self._prefetch_idx = -2
        self._recommend_keyword = ""  # 换作品强制重搜推荐
        self._populate_source_combo(detail)
        self._populate_quality_combo(source)
        self._populate_ep_cards(detail.chapters)
        # 线路菜单在后台补（不阻塞选集/播放）：详情主链路不再为换线路多花 ~3s
        self._maybe_start_line_menu(detail)
        idx = 0
        if start_ep_url:
            for i, ep in enumerate(detail.chapters):
                if ep.url == start_ep_url:
                    idx = i
                    break
            else:
                # URL 全等没命中不等于「不是这一集」：换过线路的续读 URL 与本轮
                # 详情页的分集 URL 天然不同（…/play/1/11.html vs …/play/3/11.html），
                # 只比字符串会让续读集位回落到第 1 集。按同一集身份再找一次。
                for i, ep in enumerate(detail.chapters):
                    if _same_episode(start_ep_url, "", getattr(ep, "url", ""),
                                     getattr(ep, "title", ""),
                                     single=len(detail.chapters) == 1):
                        idx = i
                        break
        self._current_idx = idx if detail.chapters else -1
        self._update_cast_button()
        self._sync_overlay_state(playing=False)
        self._refresh_ep_menu()
        self._maybe_load_recommendations()
        if not detail.chapters:
            # 无分集（season 页）→ 直接取详情页播放地址自动播放
            self._show_status("正在获取播放流...")
            self._current_idx = -1
            self._detail_url_for_play = detail.url
            task = _FetchStreamTask(self._content, self._source, detail.url, self._quality,
                                    gen=self._stream_gen)
            task.signals.finished.connect(self._on_stream_loaded)
            self._stream_task = task
            QThreadPool.globalInstance().start(task)
            return
        if detail.chapters and restore_location and (
                detail.chapters[idx].url == start_ep_url
                or _same_episode(start_ep_url, "",
                                 getattr(detail.chapters[idx], "url", ""),
                                 getattr(detail.chapters[idx], "title", ""),
                                 single=len(detail.chapters) == 1)):
            self._load_episode(idx)
            return
        if detail.chapters:
            # 有分集：统一停在选集态，由用户明确选择后播放，单集也不例外。
            self._selection_mode = True
            self._paint_card_selection()
            self._show_status("请选择要播放的集数")
            return

    def reload_detail(self, new_detail: Detail) -> None:
        """换源重载分集后调用：多集源停选集态等用户选择，单集/无分集自动取流播放。"""
        self._cancel_probe_task()
        self._clear_probe_state()
        self._cancel_stream_tasks()
        self._invalidate_stream_cache()
        self._stop_cast()    # 换源 → 投屏会话一并收掉（旧源的流不能再推电视）
        self._stop_player()  # 换源先停旧播放流
        self._stream_gen += 1
        # 换源：代数自增。关键——多集源分支**不调用 _play**，若不在此自增，
        # 旧系列的 on_play 会带着仍然有效的代数回来，把旧源流写进新源的
        # _stream_cache（_load_episode 命中缓存不复验 → 持久播错集）。
        self._play_gen += 1
        self._detail = new_detail
        self._pending_position = None
        self._pending_location = None
        self._restore_episode_url = ""
        self._populate_source_combo(new_detail)
        self._episodes = new_detail.chapters
        self._stream_cache.clear()
        self._prefetch_idx = -2
        self._detail_url_for_play = ""
        self._recommend_keyword = ""  # 换源后按新源重搜推荐
        self._has_played = False  # 新源尚未播放，不把选集态误存为续读进度
        self._selection_mode = False  # 重置选集态
        # 「当前流」三元组一并作废：_stop_player() 刚停完，界面上摆的已是新源的集，
        # 留着 _current_play 的话它指的是**旧源**的流（见 _invalidate_current_stream）
        self._invalidate_current_stream()
        self._populate_ep_cards(new_detail.chapters)
        # 换过来的线路若还有「其它线路」可拉，同样后台补菜单（不阻塞换源）
        self._maybe_start_line_menu(new_detail)
        self._switching = False
        self._sync_overlay_state(playing=False)
        self._current_idx = 0 if new_detail.chapters else -1
        self._update_cast_button()
        self._refresh_ep_menu()
        self._maybe_load_recommendations()
        if not new_detail.chapters:
            # 无分集（season 页）→ 直接取详情页播放地址自动播放
            self._detail_url_for_play = new_detail.url
            self._show_status("正在获取播放流...")
            task = _FetchStreamTask(self._content, self._source, new_detail.url, self._quality,
                                    gen=self._stream_gen)
            task.signals.finished.connect(self._on_stream_loaded)
            self._stream_task = task
            QThreadPool.globalInstance().start(task)
            return
        if new_detail.chapters:
            # 有分集：统一停在选集态，由用户明确选择后播放，单集也不例外。
            self._selection_mode = True
            self._paint_card_selection()
            self._show_status("请选择要播放的集数")
            return

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
        """App 退出释放内嵌播放器与 播放器 实例。"""
        self._stop_cast()  # 退出 → 先停投屏轮询并 Stop/释放 DLNA 会话与 LAN 端点
        self._stop_progress_ticker()
        self._stop_prefetch_ticker()
        if self._player is not None:
            try:
                self._player.close()
            except Exception:  # noqa: BLE001 —— 句柄已失效：继续释放其余资源
                log.warning("关闭内嵌播放器失败", exc_info=True)
            self._player = None
        self._has_played = False
        self._unregister_series()

    def _stop_player(self) -> None:
        """停止当前视频播放并释放媒体/代理（保留播放器实例复用）。

        换视频/换源时调用，避免旧视频在后台继续拉流占用网络/CPU。
        内嵌播放器只 release（停网络、断代理、释放解码），实例留着复用。
        """
        self._stop_progress_ticker()
        if self._player is not None:
            try:
                self._player.release()
            except Exception:  # noqa: BLE001 —— 释放失败不阻断换集
                log.warning("内嵌播放器 release 失败", exc_info=True)
        self._has_played = False
        self._player_length_ms = 0
        try:
            self.progress._total_ms = 0
        except (RuntimeError, AttributeError):
            pass
        self._pending_seek_s = None
        self._pending_seek_ratio = None
        self.play_btn.setText("▶")
        self._stop_prefetch_ticker()
        self._sync_overlay_state(playing=False)
        self.buffer_spinner.hide()
        self.control_bar.show()  # 停播后未播放状态 → 控制条常驻
        self._hide_timer.stop()
        self._unregister_series()

    def stop_playback(self) -> None:
        """离开视频视图时释放资源：停播放 + 清空播放缓存（不堆积）。

        换小说/漫画/换另一部视频前由 ReaderPage 调用；全屏状态先退出，
        播放缓存与取流预拉全部清空，下次进入按需重新拉取。
        """
        if self._fs_win is not None:
            self._exit_fullscreen()
        self._play_gen += 1
        self._cancel_probe_task()
        self._clear_probe_state()
        self._cancel_stream_tasks()
        self._stream_gen += 1
        self._invalidate_stream_cache()
        self._stop_cast()  # 离开视频页 → 投屏会话清理（会话回调不会再来停本地播放）
        self._stop_player()
        self._current_play = ""
        self._detail_url_for_play = ""
        self._prefetch_idx = -2
        self._stream_cache.clear()
        self.progress.setValue(0)
        self.time_label.setText("00:00 / 00:00")
        self.play_label.hide()
        self.setCursor(Qt.ArrowCursor)

    def _invalidate_current_stream(self) -> None:
        """作废「当前流」三元组（换作品 `load` / 换源 `reload_detail` 两处共用）。

        这三个字段是「界面此刻放的是什么」的唯一记录，`_stop_player()` 不碰它们
        （它只负责停播）。换作品/换源后若留着，界面摆的是新的一集，
        `_current_play` 却还指着**上一个作品/源**的流 → ⚙外部播放器（无系列时
        的单集回退）、`▶_toggle_play_pause`、复制流地址三处都会把旧流当新一集
        交出去/复制出去（静默播错，App 里看不出来）。

        收成具名方法是为了让 `load()` 与 `reload_detail()` 两个 reset 块无法再
        分叉：今天两边都安全，只是因为 `reader_page` 恰好在 `load()` 之前调了
        `stop_playback()`（它会清 `_current_play`）；将来多出一条不经它的 `load()`
        路径（刷新按钮 / 重复 open）就会把这个 bug 静默放回来。
        """
        self._current_play = ""
        self._current_audio = ""
        self._current_title = ""

    # ------------------------------------------------------------------ #
    def _show_status(self, text: str) -> None:
        """状态浮层显示（加载/错误提示，覆盖在视频区内不占布局；成功后自动清空）。"""
        self._cast_status_text = text  # 投屏轮询用它去抖（每秒同文案不重排 UI）
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
        self._probe_names = {}
        for item in self._source_list:
            sid = str(
                (item.get("source_id") or item.get("sid"))
                if isinstance(item, dict) else getattr(item, "source_id", "")
            )
            name = str(
                (item.get("name") if isinstance(item, dict) else "")
                or f"源{sid}"
            )
            self._probe_names.setdefault(sid, name)
            self._probe_names[self._probe_key(item)] = name
        self._switching = False
        try:
            self.source_combo.currentIndexChanged.disconnect(self._on_source_switch)
        except (RuntimeError, TypeError):
            pass
        self.source_combo.clear()
        for s in self._source_list:
            sid = s.get("source_id") or s.get("sid")
            name = self._probe_names.get(self._probe_key(s)) or f"源{sid}"
            self.source_combo.addItem(name, sid)
        has = bool(self._source_list)
        self.source_label.setVisible(has)
        self.source_combo.setVisible(has)
        self._update_probe_button()
        if has:
            cur_sid = self._current_sid
            cur_url = str(getattr(detail, "url", "") or "")
            if not cur_sid:
                query = parse_qs(urlsplit(str(getattr(detail, "url", "") or "")).query)
                cur_sid = str((query.get("sid") or query.get("source") or [""])[0])
                self._current_sid = cur_sid
            # 先按「详情 URL 相同」定位（同一采集源常有多条同名线路，sid 一样），
            # 再回退按 sid 匹配第一条。
            idx = 0
            for i, s in enumerate(self._source_list):
                if cur_url and str(s.get("detail_url") or "") == cur_url:
                    idx = i
                    break
            else:
                for i, s in enumerate(self._source_list):
                    if str(s.get("source_id") or s.get("sid")) == str(cur_sid):
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
        if not sid:
            return
        item = self._source_list[idx] if 0 <= idx < len(self._source_list) else {}
        target_url = str(item.get("detail_url") or "") if isinstance(item, dict) else ""
        current_url = str(getattr(self._detail, "url", "") or "")
        # 同一采集源可能有多条同名线路（洛雪TV 的 jl），sid 相同但 detail_url
        # 不同：只比 sid 会「点了没反应」。sid 相同且目标 URL 就是当前 URL 才算
        # 原地不动。
        if str(sid) == str(self._current_sid) and (
            not target_url or target_url == current_url
        ):
            return
        self._current_sid = str(sid)
        self._locked_source_id = str(sid)
        self._switching = True
        self._play_gen += 1
        self._clear_probe_state()
        self._cancel_probe_task()
        self._populate_source_combo(self._detail)
        self.source_changed.emit((self._detail, str(sid), target_url))
        self._video_frame.setFocus()  # 换源后快捷键继续可用

    def _stream_cache_key(self, ep_url: str, quality: str):
        source_id = getattr(self._source, "source_id", "")
        if not source_id:
            return (ep_url, quality)
        return (source_id, ep_url, quality, True)

    def _prefetch_cache_get(self, key):
        value = self._prefetch_cache.get(key)
        if value is None:
            return None
        if len(value) == 2:
            return value
        expires_at, video, audio = value
        if expires_at <= time.monotonic():
            self._prefetch_cache.pop(key, None)
            return None
        return video, audio

    def _source_needs_merged(self) -> bool:
        raw = getattr(self._source, "raw", None) or {}
        episode = (raw.get("api_endpoints") or {}).get("episode") or {}
        return episode.get("engine") == "ytdlp"

    def _stream_cache_get(self, key):
        value = self._stream_cache.get(key)
        if value is None and len(key) == 4:
            value = self._stream_cache.get((key[1], key[2]))
        if value is None:
            return None
        if len(value) == 2:
            video, audio = value
            expires_at = time.monotonic() + self._stream_cache_ttl
        else:
            expires_at, video, audio = value
        if expires_at <= time.monotonic():
            self._stream_cache.pop(key, None)
            return None
        try:
            from framework.stream_resolver import StreamResolver

            if StreamResolver.cache_ttl_for_result(
                (video, audio), self._stream_cache_ttl
            ) <= 0:
                self._stream_cache.pop(key, None)
                return None
            resolver = getattr(self._content, "_stream_resolver", None)
            if resolver is not None and self._source is not None:
                ep_url = key[1] if len(key) == 4 else key[0]
                quality = key[2] if len(key) == 4 else key[1]
                if resolver.get_cached(self._source, ep_url, quality, True) != (video, audio):
                    self._stream_cache.pop(key, None)
                    return None
        except Exception:
            self._stream_cache.pop(key, None)
            return None
        return video, audio

    def _stream_cache_set(self, key, video, audio):
        try:
            from framework.stream_resolver import StreamResolver

            ttl = StreamResolver.cache_ttl_for_result(
                (video, audio), self._stream_cache_ttl
            )
        except Exception:
            ttl = 0.0
        if ttl <= 0:
            self._stream_cache.pop(key, None)
            return
        resolver = getattr(self._content, "_stream_resolver", None)
        if resolver is None:
            self._stream_cache[key] = (video, audio)
            return
        if len(key) >= 2 and self._source is not None:
            ep_url = key[1] if len(key) == 4 else key[0]
            quality = key[2] if len(key) == 4 else key[1]
            if resolver.get_cached(self._source, ep_url, quality, True) != (video, audio):
                self._stream_cache.pop(key, None)
                return
        self._stream_cache[key] = (
            time.monotonic() + ttl, video, audio
        )

    def _cancel_stream_tasks(self) -> None:
        for name in ("_stream_task", "_prefetch_task"):
            task = getattr(self, name, None)
            if task is not None:
                cancel = getattr(task, "cancel", None)
                if callable(cancel):
                    cancel()
                setattr(self, name, None)
        self._prefetch_idx = -2
        if hasattr(self, "_prefetch_attempted"):
            self._prefetch_attempted.clear()
        self._prefetch_retry_at.clear()
        self._prefetch_hopeless.clear()
        self._prefetch_state.clear()
        self._prefetch_cache.clear()

    def _invalidate_stream_cache(self, ep_url=None, quality=None) -> None:
        source = self._source
        if source is not None:
            invalidate = getattr(self._content, "invalidate_video_stream_cache", None)
            if callable(invalidate):
                invalidate(source, ep_url, quality, True)
        if ep_url is None and quality is None:
            self._stream_cache.clear()
            self._prefetch_cache.clear()
            self._prefetch_retry_at.clear()
            self._prefetch_hopeless.clear()
            self._prefetch_state.clear()
            return
        for key in list(self._stream_cache) + list(self._prefetch_cache):
            key_url = key[1] if len(key) == 4 else key[0]
            key_quality = key[2] if len(key) == 4 else key[1]
            if ep_url is not None and key_url != ep_url:
                continue
            if quality is not None and key_quality != quality:
                continue
            self._stream_cache.pop(key, None)
            self._prefetch_cache.pop(key, None)
            self._prefetch_retry_at.pop(key, None)
            self._prefetch_hopeless.discard(key)
            for latch_key in [k for k in self._prefetch_state if k[1] == key_quality]:
                idx = int(latch_key[0])
                if 0 <= idx < len(self._episodes) and self._episodes[idx].url == key_url:
                    self._prefetch_state.pop(latch_key, None)

    def _on_quality_changed(self, text: str) -> None:
        if not text or text == self._quality or self._current_idx < 0:
            self._quality = text or "best"
            return
        old_quality = self._quality
        self._quality = text
        # 清当前集播放缓存 → 新画质重拉
        ep = self._episodes[self._current_idx]
        self._cancel_stream_tasks()
        self._invalidate_stream_cache(ep.url, old_quality)
        self._invalidate_stream_cache(ep.url, text)
        for act in self._quality_actions:
            act.setChecked(act.text() == text)
        self._load_episode(self._current_idx)

    # ------------------------------------------------------------------ #
    def _load_episode(self, idx: int) -> None:
        if self._source is None or not (0 <= idx < len(self._episodes)):
            return
        self._selection_mode = False  # 真实选集 → 退出选集态
        if self._current_sid:
            self._locked_source_id = self._current_sid
        self._current_idx = idx
        self._paint_card_selection()  # 当前集卡片选中态跟随
        self._update_ep_check()  # 仅刷新勾选，不重建菜单（切集秒级响应）
        self._refresh_fs_title()  # 全屏顶栏标题跟随
        ep = self._episodes[idx]
        self.episode_changed.emit((self._detail, ep.title, ep.url))  # 进度记忆
        key = self._stream_cache_key(ep.url, self._quality)
        cached = self._stream_cache_get(key)
        if cached:
            video, audio = cached
            self._current_play = video
            self._play(video, audio, ep.title)
            return
        hint = None
        if not self._source_needs_merged():
            hint = self._prefetch_cache_get(key)
            self._prefetch_cache.pop(key, None)
        if hint:
            video, audio = hint
            self._current_play = video
            self._play(video, audio, ep.title)
            return
        self._show_status(f"正在获取播放流：{ep.title}...")
        self._current_play = ""
        task = _FetchStreamTask(
            self._content, self._source, ep.url, self._quality,
            gen=self._stream_gen, source_lock=bool(self._locked_source_id),
        )
        task.signals.finished.connect(self._on_stream_loaded)
        self._stream_task = task  # 防 GC
        QThreadPool.globalInstance().start(task)

    def _on_ep_card(self, card) -> None:
        """点分集卡片 → 选集加载。"""
        idx = card.p_data if isinstance(card.p_data, int) else -1
        if not (0 <= idx < len(self._episodes)):
            return
        self._select_episode(idx)

    def _on_prev_ep(self) -> None:
        """上一集（全屏/非全屏均可用）。"""
        idx = self._current_idx - 1
        if not (0 <= idx < len(self._episodes)):
            return
        if self._playlist_step(self._player, "playlist_prev"):
            return
        self._select_episode(idx)

    def _on_next_ep(self) -> None:
        """下一集（全屏/非全屏均可用）。

        外部播放器加载了播放列表时交给播放器自己前进（playlist-next）：
        该集的 /e/ 地址通常已被预取写进代理 memo，切集等于直接开播；
        其它播放器/无 IPC 时退回 App 取流播放。已是最后一集 → 提示。
        """
        idx = self._current_idx + 1
        if not (0 <= idx < len(self._episodes)):
            self._notify_last_episode()
            return
        if self._playlist_step(self._player, "playlist_next"):
            return
        self._select_episode(idx)

    @staticmethod
    def _playlist_step(player, method: str) -> bool:
        """让外部播放器在播放列表里前进/后退一项；不可用时返回 False。"""
        if player is None or not getattr(player, "has_playlist", False):
            return False
        step = getattr(player, method, None)
        if not callable(step):
            return False
        try:
            return bool(step())
        except Exception:  # noqa: BLE001 —— IPC 竞态：退回 App 取流
            log.debug("外部播放器切集失败（%s）", method, exc_info=True)
            return False

    def _on_speed_changed(self, text: str) -> None:
        """倍速（0.5x~2.0x，交给 播放器 set_rate）。"""
        try:
            rate = float(text.rstrip("x"))
        except Exception:  # noqa: BLE001
            return
        if self._player is not None:
            try:
                self._player.set_rate(rate)
            except Exception:  # noqa: BLE001 —— 播放器释放竞态
                log.debug("设置倍速失败（%.2fx）", rate, exc_info=True)

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

    def _on_stream_loaded(self, ep_url, video, audio, err, quality, gen=None) -> None:
        """取流完成：写缓存 →（仍为当前集/画质则）播放。

        旧任务后到（ep_url/quality 已非当前）：只写缓存，不覆盖播放。
        缓存 key 用发起时 quality，避免切画质后 key 错位（B3）。
        """
        if self._source is None or (gen is not None and gen != self._stream_gen):
            return
        if self._stream_task is not None:
            self._stream_task = None
        # 该集的前后端已在解析 → 预取那一路无事可做。不取消的话后端会再跑
        # 一次解析、写一次缓存，既白耗一次请求，也把「预取」变成「和播放
        # 抢同一个站点的请求」。取消后 _on_prefetch_done 不会再回调。
        self._cancel_prefetch_for(ep_url, quality)
        if err or not video:
            if self._is_current_stream(ep_url, quality):
                detail_text = _safe_error_text(err) if err else "无播放地址"
                # 取流失败同样是「这条线路播不了」的实证，翻掉「可播放」标记
                self._mark_current_line_unplayable(detail_text)
                self._show_status(f"获取播放流失败：{detail_text}")
            return
        self._stream_cache_set(self._stream_cache_key(ep_url, quality), video, audio)
        if not self._is_current_stream(ep_url, quality):
            return  # 旧任务/旧画质后到：不覆盖当前播放（B2）
        self._current_play = video
        title = ""
        if 0 <= self._current_idx < len(self._episodes):
            title = self._episodes[self._current_idx].title or ""
        self._request_play(video, audio, title)

    # ------------------------------------------------------------------ #
    def _request_play(self, video: str, audio: str, title: str) -> None:
        """请求播放：视图可见立即播；不可见（用户在别的 Tab）暂存，显示后再播。

        取流完成时若用户已切走（视频页隐藏），主线程直接 _play 会拉起播放器
        进程并在后台出声 → 切页卡顿且串音。暂存后由 _VideoFrame.showEvent
        在回到阅读页时补播。
        """
        if self.isVisible():
            self._play(video, audio, title)
            self._start_prefetch_ticker()
        else:
            self._pending_play = (video, audio, title)

    def _flush_pending_play(self) -> None:
        """视图重新可见时补播暂存的取流结果。"""
        if self._pending_play is not None:
            video, audio, title = self._pending_play
            self._pending_play = None
            self._play(video, audio, title)
            self._start_prefetch_ticker()

    # ------------------------------------------------------------------ #
    # 内嵌播放器（播放器 嵌入 _video_frame）
    # ------------------------------------------------------------------ #
    def _ensure_player(self, hdrs: dict | None = None):
        """取（必要时创建）播放后端实例（mpv + JSON IPC，见 player_session）。

        播放器是**长生命周期**对象：切集/换源只换 media（close + play），
        不重建进程——重建会丢缓存、丢窗口位置，代价远大于复用。
        只有 referer/UA 变化时才重建（防盗链头在进程启动参数里固化）。
        """
        from framework.player_session import PlayerSession

        referer = (hdrs or {}).get("Referer", "")
        ua = (hdrs or {}).get("User-Agent", "")
        player = self._player
        wrap_ok = (
            player is not None
            and getattr(player, "_referer", "") == referer
            and getattr(player, "_user_agent", "") == ua
        )
        if wrap_ok:
            return player
        if player is not None:
            try:
                player.close()
            except Exception:  # noqa: BLE001 —— 旧实例释放失败不阻断新播放
                log.warning("释放旧播放后端失败", exc_info=True)
            self._player = None
        player = PlayerSession(referer=referer, user_agent=ua)
        player.ended.connect(self._on_media_ended)
        player.error.connect(self._on_player_error)
        player.time_changed.connect(self._on_player_time)
        player.length_changed.connect(self._on_player_length)
        player.buffering.connect(self._on_player_buffering)
        # 状态信号是 ▶/⏸ 图标的唯一真相来源：点击只改一次，缓冲→播放、
        # 播放→暂停（播放器内/媒体键/自动暂停）、播放→结束都必须能刷新图标。
        # 连接失败不能拖垮播放器创建（历史上就是这类硬依赖导致「播放失败」）。
        state_sig = getattr(player, "state_changed", None)
        if state_sig is not None:
            try:
                state_sig.connect(self._on_player_state)
            except (RuntimeError, TypeError, AttributeError):
                pass
        self._player = player
        return player

    @staticmethod
    def _call_player_play(player, play_url: str, audio_url: str, title: str,
                          kind: str, caching_ms: int,
                          playlist=None, playlist_index: int = 0) -> None:
        """调用播放器的 play()，并把源级 network_caching / 播放列表传下去。

        旧签名/测试桩只接受 (url, audio, title, kind)：按实际接受的参数逐级
        降级，不能为一个增强参数把播放链路整体打断。
        """
        try:
            parameters = inspect.signature(player.play).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        kwargs = {}
        if accepts_kwargs or "network_caching_ms" in parameters:
            kwargs["network_caching_ms"] = caching_ms
        if playlist and (accepts_kwargs or "playlist" in parameters):
            kwargs["playlist"] = playlist
            kwargs["playlist_index"] = playlist_index
        player.play(play_url, audio_url, title, kind=kind, **kwargs)

    def _embedded_play_url(self, video: str, audio: str, hdrs: dict,
                           ad_block: dict) -> tuple[str, str]:
        """把上游流包成本地代理 URL（广告过滤 + 分片缓存 + 防盗链头）。

        内嵌播放器与原来的外部播放器走**同一条代理**：直连 CDN 对不少源
        会 403（校验 Host/Referer），而代理能补头；HLS 还需要代理侧的
        广告段剔除与分片预取。只有「非 HLS、无防盗链头、未强制代理」才直连。
        """
        is_hls = "m3u8" in (video or "").lower()
        needs_headers = any(
            str(k).lower() in {"referer", "cookie"} and v
            for k, v in (hdrs or {}).items()
        )
        if not (is_hls or needs_headers or self._force_proxy_enabled()):
            return video, audio
        try:
            from framework.media_proxy import MediaProxy, trusted_media_context

            proxy = MediaProxy.instance()
            kwargs = {"ad_block": ad_block,
                      "force_proxy": self._force_proxy_enabled()}
            sid = trusted_media_context(getattr(self._source, "source_id", ""))
            play = proxy.build_url(video, hdrs, source_id=sid, **kwargs)
            audio_url = (proxy.build_url(audio, hdrs, source_id=sid, **kwargs)
                         if audio else "")
            return play, audio_url
        except Exception:  # noqa: BLE001 —— 代理不可用：直连兜底
            log.warning("铸造本地代理 URL 失败，改用直连", exc_info=True)
            return video, audio


    def _ensure_series(self) -> str:
        """注册/复用惰性系列，供外部播放器的播放列表按集切流（失败返回 ""）。

        系列项是 ``/e/<key>/<idx>``：播放器点「下一集」时才请求它；预取已把
        该集的流地址写进代理 memo 时直接 302 开播，命中不了才现解析。
        """
        if self._source is None or not self._episodes or self._source_needs_merged():
            return ""
        if self._series_key and self._series_quality == self._quality:
            return self._series_key
        self._unregister_series()
        try:
            from framework.media_proxy import MediaProxy, trusted_media_context

            proxy = MediaProxy.instance()
            headers = {}
            request_headers = getattr(self._source, "request_headers", None)
            if callable(request_headers):
                headers = request_headers() or {}
            ad_block = dict((getattr(self._source, "raw", None) or {}).get("ad_block") or {})
            episodes = list(self._episodes)
            quality = self._quality
            content = self._content
            source = self._source

            def resolver(idx):
                episode = episodes[int(idx)]
                video, audio = content.fetch_video_streams(
                    source, episode.url, quality=quality, merged=True
                )
                return video, audio, dict(headers), dict(ad_block)

            key = proxy.register_series(
                resolver,
                # 代理线程回调：只 emit 信号，UI/进度都在主线程槽里更新
                on_play=lambda idx, video="", audio="": self._series_played.emit(int(idx)),
                count=len(episodes),
                force_proxy=self._force_proxy_enabled(),
                source_id=trusted_media_context(getattr(source, "source_id", "")),
            )
        except Exception:  # noqa: BLE001 —— 系列只是增强，失败退化单集播放
            log.warning("注册惰性系列失败（外部播放器无法切下一集）", exc_info=True)
            return ""
        self._series_key = key
        self._series_quality = quality
        return key

    def _playlist_entries(self, current_url: str, series_key: str) -> list:
        """整部作品的播放列表：当前集用已铸好的 /s/ 地址，其余走 /e/ 惰性系列。"""
        try:
            from framework.media_proxy import MediaProxy

            proxy = MediaProxy.instance()
        except Exception:  # noqa: BLE001 —— 代理不可用：不给列表
            return []
        entries = []
        for index, episode in enumerate(self._episodes):
            title = getattr(episode, "title", "") or f"第{index + 1}集"
            if index == self._current_idx:
                url = current_url
            else:
                url = proxy.series_episode_url(series_key, index)
            entries.append((url, title))
        return entries

    def _on_series_played(self, idx: int) -> None:
        """外部播放器切到某一集：只跟随当前集，不重新取流、不重开播放器。

        播放器请求 /e/ 时会话的流已经由代理给出，这里同步 UI/选集高亮与
        进度记忆的「当前集」标识；随后进度心跳会按新集继续驱动 4 分钟预取。
        """
        if not (0 <= idx < len(self._episodes)) or idx == self._current_idx:
            return
        self._selection_mode = False
        self._current_idx = idx
        self._last_pos_save_ts = 0.0
        episode = self._episodes[idx]
        # 播放器已经在解析这一集（/e/），同一集的预取再跑一遍只是重复请求
        self._cancel_prefetch_for(episode.url, self._quality)
        try:
            self._paint_card_selection()
            self._update_ep_check()
            self._refresh_fs_title()
        except (RuntimeError, AttributeError):
            pass
        try:
            self.episode_changed.emit((self._detail, episode.title, episode.url))
        except RuntimeError:
            pass

    def _play(self, video: str, audio: str, title: str) -> None:
        """播放当前集：把代理地址交给播放后端（mpv + IPC，见 player_session）。

        画面在播放器自己的窗口里（VLC 内嵌方案已下线），但进度/暂停/seek/
        音量/倍速经 JSON IPC 回流，所以界面控件与进度回写照旧可用。
        """
        self._current_play = video
        self._current_audio = audio
        self._current_title = title
        self._play_gen += 1
        # 起播剖面（配合 media_proxy 的 [起播] 清单→首片）：取流已在本函数
        # 之前完成，这里开始计"播放器起播段"，首帧到达时打一条日志。
        self._play_started_at = time.monotonic()
        self._first_frame_at = None
        hdrs = {}
        _rh = getattr(self._source, "request_headers", None)
        if callable(_rh):
            hdrs = _rh() or {}
        # ikanpp 这类按连接限速的源：播放前按源配置开启有限 HLS 预取，
        # 让代理提前落盘后续分片，避免播放器在下一片到达前反复卡住。
        self._configure_playback_prefetch(video)
        ad_block = {}
        try:
            ad_block = dict((self._source.raw or {}).get("ad_block") or {})
        except Exception:  # noqa: BLE001
            ad_block = {}

        # 续播位置在开播时取一次（_progress_options 有消费即清的语义）
        try:
            options = self._progress_options()
        except Exception:  # noqa: BLE001 —— 进度读失败不能阻断播放
            log.warning("读取续播位置失败", exc_info=True)
            options = {}
        self._progress_cb = options.get("on_progress")
        resume = (options.get("resume_positions") or {}).get(self._current_idx) or {}
        self._pending_seek_s = None
        if isinstance(resume.get("seconds"), (int, float)) and resume["seconds"] > 0:
            self._pending_seek_s = float(resume["seconds"])
        elif isinstance(resume.get("position"), (int, float)) and 0 < resume["position"] < 1:
            self._pending_seek_ratio = float(resume["position"])
        self._player_length_ms = 0
        try:
            self.progress._total_ms = 0
        except (RuntimeError, AttributeError):
            pass
        self._last_pos_save_ts = 0.0

        if not video:
            self._show_status("没有可播放的地址")
            self._sync_overlay_state(playing=False)
            return
        play_url, audio_url = self._embedded_play_url(video, audio, hdrs, ad_block)
        # 外部播放器「下一集」：把整部作品作为播放列表交给播放器，后续集指向
        # 媒体代理的 /e/<key>/<idx>。切集时预取过的集直接命中 memo（302 秒开），
        # 播放器不重启进程，也不经过 App 的现场取流。
        playlist = ()
        playlist_index = 0
        if (self._episodes and not audio_url
                and 0 <= self._current_idx < len(self._episodes)):
            series_key = self._ensure_series()
            if series_key:
                playlist = self._playlist_entries(play_url, series_key)
                playlist_index = self._current_idx
        # 媒体类型提示必须按**上游原始 URL** 判：包成代理 URL 后扩展名线索
        # 就没了（classify 会落到 unknown 档，缓冲偏小 → HLS 频繁卡）。
        kind = "hls" if "m3u8" in (video or "").lower() else (
            "mp4" if ".mp4" in (video or "").lower() else "")
        # 源级 network_caching_ms 下发给播放器：HLS 缺省 5000ms 对按连接限速
        # 的 CDN 太浅，缓冲一见底就重缓冲（用户可见的「频繁跳帧/卡一下」）。
        caching_ms = self._source_network_caching_ms()
        # 新播放会话：清掉上一集的卡顿统计，并立刻显示 0% 的加载条
        # （缓冲事件要等首片到达才来，之前这段是纯黑屏无反馈）。
        try:
            self._tuner.reset()
        except Exception:  # noqa: BLE001
            pass
        # 新会话：清空缓冲/状态去重缓存，否则上一集的 100% 会让首片缓冲事件被吞掉
        self._last_buffer_pct = -1
        self._last_player_state = -1
        try:
            self._show_buffer_indicator(0)
        except Exception:  # noqa: BLE001
            pass
        try:
            player = self._ensure_player(hdrs)
            self._call_player_play(player, play_url, audio_url, title, kind, caching_ms,
                                   playlist, playlist_index)
        except Exception as exc:  # noqa: BLE001 —— mpv 缺失/拉起失败
            # 必须落日志：只弹 UI 的话现场完全无法定位（历史上一次「播放失败」
            # 就是这样查不到痕迹的）。
            log.warning("播放器起播失败：%s", exc, exc_info=True)
            self._has_played = False
            self._show_status(f"播放失败：{_safe_error_text(exc)}")
            self._sync_overlay_state(playing=False)
            self._stop_progress_ticker()
            self._stop_prefetch_ticker()
            return

        # 播放器已接受 media：允许回写进度（此前的闸门是「进程真的拉起了」，
        # 内嵌等价于「play() 没抛异常」）。
        self._has_played = True
        self._enable_embedded_controls()
        self.control_bar.show()
        self._sync_overlay_state(playing=True)  # 全屏才启动 5s 空闲隐藏
        self._start_progress_ticker()
        self._start_prefetch_ticker()
        self._notify_last_episode()

    # ------------------------------------------------------------------ #
    # 内嵌播放器事件 → 进度/预取/自动续集
    # ------------------------------------------------------------------ #
    def _on_player_length(self, length_ms: int) -> None:
        """拿到时长：执行挂起的续播 seek。"""
        try:
            self._player_length_ms = int(length_ms or 0)
        except (TypeError, ValueError):
            self._player_length_ms = 0
        # 同步给进度条 hover 时间提示（否则 hover 只显示百分比）
        try:
            self.progress._total_ms = self._player_length_ms
        except (RuntimeError, AttributeError):
            pass
        if self._player_length_ms <= 0 or self._player is None:
            return
        target = self._pending_seek_s
        if target is None and getattr(self, "_pending_seek_ratio", None):
            target = self._pending_seek_ratio * (self._player_length_ms / 1000.0)
        self._pending_seek_s = None
        self._pending_seek_ratio = None
        if not target or target <= 0:
            return
        duration_s = self._player_length_ms / 1000.0
        if target >= duration_s - 1.0:
            return  # 已到片尾：从头播比跳到末尾更符合预期
        try:
            self._player.set_position(target / duration_s)
        except Exception:  # noqa: BLE001 —— seek 失败不影响正常播放
            log.warning("续播 seek 失败（目标 %.1fs）", target, exc_info=True)

    def _on_player_time(self, time_ms: int) -> None:
        """播放进度心跳：驱动进度回写（节流）+ 预取时基（不节流）。"""
        # 首帧耗时：与代理侧 [起播] 那两条日志拼起来，就能分清"慢在取流/
        # 清单/首片还是慢在播放器起播"，不用再靠猜。
        if time_ms > 0 and self._first_frame_at is None:
            self._first_frame_at = time.monotonic()
            if self._play_started_at:
                log.info("[起播] 首帧 %.2fs（播放器起播段）",
                         self._first_frame_at - self._play_started_at)
        duration_ms = self._player_length_ms
        if duration_ms <= 0 and self._player is not None:
            # LengthChanged 事件可能缺席（部分容器只在首帧后才报长度）：
            # 时间事件到达时就地补读一次，否则进度与预取都会整段失效。
            try:
                duration_ms = int(self._player.get_length() or 0)
            except Exception:  # noqa: BLE001 —— 播放器释放竞态
                duration_ms = 0
            if duration_ms > 0:
                self._player_length_ms = duration_ms
                try:
                    self.progress._total_ms = duration_ms
                except (RuntimeError, AttributeError):
                    pass
        if duration_ms <= 0 or not (0 <= time_ms <= duration_ms):
            return
        seconds = time_ms / 1000.0
        duration = duration_ms / 1000.0
        idx = self._current_idx
        # 进度条/时间文本跟随（用户正在拖动时不抢他的手）
        if not self._cast_active and not self.progress.isSliderDown():
            try:
                self.progress.blockSignals(True)
                self.progress.setValue(int(time_ms * 1000 / duration_ms))
                self.progress.blockSignals(False)
            except (RuntimeError, TypeError, ZeroDivisionError):
                pass
            self.time_label.setText(f"{_fmt_hms(seconds)} / {_fmt_hms(duration)}")
        now = time.monotonic()
        throttle = float(self.POSITION_SAVE_INTERVAL_S)
        if self._progress_cb is not None and now - self._last_pos_save_ts >= throttle:
            self._last_pos_save_ts = now
            try:
                self._progress_cb(idx, {"seconds": seconds, "duration": duration})
                return  # 回写内部已 emit _progress_position
            except Exception:  # noqa: BLE001 —— 回写失败仍要驱动预取
                log.warning("播放进度回写失败（第 %s 集）", idx, exc_info=True)
        # 只驱动预取判定：锁存保证同一位置不会重复取流，故不必节流。
        try:
            self._progress_position.emit(int(idx), float(seconds), float(duration))
        except (RuntimeError, TypeError):
            pass

    # 播放器状态码（与 framework.player_session 的 ST_* 常量一致）
    _PLAYER_BUFFERING = 2
    _PLAYER_PLAYING = 3
    _PLAYER_PAUSED = 4
    # 去重缓存：播放器 的 buffering 事件是按「缓存填充每变一点」发的，起播阶段
    # 一秒能发几十上百次；每次都重排覆盖层 = UI 线程被自己的重绘淹没
    # （现场表现就是「进播放界面/按按键异常卡顿」）。
    _last_buffer_pct = -1
    _last_player_state = -1

    @staticmethod
    def _normalize_buffer_percent(value) -> int:
        """把 播放器 的缓冲百分比归一成 0~100；无法解析返回 -1。

        口径不统一是实测存在的：播放器 3.x 的 MediaPlayerBuffering 事件里
        `new_cache` 多数构建是 0~100 的百分比，但也有构建给 0~1 的浮点比例。
        只按其中一种解读，另一种就会表现为「进度条永远停在 1%」或「一开始
        就满格」。这里按数值大小判别：<=1 视为比例，其余视为百分比。
        """
        try:
            v = float(value)
        except (TypeError, ValueError):
            return -1
        if v <= 0:
            return 0
        if v <= 1.0:
            v *= 100.0
        return max(0, min(100, int(round(v))))

    def _center_buffer_bar(self) -> None:
        """只把加载条摆到画面正中。

        刻意不调用 `_reposition_overlays()`：那里有 `help_overlay.adjustSize()`，
        会触发整层布局重算；缓冲事件一秒几十次时这条路径本身就是卡顿源。
        """
        bar = getattr(self, "buffer_spinner", None)
        if bar is None:
            return
        frame = self._video_frame
        try:
            w, h = frame.width(), frame.height()
        except (RuntimeError, AttributeError):
            return
        if w <= 0 or h <= 0:
            return
        bar.move((w - bar.width()) // 2, max(0, (h - bar.height()) // 2))

    def _show_buffer_indicator(self, percent: int = -1) -> None:
        """显示/更新画面中央的加载进度条（percent<0 表示只显示不改数值）。

        幂等 + 去重：值没变就只重绘不重排，已可见就不再 show/raise。
        """
        bar = getattr(self, "buffer_spinner", None)
        if bar is None:
            return
        try:
            value = bar.value() if percent < 0 else max(0, min(100, int(percent)))
            hidden = bar.isHidden()
            if value != bar.value():
                bar.setValue(value)
            # format() 返回的是模板串（"缓冲中 %p%"），比较模板而不是渲染文本
            fmt = "缓冲中 %p%" if value > 0 else "载入中…"
            if bar.format() != fmt:
                bar.setFormat(fmt)
            if hidden:
                self._center_buffer_bar()
                bar.show()
                bar.raise_()
        except (RuntimeError, AttributeError):
            return
        # 缓冲不是「暂停」：中央播放键必须保持隐藏。之前这里走的是
        # _sync_overlay_state(playing=False)，于是缓冲时中央按钮与进度条
        # 叠在一起，看起来像「播放被停了」。
        try:
            if not self.center_play_btn.isHidden():
                self.center_play_btn.hide()
        except (RuntimeError, AttributeError):
            pass
        try:
            self._hide_timer.stop()
        except (RuntimeError, AttributeError):
            pass

    def _hide_buffer_indicator(self) -> None:
        """收起加载进度条，并同步覆盖层到当前真实播放状态。"""
        bar = getattr(self, "buffer_spinner", None)
        if bar is not None:
            try:
                if not bar.isHidden():
                    bar.hide()
            except RuntimeError:
                pass
        self._last_buffer_pct = -1
        self._sync_overlay_state(playing=self._player_is_active())

    def _player_state(self) -> int:
        """读一次 播放器 状态码（-1 = 未知/播放器未就绪）。"""
        player = self._player
        if player is None:
            return -1
        getter = getattr(player, "get_state", None)
        if not callable(getter):
            return -1
        try:
            return int(getter())
        except Exception:  # noqa: BLE001 —— 播放器释放竞态
            return -1

    def _player_is_active(self) -> bool:
        """UI 口径的「正在播」：播放中与缓冲中都算。

        `is_playing()` 只有 Playing 状态为真——缓冲、起播、seek 后
        的重缓冲期间都是 False。图标若直接绑它，缓冲时就会把 ⏸ 翻成 ▶，
        用户点了播放却看到「播放」图标，正是「图标显示不正确」的根因。
        """
        player = self._player
        if player is None:
            return False
        probe = getattr(player, "is_active", None)
        if callable(probe):
            try:
                return bool(probe())
            except Exception:  # noqa: BLE001
                return False
        state = self._player_state()
        if state >= 0:
            return state in (self._PLAYER_BUFFERING, self._PLAYER_PLAYING)
        try:
            return bool(player.is_playing())
        except Exception:  # noqa: BLE001
            return False

    def _on_player_buffering(self, percent: int) -> None:
        """缓冲进度 → 画面中央的加载进度条（显示真实百分比）。"""
        pct = self._normalize_buffer_percent(percent)
        if pct < 0:
            return
        # 去重：播放器 起播阶段会连发几十上百条 buffering 事件（值常常重复），
        # 逐条刷新控件和卡顿统计就是纯浪费 —— 同值直接丢，UI 只跟变化走。
        if pct == self._last_buffer_pct:
            return
        self._last_buffer_pct = pct
        # 喂给自适应缓冲器：此前 MediaTuner 只被创建/重置、从没接收过缓冲
        # 事件，于是「卡顿 3 次自动加大缓冲」这条自愈链路一次都没生效过。
        try:
            self._tuner.on_buffering(pct)
        except Exception:  # noqa: BLE001 —— 统计失败不能影响播放
            pass
        try:
            if pct >= 100:
                self._hide_buffer_indicator()
            else:
                self._show_buffer_indicator(pct)
        except Exception:  # noqa: BLE001 —— 覆盖层异常不影响播放
            pass

    def _on_player_state(self, state: int) -> None:
        """播放器状态迁移 → 播放/暂停图标 + 加载进度条的唯一真相来源。"""
        try:
            st = int(state)
        except (TypeError, ValueError):
            return
        # 同一状态重复到达（播放器 会重复发）不必重排覆盖层
        if st == self._last_player_state:
            return
        self._last_player_state = st
        try:
            if st == self._PLAYER_PLAYING:
                self._hide_buffer_indicator()
                self._sync_overlay_state(playing=True)
            elif st == self._PLAYER_BUFFERING:
                # 缓冲中：进度条在，中央播放键不在，图标保持「播放中」
                self._show_buffer_indicator(-1)
                self._sync_overlay_state(playing=True)
            elif st == self._PLAYER_PAUSED:
                self._hide_buffer_indicator()
                self._sync_overlay_state(playing=False)
            elif st in (0, 1):          # Nothing / Opening：还在起播
                self._show_buffer_indicator(-1)
                if not self.center_play_btn.isHidden():
                    self.center_play_btn.hide()
            else:                        # Stopped / Ended / Error
                self._hide_buffer_indicator()
                self._sync_overlay_state(playing=False)
        except (RuntimeError, AttributeError):
            pass
        self._update_play_button()

    def _on_player_error(self, message: str) -> None:
        """播放器报错：提示并允许重试（不静默黑屏）。"""
        log.warning("内嵌播放器报错：%s", message)
        # 真实播放失败＝最可信的判据：把这条线路的「可播放」改判「不可用」，
        # 否则界面继续标着「可播放」而用户点了就是黑屏。
        self._mark_current_line_unplayable(_safe_error_text(message))
        self._show_status(f"播放出错：{_safe_error_text(message)}")
        self._sync_overlay_state(playing=False)
        self._stop_progress_ticker()

    def _on_media_ended(self) -> None:
        """当前集播完 → 自动下一集（末集则提示）。"""
        if self._cast_active:
            return
        player = self._player
        if (player is not None and getattr(player, "has_playlist", False)
                and 0 <= self._current_idx < len(self._episodes) - 1):
            # 外部播放器自己会前进到列表下一项（请求 /e/ 预取地址）：
            # 这里再调一次下一集就会跳过一集，交给播放器。
            return
        if 0 <= self._current_idx < len(self._episodes) - 1:
            self._stop_progress_ticker()
            self._select_episode(self._current_idx + 1)
            return
        self._notify_last_episode()
        self._sync_overlay_state(playing=False)

    def _start_progress_ticker(self) -> None:
        """进度兜底轮询（播放器 个别格式不发 TimeChanged 事件）。"""
        timer = getattr(self, "_progress_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(int(self.PROGRESS_POLL_MS))
            timer.timeout.connect(self._poll_progress)
            self._progress_timer = timer
        timer.start()

    def _stop_progress_ticker(self) -> None:
        timer = getattr(self, "_progress_timer", None)
        if timer is not None:
            timer.stop()

    def _poll_progress(self) -> None:
        """定时从 播放器 读一次时间（事件缺失时的兜底）。"""
        player = self._player
        if player is None or not self._has_played:
            return
        # 状态兜底对账：个别封装/格式不投递状态事件，每秒校一次图标，
        # 保证 ▶/⏸ 不会长期停在错误的一面。
        self._update_play_button()
        try:
            if not self._player_is_active():
                return
            time_ms = int(player.get_time() or 0)
            length_ms = int(player.get_length() or 0)
        except Exception:  # noqa: BLE001 —— 播放器释放竞态
            return
        # 时间已经在推进 = 首帧已出：加载条必须收起（缓冲事件缺失时的兜底）
        if time_ms > 0:
            try:
                if self.buffer_spinner.isVisible():
                    self._hide_buffer_indicator()
            except (RuntimeError, AttributeError):
                pass
        if length_ms > 0 and length_ms != self._player_length_ms:
            self._on_player_length(length_ms)
        if time_ms > 0:
            self._on_player_time(time_ms)

    def _source_cast_tuning(self) -> dict:
        try:
            media = (getattr(self._source, "raw", None) or {}).get("media") or {}
            tuning = media.get("cast") or {}
            return dict(tuning) if isinstance(tuning, dict) else {}
        except Exception:
            return {}

    def _source_hls_prefetch(self) -> dict:
        """读取本地播放用的源级 HLS 预取配置。"""
        try:
            media = (getattr(self._source, "raw", None) or {}).get("media") or {}
            hls = media.get("hls") or {}
            prefetch = hls.get("prefetch") or {}
            return dict(prefetch) if isinstance(prefetch, dict) else {}
        except Exception:
            return {}

    def _configure_playback_prefetch(self, video_url: str = "") -> None:
        """按源配置 HLS 预取；未配置的 m3u8 使用保守通用默认值。

        大多数源不会在 JSON 中重复声明同一套抗抖参数，但实际返回的
        播放地址仍可能是 HLS。只对真实 m3u8 自动启用，非 HLS 不增加请求。
        源显式写入 ``prefetch`` 时优先尊重源配置。
        """
        try:
            from framework.media_proxy import MediaProxy

            configure = getattr(MediaProxy.instance(), "configure_prefetch", None)
            if not callable(configure):
                return
            raw = (getattr(self._source, "raw", None) or {})
            media = raw.get("media") or {}
            hls = media.get("hls") or {}
            declared = isinstance(hls, dict) and isinstance(
                hls.get("prefetch"), dict)
            # 源显式声明 prefetch 即视为「按 HLS 调优」：有的源 format 写的是
            # mp4（如 agedm，实际播放地址仍是 m3u8 或短链），只靠 URL 里的
            # "m3u8" 猜会让声明好的源级参数静默失效。
            is_hls = (
                declared
                or "m3u8" in str(video_url or "").lower()
                or str(media.get("format") or "").lower() in {"hls", "m3u8"}
            )
            if (is_hls and declared):
                prefetch = dict(hls["prefetch"])
            elif is_hls:
                # 未声明 prefetch 的 HLS 源：前瞻 10 片、3 路并发。
                # 旧默认 4 片/2 路是按「小分片、低延迟」估的，遇到 2MB 级
                # 分片 + 上游偶发数秒抖动（18mh 实测单片 4.7s）时缓冲两下
                # 就耗尽，表现为播放卡顿。10 片 ≈ 一分多钟缓冲，仍远低于
                # 源侧上限（_PREFETCH_MAX_DEPTH=64），不会放大请求。
                prefetch = {"enabled": True, "depth": 10, "workers": 3}
            else:
                prefetch = {"enabled": False}
            configure(
                bool(prefetch.get("enabled", False)),
                depth=prefetch.get("depth", 10),
                workers=prefetch.get("workers", 3),
            )
        except Exception:
            # 预取只是抗抖动增强，配置异常不能阻断正常播放。
            return

    def _configure_cast_tuning(self) -> None:
        from framework.media_proxy import MediaProxy, trusted_media_context
        configure = getattr(MediaProxy.instance(), "configure_prefetch", None)
        if not callable(configure):
            return
        prefetch = self._source_cast_tuning().get("prefetch") or {}
        configure(
            bool(prefetch.get("enabled", False)),
            depth=prefetch.get("depth", 4),
            workers=prefetch.get("workers", 3),
        )

    def _force_proxy_enabled(self) -> bool:
        """源 media.proxy_mode == force_proxy → 播放代理强制走系统代理。

        hanime1 的 CDN 直连不稳（0.3~1MB/s 波动）而系统代理稳定
        （1.8~2.6MB/s）；force_proxy 由播放后端（PlayerSession）透传主
        MRL/音频轨/分集列表每一集。
        """
        try:
            media_cfg = (getattr(self._source, "raw", None) or {}).get("media") or {}
            return media_cfg.get("proxy_mode") == "force_proxy"
        except Exception:  # noqa: BLE001
            return False

    def _notify_last_episode(self) -> None:
        """「已是最后一集」提示：非末集静默；去抖 3s 防连点/播放连开重复弹。"""
        if self._episodes:
            if 0 <= self._current_idx < len(self._episodes) - 1:
                return  # 还有下一集 → 非末集静默
        # 无分集（season 页单集）或处于末集 → 提示
        now = time.monotonic()
        if now - self._last_ep_toast_ts < 3.0:
            return
        self._last_ep_toast_ts = now
        self._show_status("已是最后一集")

    def _unregister_series(self) -> None:
        """注销惰性系列（_series_key 为空 → 幂等无操作）。

        内嵌播放不走 /e/ 惰性系列，实际几乎总是空操作；保留它是为了让
        投屏/历史路径一旦注册过也能被正确回收。
        """
        if not self._series_key:
            self._series_quality = ""
            return
        key, self._series_key = self._series_key, ""
        self._series_quality = ""
        self._series_urls = []
        try:
            from framework.media_proxy import MediaProxy

            MediaProxy.instance().unregister_series(key)
        except Exception:  # noqa: BLE001 —— 代理不可用：本地状态已清
            log.debug("惰性系列 %s 注销失败（代理不可用）", key, exc_info=True)

    def _retry_play(self) -> None:
        """刷新播放：清当前集取流缓存后重新取流播放（播放失败后的重试入口）。

        分集源重拉当前集；无分集（season 页）重拉详情 URL。
        """
        if self._source is None:
            return
        self._cancel_stream_tasks()
        if 0 <= self._current_idx < len(self._episodes):
            ep = self._episodes[self._current_idx]
            self._invalidate_stream_cache(ep.url, self._quality)
            self._current_play = ""
            self._show_status(f"正在重新获取播放流：{ep.title}...")
            self._load_episode(self._current_idx)
        elif self._detail_url_for_play:
            # season 页：直接重拉详情 URL 的播放流
            self._current_play = ""
            self._show_status("正在重新获取播放流...")
            task = _FetchStreamTask(
                self._content, self._source, self._detail_url_for_play, self._quality,
                gen=self._stream_gen,
            )
            task.signals.finished.connect(self._on_stream_loaded)
            self._stream_task = task
            QThreadPool.globalInstance().start(task)

    # ------------------------------------------------------------------ #
    # 投屏（DLNA）：📺 入口 / 启停 / 双向控制 / 后台轮询状态同步
    # ------------------------------------------------------------------ #
    def _update_cast_button(self) -> None:
        """刷新投屏按钮的可用性与悬停文案（未投屏「投屏」/ 投屏中「停止投屏」）。"""
        btn = getattr(self, "cast_btn", None)
        if btn is None:  # 构建控制条前（__init__ 早期）可能被调
            return
        has_source = self._source is not None
        has_episode = 0 <= self._current_idx < len(self._episodes)
        if self._cast_session is not None or self._cast_active:
            # 投屏中始终可点（这是「停止投屏」入口），并展示设备名
            tip = "停止投屏"
            if self._cast_device:
                tip += f" - {self._cast_device}"
            btn.setEnabled(True)
            btn.setToolTip(tip)
            return
        if self._cast_error:
            # 上次失败 → 悬停可见原因；仍可点（点击即重试）
            btn.setEnabled(has_source and has_episode)
            btn.setToolTip(f"投屏失败：{self._cast_error}")
            return
        busy = self._cast_state in ("preparing_stream", "connecting")
        btn.setEnabled(has_source and has_episode and not busy)
        btn.setToolTip("正在投屏..." if busy else "投屏")

    def _on_cast_clicked(self) -> None:
        """📺 按钮：投屏中 → 停止投屏；否则启动投屏。"""
        if self._cast_session is not None or self._cast_active:
            self._stop_cast()
            return
        self._begin_cast()

    # ---- 启动：取流 → 选设备 → CastSession.start ---- #
    def _begin_cast(self) -> None:
        """投屏第一步：解析当前集播放流（缓存命中不重复取流）。"""
        if self._source is None or not (0 <= self._current_idx < len(self._episodes)):
            return
        if self._cast_state in ("preparing_stream", "connecting"):
            return  # 防连点
        if self._cast_start_task is not None:
            # 上一次 start 已被 _stop_cast 作废但仍在后台跑：等它在槽里收尾。
            # 其 session.stop() 与新 start 都碰 MediaProxy 单例 LAN 端点，
            # 必须串行——代数守卫保证旧的先停完，新的才能提交。
            self._show_status("上一次投屏启动尚未结束，请稍候...")
            return
        self._cast_state = "preparing_stream"
        self._cast_error = ""  # 重试入口：清掉上次失败，按钮文案回到「正在投屏...」
        self._cast_direct = False  # 重新起投：由 _cast_pick_device 按历史重判
        self._update_cast_button()
        self._cast_resolve_stream(self._current_idx, "start")

    def _cast_resolve_stream(self, idx: int, kind: str) -> None:
        """取第 idx 集播放流；命中 _stream_cache 直接续接，否则复用 _FetchStreamTask。

        先自增投屏代数：在途的上一个取流任务从此刻起视为陈旧，其回调不得
        再消费/覆盖本次的 _cast_stream_pending——否则旧任务后到会把新 pending
        吞掉（两个任务都完成后不再有回调，卡死 preparing_stream、按钮禁用）。
        """
        self._cast_gen += 1
        gen = self._cast_gen
        ep = self._episodes[idx]
        key = self._stream_cache_key(ep.url, self._quality)
        cached = self._stream_cache_get(key)
        if cached:
            self._cast_stream_arrived(idx, kind, cached[0])
            return
        self._cast_stream_pending = (idx, kind)
        self._show_status(f"正在获取播放流：{ep.title}...")
        task = _FetchStreamTask(self._content, self._source, ep.url,
                                self._quality, gen=gen)
        # 连**方法**（非 lambda）：接收方是本视图（主线程亲和），跨线程 emit
        # 才会被排队投递到主线程；连 lambda 则线程归属不确定。
        task.signals.finished.connect(self._on_cast_stream_ready)
        self._cast_stream_task = task  # 防 GC
        QThreadPool.globalInstance().start(task)

    def _on_cast_stream_ready(self, ep_url, video, audio, err, quality,
                              gen) -> None:
        """投屏取流完成（主线程槽）：代数守卫 → 写缓存 → 续接启停流程。

        gen 是发起取流时的 _cast_gen：换源/换作品（_stop_cast）或再次取流
        都会自增它，陈旧任务的结果只进缓存，**不得**消费 _cast_stream_pending
        （pending 属于当代任务；被旧任务吞掉即永久丢失）。
        """
        if gen != self._cast_gen:
            log.debug("丢弃陈旧投屏取流结果：代数 %s ≠ 当前 %s", gen, self._cast_gen)
            return
        pending, self._cast_stream_pending = self._cast_stream_pending, None
        self._cast_stream_task = None  # 当代任务完成（陈旧任务走不到这一行）
        if pending is None:
            return  # 已被清理（换作品/换源/停投屏）
        idx, kind = pending
        if err or not video:
            reason = _safe_error_text(err) if err else "无播放地址"
            if kind == "episode" and self._cast_active:
                # 投屏中切集取流失败（spec §6.5：保留原集、不自动关投屏）：
                # 当前播放状态保持不动——误置 idle 会让 _cast_toggle_play_pause
                # 把「应暂停」误判为「播放」（cast_state != "playing" → play()）。
                self._set_cast_status(f"获取播放流失败：{_safe_error_text(reason)}")
                return
            # 首次投屏取流失败：进 error 态并记原因，按钮可点重试
            self._cast_state = "error"
            self._cast_error = reason
            self._show_status(f"获取播放流失败：{reason}")
            self._update_cast_button()
            return
        self._stream_cache_set(self._stream_cache_key(ep_url, quality), video, audio)
        if not (0 <= idx < len(self._episodes)) or self._episodes[idx].url != ep_url:
            return  # 集已切换：陈旧结果只进缓存
        self._cast_stream_arrived(idx, kind, video)

    def _cast_stream_arrived(self, idx: int, kind: str, video: str) -> None:
        if kind == "start":
            if idx == self._current_idx and self._cast_state == "preparing_stream":
                self._cast_pick_device(idx, video)
            return
        if self._cast_active:
            self._cast_apply_episode(idx, video)

    # ---- 设备历史：直投上次设备 / 失败回落选设备对话框 ---- #
    def _cast_history(self):
        """投屏历史存储（惰性打开 data/cast_history.json；不可用返回 None）。"""
        store = self._cast_history_store
        if store is None:
            try:
                from framework.cast_history import CastHistory

                store = CastHistory(_cast_history_path())
            except Exception:  # noqa: BLE001 —— 历史不可用不能挡住投屏本身
                log.warning("投屏历史不可用", exc_info=True)
                return None
            self._cast_history_store = store
        return store

    def _cast_history_entries(self) -> list:
        """历史条目（最近在前）；存储不可用返回空列表。"""
        store = self._cast_history()
        if store is None:
            return []
        try:
            return store.entries()
        except Exception:  # noqa: BLE001 —— 读失败按无历史处理
            log.warning("读取投屏历史失败", exc_info=True)
            return []

    def _last_cast_renderer(self):
        """上次投屏成功的设备（DlnaRenderer）；无历史/历史不可用返回 None。"""
        try:
            from framework.cast_history import renderer_from_entry

            entries = self._cast_history_entries()
            return renderer_from_entry(entries[0]) if entries else None
        except Exception:  # noqa: BLE001 —— 模块损坏按无历史处理，落到弹框
            # 不兜底则异常冒到 Qt 槽，状态卡死 preparing_stream、按钮禁用，
            # 对话框（唯一「清除历史」入口）永不开 = IMP-1 的入口侧镜像。
            log.warning("读取上次投屏设备失败", exc_info=True)
            return None

    def _record_cast_history(self, renderer) -> None:
        """投屏 start 成功即写历史（不等 PLAYING；写失败不影响投屏）。"""
        store = self._cast_history()
        if store is None or renderer is None:
            return
        try:
            store.record(renderer)
        except Exception:  # noqa: BLE001 —— 历史写失败只记日志
            log.warning("写入投屏历史失败", exc_info=True)

    def _on_cast_history_cleared(self) -> None:
        """对话框「清除历史」信号：清掉存储（对话框本身不碰文件）。"""
        store = self._cast_history()
        if store is None:
            return
        try:
            store.clear()
        except Exception:  # noqa: BLE001
            log.warning("清除投屏历史失败", exc_info=True)

    def _cast_pick_device(self, idx: int, video: str) -> None:
        """取流完成选目标设备：有历史直投上次设备，否则开选择对话框。

        直投在 _start_cast 后台 start；连接失败（SOAP 超时、构造/建线程同步
        异常）由 _cast_direct_fallback 打开对话框让用户换设备。
        切集（kind == "episode"）走不到这里，不受直投影响。
        """
        self._cast_direct = False
        self._open_cast_dialog(idx, video)

    def _open_cast_dialog(self, idx: int, video: str) -> None:
        """投屏第二步：选目标电视（用户取消 → 回到干净 idle）。

        历史设备随构造传入对话框：置顶分组 + 上次使用默认选中，后台 SSDP
        扫描照常进行但不阻塞选择；对话框的「清除历史」经信号回到本视图清
        存储（组件不直接碰 data/cast_history.json）。
        """
        title = self._episodes[idx].title or f"第{idx + 1}集"
        try:
            from gui.components.cast_dialog import CastDeviceDialog
        except ImportError as exc:  # 组件未就位：**不**自建该文件
            self._cast_state = "idle"
            self._show_status(f"投屏组件不可用：{exc}")
            self._update_cast_button()
            return
        # 真实接口：CastDeviceDialog(title, parent, history=None)，
        # 确认后经 selected_device() 取设备（未选中抛 RuntimeError）。
        dialog = CastDeviceDialog(title, self, history=self._cast_history_entries())
        clear_signal = getattr(dialog, "clearHistoryRequested", None)
        if clear_signal is not None:
            clear_signal.connect(self._on_cast_history_cleared)
        if not dialog.exec():  # Accepted=1 真 / Rejected=0 假
            self._cast_state = "idle"
            self._show_status("")
            self._update_cast_button()
            return
        try:
            renderer = dialog.selected_device()
        except RuntimeError:
            renderer = None
        if renderer is None:
            self._cast_state = "idle"
            self._show_status("")
            self._update_cast_button()
            return
        if self._cast_state != "preparing_stream":
            # 对话框模态期间流程已被 _stop_cast 作废（换源/换作品/关页）：
            # UI 已复位，此刻确认设备不得再把会话拉起来
            return
        self._start_cast(idx, video, renderer)

    def _start_cast(self, idx: int, video: str, renderer) -> None:
        """投屏第三步：后台起会话，完成后开轮询。start **不**动本地播放器。

        session.start()（LAN 端点 + SetAVTransportURI + Play）是同步网络调用，
        挪到 _CastStartTask 后台线程执行（spec §7「Qt 主线程只处理信号」，
        最坏 ~10s 的冻结不再发生在 GUI 线程），完成经信号回主线程在
        _on_cast_start_finished 装配状态。提交即自增代数：启动期间
        _stop_cast（换源/换作品/关页）会再自增使本次结果作废。
        """
        title = self._episodes[idx].title or f"第{idx + 1}集"
        headers, ad_block, force_proxy = self._cast_media_args()
        cast_tuning = self._source_cast_tuning()
        self._configure_cast_tuning()
        self._cast_state = "connecting"
        self._update_cast_button()
        # 构造会话 / 建线程 / 起线程都在 try 内：任何一步抛错都收敛到
        # _cast_fail_start（直投则走 _cast_direct_fallback），否则异常冒到
        # Qt 槽只打印一行，状态卡死在 connecting、按钮禁用、
        # _cast_start_task 残留（F5-2）。
        try:
            from framework.cast_session import CastSession
            from framework.dlna import DlnaClient
            from framework.media_proxy import MediaProxy, trusted_media_context

            # 顺序保证：本地播放器的停止**只**挂在这条回调上，而它由
            # poll_state() 在电视报 PLAYING 时才调用 → start 前后本地都在播。
            session = CastSession(
                MediaProxy.instance(),
                lambda r: DlnaClient(r),
                on_local_player_stop=lambda: self._cast_local_player_stop.emit(),
            )
            self._cast_gen += 1
            task = _CastStartTask(session, renderer, idx, video, title, headers,
                                  ad_block, force_proxy, self._cast_gen)
            task.signals.finished.connect(self._on_cast_start_finished)
            self._cast_start_task = task
            task.start()
        except Exception as exc:  # noqa: BLE001 —— 构造/建线程失败也要退出 connecting
            # 没有真正在途的任务：必须清掉引用，否则 _begin_cast 的串行守卫
            # 会永久拦截「上一次投屏启动尚未结束」，用户再也无法重试。
            self._cast_start_task = None
            if self._cast_direct:
                # 直投的同步异常与后台 start 失败同样要回落对话框：历史条目
                # 不会被清除，只报错不开框 = 下次点投屏仍直投、仍到不了选择
                # 入口的死路（决策 2 的「start 失败再弹框」唯一的漏网分支）。
                self._cast_direct_fallback(exc, idx, video)
                return
            self._cast_fail_start(exc)

    def _cast_direct_fallback(self, exc: BaseException, idx: int,
                              video: str) -> None:
        """直投上次设备失败 → 回落选设备对话框（同步异常与后台失败共用）。

        把失败原因带状态栏，让用户换一台电视继续，而不是报错死：清
        _cast_direct（只回落一层，第二次失败走 _cast_fail_start 不再弹框）、
        状态回 preparing_stream（保证对话框确认后可续投）。
        """
        self._cast_direct = False
        self._cast_state = "preparing_stream"  # 对话框确认后可续投
        self._show_status(
            f"连接上次投屏设备失败：{str(exc) or exc.__class__.__name__}，"
            "请选择投屏设备"
        )
        self._update_cast_button()
        self._open_cast_dialog(idx, video)

    def _on_cast_start_finished(self, gen: int, session, exc) -> None:
        """start 后台完成（主线程槽）：按代数分流——新鲜才装配，陈旧只收尾。"""
        task = self._cast_start_task
        if gen != self._cast_gen or task is None or task.gen != gen:
            # 启动期间 _stop_cast 已作废本次流程：不碰任何 UI/状态；会话若已
            # start 成功必须补 stop（LAN 端点已开、电视可能已在播）。同步执行
            # 与 _stop_cast 的网络清理同一策略，且天然排在任何新 start 之前
            #（_begin_cast 有 _cast_start_task 守卫，等这里收尾才允许再投）。
            if exc is None:
                try:
                    session.stop()
                except Exception:  # noqa: BLE001 —— 收尾失败也只记日志
                    log.warning("作废的投屏启动收尾失败", exc_info=True)
            if task is not None and task.gen == gen:
                self._cast_start_task = None
            return
        self._cast_start_task = None
        if exc is not None:
            if self._cast_direct:
                self._cast_direct_fallback(exc, task.idx, task.video)
                return
            if self._cast_can_transcode(task.video):
                self._cast_session = session
                self._cast_renderer = task.renderer
                self._cast_device = getattr(task.renderer, "friendly_name", "") or ""
                self._cast_active = True
                self._start_cast_transcode_fallback(
                    task.idx, task.video, task.renderer, exc,
                )
                return
            self._cast_fail_start(exc)
            return
        self._cast_direct = False
        self._cast_session = session
        self._cast_renderer = task.renderer
        self._cast_device = getattr(task.renderer, "friendly_name", "") or ""
        self._cast_active = True
        self._cast_state = "connecting"
        self._cast_error = ""
        self._cast_video = task.video
        self._cast_transcoding = False
        self._cast_transcode_attempted = False
        self._cast_startup_retry_attempted = False
        self._cast_local_stop_pending = False
        self._cast_duration = 0
        self._cast_volume = 50
        self._cast_status_text = ""
        self._record_cast_history(task.renderer)  # start 成功即记忆（非 PLAYING）
        self._apply_cast_controls(True)
        self._start_cast_poll(session)
        self._set_cast_status(f"正在连接电视 {self._cast_device}" if self._cast_device
                              else "正在连接电视")
        # spec 6.4 步骤 6：轮询直到 PLAYING 或超时 CAST_PLAYING_TIMEOUT_MS → 到期未 PLAYING 走
        # _on_cast_playing_timeout（关会话/保留本地/错误可重试）
        self._cast_playing_timer.start()
        self._update_cast_button()

    def _cast_fail_start(self, exc: BaseException) -> None:
        self._stop_cast(error=str(exc) or exc.__class__.__name__)

    @staticmethod
    def _cast_can_transcode(video: str) -> bool:
        path = urlsplit(video or "").path.lower()
        return not path.endswith((".mp4", ".m4v", ".mov", ".webm", ".m3u8", ".m3u"))

    def _start_cast_transcode_fallback(self, idx: int, video: str, renderer,
                                       error: BaseException) -> None:
        if self._cast_transcode_attempted or self._cast_transcode_task is not None:
            return
        session = self._cast_session
        if session is None:
            self._stop_cast(error=f"原始投屏失败：{error}")
            return
        self._cast_transcode_attempted = True
        self._cast_transcoding = True
        self._cast_video = video
        self._cast_playing_timer.stop()
        task = self._cast_poll_task
        self._cast_poll_task = None
        if task is not None:
            task.stop()
        self._cast_gen += 1
        headers, _ad_block, _force_proxy = self._cast_media_args()
        try:
            from framework.media_proxy import MediaProxy, trusted_media_context

            transcode_task = _CastTranscodeTask(
                session, MediaProxy.instance(), renderer, idx,
                video, self._episodes[idx].title or f"第{idx + 1}集",
                headers, self._cast_gen,
            )
            transcode_task.signals.finished.connect(self._on_transcode_finished)
            self._cast_transcode_task = transcode_task
            self._cast_state = "connecting"
            self._set_cast_status("正在转换为电视兼容格式")
            self._update_cast_button()
            transcode_task.start()
        except Exception as exc:
            self._stop_cast(error=f"转码启动失败：{exc}")

    def _on_transcode_finished(self, gen: int, session, transcode, exc) -> None:
        task = self._cast_transcode_task
        if task is None or task.gen != gen or gen != self._cast_gen:
            if transcode is not None:
                try:
                    transcode.stop()
                except Exception:
                    pass
            try:
                session.stop()
            except Exception:
                pass
            return
        self._cast_transcode_task = None
        if exc is not None:
            self._stop_cast(error=f"转码失败：{exc}")
            return
        self._cast_session = session
        self._cast_active = True
        self._cast_state = "connecting"
        self._cast_error = ""
        self._start_cast_poll(session)
        self._cast_playing_timer.start()
        self._set_cast_status("正在连接电视")
        self._update_cast_button()

    def _cast_media_args(self):
        """当前源的请求头 / 广告过滤 / 强制代理（与本地播放同一套来源）。"""
        headers = {}
        request_headers = getattr(self._source, "request_headers", None)
        if callable(request_headers):
            try:
                headers = request_headers() or {}
            except Exception:  # noqa: BLE001
                headers = {}
        try:
            ad_block = dict((getattr(self._source, "raw", None) or {}).get("ad_block") or {})
        except Exception:  # noqa: BLE001
            ad_block = {}
        return headers, ad_block, self._force_proxy_enabled()

    # ---- 后台轮询：1s → Qt signal → 主线程更新 UI ---- #
    def _start_cast_poll(self, session) -> None:
        task = _CastPollTask(session, interval=1.0, max_failures=3)
        task.signals.state_changed.connect(self._on_cast_state)
        task.signals.poll_failed.connect(self._on_cast_poll_failed)
        self._cast_poll_task = task  # 先注册身份再起线程：槽内按它丢弃陈旧信号
        task.start()

    def _on_cast_state(self, state: dict, task) -> None:
        """轮询槽（主线程）：进度条 / 时间 / 播放暂停按钮 / 设备名。

        task 是产生该信号的 _CastPollTask：「停投 + 再投」后旧任务已经排队/
        迟到的信号必须整条丢弃——否则它会撤掉新会话的 8s 超时表、改写状态
        与进度（F5-1）。会话为 None 同样丢弃（会话已收，晚到信号无处落地）。
        """
        if (task is None or task is not self._cast_poll_task
                or self._cast_session is None):
            return
        transport = str(state.get("state") or "")
        try:
            position = int(state.get("position") or 0)
        except (TypeError, ValueError):
            position = -1
        try:
            duration = int(state.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration > 0:
            self._cast_duration = duration
        if position >= 0 and self._cast_duration > 0:
            if not self.progress.isSliderDown():  # 用户正在拖 → 别抢他的手
                self.progress.blockSignals(True)
                self.progress.setValue(
                    max(0, min(1000, int(position * 1000 / self._cast_duration))))
                self.progress.blockSignals(False)
            self.time_label.setText(
                f"{_fmt_hms(position)} / {_fmt_hms(self._cast_duration)}")
        if transport == "PLAYING":
            self._cast_state = "playing"
            self._cast_playing_timer.stop()
            if self._cast_transcoding:
                self._cast_transcoding = False
                if self._cast_local_stop_pending:
                    self._cast_local_stop_pending = False
                    self._stop_local_player()
        elif transport == "PAUSED_PLAYBACK":
            if not self._cast_playing_timer.isActive():
                self._cast_state = "paused"
        elif transport in ("STOPPED", "NO_MEDIA_PRESENT"):
            if self._cast_state == "connecting" and not self._cast_startup_retry_attempted:
                self._retry_cast_start()
            else:
                self._cast_state = "stopped"
        # TRANSITIONING（切集中）不改状态：启动期仍是 connecting（按钮禁用），
        # 已投屏则维持原状态，避免切集瞬间把「停止投屏」入口关掉。
        self._update_play_button()
        device = self._cast_device
        if self._cast_state == "playing":
            self._set_cast_status(f"投屏中 - {device}" if device else "投屏中")
        elif self._cast_state == "paused":
            self._set_cast_status(f"已暂停 - {device}" if device else "已暂停")
        elif self._cast_state == "stopped":
            self._set_cast_status("电视已停止播放")
        self._update_cast_button()

    def _on_cast_poll_failed(self, msg: str, task) -> None:
        """连续 3 次轮询失败：标记 error、停轮询并释放会话端点。

        按任务身份守卫：旧任务的迟到失败（停投+再投之后才落地）不得杀死
        刚建好的新会话（F5-1）。
        """
        if (task is None or task is not self._cast_poll_task
                or self._cast_session is None):
            return
        self._stop_cast(error=str(msg) or "与电视失去连接")

    def _retry_cast_start(self) -> None:
        if self._cast_session is None or self._cast_state != "connecting":
            return
        self._cast_startup_retry_attempted = True
        try:
            self._cast_session.play()
            self._set_cast_status("正在重新启动电视播放")
        except Exception as exc:
            self._stop_cast(error=f"电视播放启动失败：{exc}")

    def _on_cast_playing_timeout(self) -> None:
        """spec 6.4 步骤 6：启动后超时（CAST_PLAYING_TIMEOUT_MS）电视仍未进
        PLAYING → 判投屏失败。

        电视接受 Play 但状态一直不进 STOPPED 以外的播放态时，SOAP 全部成功、
        轮询的「3 次异常」判据永不触发，只有这条路能把挂住的会话/LAN 端点
        收干净：关会话与轮询、保留本地播放器、进 error 态（按钮可点重试）。
        超时秒数从单一常量派生，避免常量与文案再次漂移。
        """
        if self._cast_session is None:
            return
        if self._cast_state in ("playing", "paused"):
            return
        seconds = max(1, self.CAST_PLAYING_TIMEOUT_MS // 1000)
        if (not self._cast_transcode_attempted
                and self._cast_can_transcode(self._cast_video)):
            self._start_cast_transcode_fallback(
                self._current_idx, self._cast_video, self._cast_renderer,
                RuntimeError(f"电视未在 {seconds} 秒内开始播放"),
            )
            return
        self._stop_cast(error=(f"转码后电视未在 {seconds} 秒内开始播放"
                               if self._cast_transcoding
                               else f"电视未在 {seconds} 秒内开始播放"))

    def _set_cast_status(self, text: str) -> None:
        """状态文案去抖（轮询每秒一次，同文案不重排 UI）。"""
        if text == self._cast_status_text:
            return
        self._show_status(text)

    # ---- cast_active 控制路由 ---- #
    def _apply_cast_controls(self, on: bool) -> None:
        """投屏中启用进度/时间/上下集并提示；结束投屏回到本地播放基线。

        注意结束投屏时**不能**一律禁用：本地可能还在播（投屏成功前不停本地），
        那种情况下进度/音量/倍速必须保持可用，否则停投屏就等于把控制条废掉。
        """
        if not on:
            if self._player is not None and self._has_played:
                self._enable_embedded_controls()
            else:
                self._disable_embedded_controls()
            self.play_btn.setText("▶")
            self.prev_btn.setToolTip("上一集")
            return
        for w in (self.play_btn, self.prev_btn, self.next_btn,
                  self.progress, self.time_label):
            w.setEnabled(True)
        self.progress.setToolTip("拖动进度 → 发送到电视")
        self.time_label.setToolTip("电视端播放进度")
        self.play_btn.setToolTip("播放 / 暂停（投屏）")
        self.prev_btn.setToolTip("投屏上一集")
        self.next_btn.setToolTip("投屏下一集")
        # 音量：DlnaClient/CastSession 尚无 RenderingControl setVolume 链路，
        # 有 RC 的设备也保持禁用——宁可诚实禁用也不留一个拖动无效的死控件
        # （T5-5）。补上音量链路后可恢复按 rendering_control_url 启用。
        has_rc = bool(getattr(self._cast_renderer, "rendering_control_url", None))
        tip = ("投屏设备支持音量，但投屏音量控制暂未实现（延后）" if has_rc
               else "该投屏设备不支持音量控制")
        self.vol_btn.setEnabled(False)
        self.vol_slider.setEnabled(False)
        self.vol_btn.setToolTip(tip)
        self.vol_slider.setToolTip(tip)

    def _update_play_button(self) -> None:
        """播放键图标：投屏中跟电视状态；本地播放跟内嵌播放器的真实状态。

        「缓冲中」也算播放中（图标保持 ⏸）：播放器 的 is_playing() 在缓冲、
        起播、seek 重缓冲期间都是 False，直接用它会让图标在缓冲时翻回 ▶。

        只在图标真的变了才 setText：`setText` 会触发按钮布局与重绘，进度轮询
        每秒都会调用本函数，重复设置就是白烧 UI 线程（按键卡顿的来源之一）。
        """
        if self._cast_active:
            text = "⏸" if self._cast_state == "playing" else "▶"
        elif self._player is not None and self._has_played:
            try:
                text = "⏸" if self._player_is_active() else "▶"
            except Exception:  # noqa: BLE001 —— 播放器释放竞态
                text = "▶"
        else:
            return
        if self.play_btn.text() != text:
            self.play_btn.setText(text)

    def _cast_toggle_play_pause(self) -> None:
        session = self._cast_session
        if session is None:
            return
        try:
            if self._cast_state == "playing":
                session.pause()
                self._cast_state = "paused"
            else:
                session.play()
                self._cast_state = "playing"
        except Exception as exc:  # noqa: BLE001 —— 会话已断
            self._set_cast_status(f"投屏控制失败：{exc}")
            return
        self._update_play_button()

    def _on_cast_progress_released(self) -> None:
        """进度条拖到电视上（未投屏/时长未知 → 空操作）。"""
        if not self._cast_active or self._cast_session is None:
            return
        if self._cast_duration <= 0:
            return
        seconds = int(self.progress.value() * self._cast_duration / 1000)
        try:
            self._cast_session.seek(seconds)
        except Exception as exc:  # noqa: BLE001
            self._set_cast_status(f"投屏控制失败：{exc}")

    def _cast_set_episode(self, idx: int) -> None:
        if not (0 <= idx < len(self._episodes)) or idx == self._current_idx:
            return
        self._cast_resolve_stream(idx, "episode")

    def _cast_apply_episode(self, idx: int, video: str) -> None:
        """切集成功才改当前集；失败保留原集（CastSession 内部已回滚 URI）。"""
        if not self._cast_active or self._cast_session is None:
            return
        if not (0 <= idx < len(self._episodes)):
            return
        ep = self._episodes[idx]
        title = ep.title or f"第{idx + 1}集"
        headers, ad_block, force_proxy = self._cast_media_args()
        try:
            self._cast_session.set_episode(idx, video, title, headers, ad_block,
                                           force_proxy)
        except Exception as exc:  # noqa: BLE001
            self._set_cast_status(f"投屏切集失败：{exc}")
            return
        self._selection_mode = False
        self._current_idx = idx
        self._paint_card_selection()
        self._update_ep_check()
        self._refresh_fs_title()
        self.episode_changed.emit((self._detail, ep.title, ep.url))
        self._cast_duration = 0  # 新一集时长未读到，等下一轮轮询回填

    # ---- 本地播放器停止入口 + 会话清理 ---- #
    def _on_cast_local_player_stop(self) -> None:
        if self._cast_session is None:
            return
        if self._cast_transcoding and self._cast_state != "playing":
            self._cast_local_stop_pending = True
            return
        self._stop_local_player()

    def _stop_local_player(self) -> None:
        """停止本地播放（内嵌播放器）。只停不重启。"""
        self._stop_player()

    def _stop_cast(self, error: str = "") -> None:
        """清理投屏：停轮询 → Stop/释放会话（含 LAN 端点/token）→ 复位 UI。

        换作品 / 换源 / 关闭播放页 / App 退出 / 用户点停止 / 连续轮询失败 /
        start 失败（含 8 秒未 PLAYING 超时）七条路径都收敛到这里。**不**重启
        本地播放器。自增 _cast_gen 作废在途取流/启动任务：其迟到回调按代数
        丢弃（启动已成功则在槽里补 stop），不再触碰复位后的状态。
        """
        self._cast_gen += 1  # 作废在途取流/启动任务
        self._cast_direct = False  # 直投标志随流程作废（迟到失败不再回落开框）
        self._cast_playing_timer.stop()
        stream_task = self._cast_stream_task
        self._cast_stream_task = None
        if stream_task is not None:
            cancel = getattr(stream_task, "cancel", None)
            if callable(cancel):
                cancel()
        task = self._cast_poll_task
        self._cast_poll_task = None
        if task is not None:
            task.stop()  # 线程自持引用，无需保活列表；下个 step 内自行退出
        session = self._cast_session
        self._cast_session = None
        self._cast_renderer = None
        self._cast_device = ""
        self._cast_duration = 0
        self._cast_active = False
        self._cast_stream_pending = None
        self._cast_stream_task = None
        self._cast_transcode_task = None
        self._cast_transcoding = False
        self._cast_transcode_attempted = False
        self._cast_startup_retry_attempted = False
        self._cast_video = ""
        self._cast_local_stop_pending = False
        if session is not None:
            try:
                session.stop()  # Stop + 释放 DLNA 会话 + 关 LAN 端点
            except Exception:  # noqa: BLE001 —— 网络已断也必须继续复位 UI
                log.warning("停止投屏会话失败", exc_info=True)
        self._cast_state = "error" if error else "idle"
        self._cast_error = error
        self._apply_cast_controls(False)
        self._cast_status_text = ""  # 复位去抖基准，下一次状态必能显示
        self._show_status(f"投屏失败：{error}" if error else "")
        self._update_cast_button()

    # ------------------------------------------------------------------ #
    def _progress_options(self):
        detail = self._detail
        library_url = getattr(self, "library_url", "") or detail.url
        episodes = tuple(self._episodes)
        index = max(0, self._current_idx)
        chapter_url = episodes[index].url if episodes else detail.url
        chapter_title = (getattr(episodes[index], "title", "") if episodes
                         else getattr(detail, "title", ""))
        single = len(episodes) <= 1
        location = {}
        lookup = getattr(self, "progress_lookup", None)
        record = lookup(library_url) if lookup else None
        matched = False
        if record:
            # 同一集就续播——不要求 URL 全等（换线路/CDN/签名都会让 URL 变样，
            # 见 _same_episode）；否则换条线路看过的集会被当成「没看过」重播。
            matched = _same_episode(
                record.get("chapter_url") or "", record.get("chapter_title") or "",
                chapter_url, chapter_title, single=single,
            )
        if matched:
            location = dict(record.get("location") or {})
            if "seconds" not in location and record.get("position") is not None:
                location["position"] = record["position"]
        elif _same_episode(getattr(self, "_restore_episode_url", "") or "", "",
                           chapter_url, chapter_title, single=single):
            location = dict(getattr(self, "_pending_location", None) or {})
            if "seconds" not in location and self._pending_position is not None:
                location["position"] = self._pending_position
        self._pending_position = None
        self._pending_location = None
        allowed = getattr(self, "progress_allowed", None)
        if allowed is not None and not allowed(library_url):
            location = {}

        def changed(idx, position):
            if episodes:
                if not 0 <= idx < len(episodes):
                    return
                episode = episodes[idx]
                title = getattr(episode, "title", "") or f"第{idx + 1}集"
                url = episode.url
            else:
                title, url = getattr(detail, "title", ""), detail.url
            position = {**position, "chapter_url": url}
            position["library_url"] = library_url
            # 内嵌播放器没有「外播会话」可校验，用引擎标记让上层放行
            position["engine"] = "embedded"
            payload = (detail, title, url, position["seconds"] / position["duration"], None, position)
            writer = getattr(self, "progress_writer", None)
            if writer is not None:
                writer(payload)
            else:
                try:
                    self.position_changed.emit(payload)
                except RuntimeError:
                    pass
            # 预取判定的唯一时基。**必须放在进度回写之后**：回写是用户可见的
            # 进度记忆，预取只是加速，任何一侧出错都不能连累另一侧。这里用
            # 信号而不是直接调用 _prefetch_next，是因为 PlaybackMonitor 在
            # 后台线程回调，跨线程直接改视图状态属线程归属违规。
            try:
                self._progress_position.emit(
                    int(idx), float(position["seconds"]), float(position["duration"])
                )
            except (RuntimeError, KeyError, TypeError, ValueError):
                pass

        handoff_writer = getattr(self, "handoff_writer", None)
        if episodes:
            title = getattr(episodes[index], "title", "") or f"第{index + 1}集"
        else:
            title = getattr(detail, "title", "")

        def handed_off():
            if handoff_writer is not None:
                handoff_writer(detail, library_url, title, chapter_url)

        return {"on_progress": changed, "resume_positions": {index: location}, "on_handoff": handed_off}

    def position_snapshot(self):
        return None, None

    def _build_current_location(self):
        return None

    @property
    def detail(self):
        """当前作品（只读公共访问器，供阅读页判定是否仍为同一作品）。"""
        return self._detail

    @property
    def current_sid(self):
        """当前播放源 id（只读公共访问器）。"""
        return self._current_sid

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
        if pos is None:
            return
        try:
            self.position_changed.emit((ctx[0], ctx[1], ctx[2], pos, None))
        except RuntimeError:
            pass

    def _toggle_play_pause(self) -> None:
        """播放/暂停（内嵌播放器）。

        - 投屏中：转投屏控制
        - 选集态无当前流：按高亮集取流播放
        - 已在播/暂停：直接 pause/resume（不再重开播放器、不重新取流）
        """
        if self._cast_active:
            self._cast_toggle_play_pause()
            return
        player = self._player
        if player is not None and self._current_play and self._has_played:
            try:
                # 判定用 _player_is_active()（播放中/缓冲中都算在播），
                # 与图标口径一致：否则缓冲中按一下会走 resume() 分支，
                # 实际什么都没发生，图标却在 ▶/⏸ 之间来回跳。
                if self._player_is_active():
                    player.pause()
                    self._sync_overlay_state(playing=False)
                else:
                    player.resume()
                    self._hide_buffer_indicator()
                    self._sync_overlay_state(playing=True)
                self.control_bar.show()       # 操作即唤回，避免卡在隐藏态
                self.setCursor(Qt.ArrowCursor)  # 操作中指针必须可见
                self._update_play_button()    # 图标立即切 ▶/⏸（秒级反馈）
                return
            except Exception:  # noqa: BLE001 —— 播放器已释放：落到重新播放
                log.warning("内嵌播放器暂停/恢复失败，改为重新播放", exc_info=True)
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
        self._fs_title_label = fs_title  # 全屏下切集时同步刷新标题
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
        # 整个播放器容器（画面 + 控制条）进全屏：控制条跟着走，全屏里照样
        # 能暂停/拖进度/倍速/切集。只搬画面的话控制条会留在主窗口里。
        self._player_box.setParent(fs)
        lay.addWidget(self._player_box, stretch=1)
        self._fs_win = fs
        self._sync_fs_button()  # 同一按钮切到「退出全屏」态
        # 退出途径：Esc / 双击 / 控制栏按钮 / 关闭窗口
        # （键盘/关闭/双击均在全局 eventFilter 里兜底——实例属性赋值在
        #   PySide6 不进 Qt 虚表永不回调，属无效代码，见 eventFilter 注释）
        fs.showFullScreen()
        self.control_bar.show()
        self._fs_titlebar.show()
        self._reposition_overlays()
        self._wake_controls()

    def _refresh_fs_title(self) -> None:
        """全屏下切集时刷新顶栏标题（窗口化忽略）。"""
        if self._fs_win is None:
            return
        label = getattr(self, "_fs_title_label", None)
        if label is not None:
            try:
                label.setText(self._fs_title_text())
            except RuntimeError:
                pass

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
        # 播放器容器 reparent 回主视图（画面 + 控制条一起回来）
        self._player_box.setParent(self)
        self._right_layout.insertWidget(0, self._player_box)
        try:
            fs.close()
            fs.deleteLater()
        except Exception:  # noqa: BLE001
            pass
        self._reposition_overlays()
        self._sync_fs_button()  # 同一按钮切回「全屏」态
        self._hide_timer.stop()  # 退出全屏不再自动隐藏
        self.control_bar.show()  # 控制条回到常驻可见
        self.setCursor(Qt.ArrowCursor)

    def _sync_fs_button(self) -> None:
        """全屏/退出全屏共用同一按钮：按当前是否全屏切换图标与提示。"""
        in_fs = self._fs_win is not None
        self.fs_btn.setText("⤡" if in_fs else "⛶")
        self.fs_btn.setToolTip("退出全屏（F）" if in_fs else "全屏（F）")

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
    def _on_progress_position(self, idx: int, seconds: float, duration: float) -> None:
        """进度时基入口（主线程槽）：进窗口才预取，且同一位置只预取一次。"""
        self._maybe_prefetch_next(idx, float(seconds), float(duration))

    def _prefetch_next(self, idx: int = -1) -> None:
        """「准备下一集」的唯一入口：先判可行性（缓存/放弃/冷却），再串行取流。

        窗口内的首次触发（_maybe_prefetch_next）与冷却到期后的重试
        （_prefetch_tick）都走这里，保证「要不要发请求」只有一处判定。
        """
        if idx < 0:
            idx = self._current_idx
        if self._source is None:
            return
        if not (0 <= idx < len(self._episodes) - 1):
            return
        next_idx = idx + 1
        key = self._prefetch_key_for_episode(next_idx)
        if key is None:
            return
        if self._stream_cache_get(key) is not None or self._series_memo_ready(next_idx):
            return
        if key in self._prefetch_hopeless:
            return
        retry_at = self._prefetch_retry_at.get(key)
        if retry_at is not None:
            if time.monotonic() < retry_at:
                return  # 失败冷却中
            self._prefetch_retry_at.pop(key, None)  # 冷却到期 → 允许再试
        # 冷却到期的重试也要清掉 attempted（_prefetch_episode 见到它就直接返回）
        self._prefetch_attempted.discard(key)
        self._prefetch_episode(next_idx)

    def _prefetch_episode(self, idx: int) -> None:
        """后台预拉指定集播放流（串行防反爬限流，命中/越界/在拉则跳过）。

        与旧 _prefetch_next 同参数同回调：实际在预拉中（_prefetch_idx >= 0）
        或已缓存则跳过；预拉完成后由 _on_prefetch_done 写缓存并解锁。

        **串行是硬约束**：预取请求与正在播放的取流共用源站配额，并发会让
        本集的分片/取流抢带宽，表现为「预取一开播就卡」。所以同时只允许
        一个预取任务在跑，其余请求等在巡检/下一次进度回调里再补。
        """
        if self._source is None:
            return
        if not (0 <= idx < len(self._episodes)):
            return
        ep = self._episodes[idx]
        key = self._stream_cache_key(ep.url, self._quality)
        if self._stream_cache_get(key) is not None:
            return
        # 媒体代理里已有该集的惰性流映射 → 播放端不会再跑 resolver，
        # 预取毫无收益（也避免在受限站点上白打一个请求）。
        if self._series_memo_ready(idx):
            return
        if key in self._prefetch_hopeless:
            return  # 本集在当前线路/画质下确定取不到流，直到换集/换源才重试
        if self._prefetch_idx >= 0:
            return  # 已有其他集在预拉（串行）
        # 每个 (集, 画质) 一生只发起一次取流：失败由 _on_prefetch_failed 写
        # 冷却/放弃标记，冷却到点后由 _prefetch_tick 或下一次进度回调重试。
        # 冷却结束后的重试**不能**依赖「再次进入窗口」——真实进度到那时已经
        # 过了窗口，旧实现就是卡在这里，看起来像预取根本没跑。
        if key in self._prefetch_attempted:
            return
        retry_at = self._prefetch_retry_at.get(key)
        if retry_at is not None:
            if time.monotonic() < retry_at:
                return  # 失败冷却中
            self._prefetch_retry_at.pop(key, None)
        self._prefetch_attempted.add(key)
        self._prefetch_idx = idx
        self._prefetch_quality = self._quality
        task = _FetchStreamTask(self._content, self._source, ep.url, self._quality,
                                gen=self._stream_gen, prefetch=True)
        task.signals.finished.connect(self._on_prefetch_done)
        self._prefetch_task = task
        try:
            QThreadPool.globalInstance().start(task)
        except Exception:  # noqa: BLE001 —— 线程池拒绝时不能让标记卡死串行锁
            self._prefetch_idx = -2
            self._prefetch_task = None
            raise

    def _series_memo_ready(self, idx: int) -> bool:
        """该集的流映射是否已在媒体代理里（有则播放端不会再跑 resolver）。"""
        if not self._series_key or self._source_needs_merged():
            return False
        try:
            from framework.media_proxy import MediaProxy

            return MediaProxy.instance().series_episode_cached(self._series_key, idx)
        except Exception:  # noqa: BLE001 —— 纯优化判定，取不到就当没缓存
            return False

    def _prefetch_key_for_episode(self, idx: int):
        if not (0 <= idx < len(self._episodes)):
            return None
        return self._stream_cache_key(self._episodes[idx].url, self._quality)

    def _cancel_prefetch_for(self, ep_url: str, quality: str) -> None:
        """取消正在跑的那一路预取（仅当它正是 ep_url/quality）。

        用途：用户在窗口内手动点/自动续播切到这一集时，前端已经拿到了流，
        后端再解析一遍纯属重复请求。取消要**真正 interrupt 网络请求**
        （task.cancel() 会 cancel StreamBudget），否则只是白等它跑完。
        """
        task = getattr(self, "_prefetch_task", None)
        if task is None:
            return
        if getattr(task, "_url", None) != ep_url:
            return
        cancel = getattr(task, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:  # noqa: BLE001 —— 取消失败不影响播放
                log.debug("[prefetch] 取消在途预取失败", exc_info=True)
        self._prefetch_task = None
        self._prefetch_idx = -2

    def _prefetch_latched(self, source_idx: int, seconds: float) -> bool:
        """该集在「这个播放位置」是否已经触发过预取。

        锁存的意义：进度回写是 5 秒一条，用户暂停在片尾或回拖两下就会在
        窗口边界反复进出。没有锁存时每次进出都重新取流，既压站又可能把
        正在播放的本集拖慢——这正是「提前 4 分钟预加载」实际不生效的原因
        之一（一次成功之后又被打回未取）。
        """
        entry = self._prefetch_state.get((source_idx, self._quality))
        if not entry:
            return False
        latched_pos, attempts = entry
        if attempts >= self.PREFETCH_MAX_ATTEMPTS:
            return True
        return abs(float(seconds) - float(latched_pos[0])) < self.PREFETCH_LATCH_TOLERANCE_S

    def _prefetch_latch_at(self, source_idx: int, seconds: float, duration: float,
                        attempts: int | None = None) -> None:
        key = (source_idx, self._quality)
        previous = self._prefetch_state.get(key)
        count = (previous[1] if previous else 0) if attempts is None else attempts
        self._prefetch_state[key] = ((float(seconds), float(duration)), count)

    def _maybe_prefetch_next(self, idx: int, seconds: float, duration: float) -> None:
        """进度时基：进入片尾窗口且未锁存时，串行预拉下一集。

        只做判定与调度，不碰播放路径：取流在线程池里跑，单次请求也不会
        阻塞主线程；且本集已经在播，多出来的只有一次轻量 HTTP。
        """
        if duration <= 0 or idx != self._current_idx:
            return
        if not (0 <= idx < len(self._episodes) - 1):
            return
        remaining = duration - seconds
        if not (0 <= remaining <= self.PREFETCH_BEFORE_END_S):
            return
        if self._prefetch_latched(idx, seconds):
            return
        self._prefetch_latch_at(idx, seconds, duration)
        self._prefetch_next(idx)

    def _prefetch_tick(self) -> None:
        """兜底巡检：进度事件可能整段缺席（缓冲/暂停/画面卡住）。

        只读一次播放器状态（内嵌播放器直接读 播放器，无进程无 HTTP），
        **已锁存或已缓存时不做任何网络请求**；位置明显变化（用户回拖或
        续播定位后前进）时清掉锁存，让窗口重新生效。
        """
        if self._source is None or self._selection_mode:
            return
        if not (0 <= self._current_idx < len(self._episodes) - 1):
            return
        player = self._player
        if player is None or not self._has_played:
            return  # 未播放/已停止 → 巡检无事可做
        try:
            length_ms = int(player.get_length() or 0)
            time_ms = int(player.get_time() or 0)
        except Exception:  # noqa: BLE001 —— 播放器释放竞态：下一轮再来
            return
        if length_ms <= 0 or not (0 <= time_ms <= length_ms):
            return
        seconds = time_ms / 1000.0
        duration = length_ms / 1000.0
        idx = self._current_idx
        entry = self._prefetch_state.get((idx, self._quality))
        if entry is not None:
            latched_pos = entry[0][0]
            # 位置往回走了不止一个容差 → 用户回拖了，旧锁存作废
            if seconds < latched_pos - self.PREFETCH_LATCH_TOLERANCE_S:
                self._prefetch_state.pop((idx, self._quality), None)
        window_hit = duration - float(seconds) <= self.PREFETCH_BEFORE_END_S
        if not window_hit:
            return
        # 锁存读的是「上一次触发的播放位置」，必须在本轮判定之前取值。
        latched = self._prefetch_latched(idx, float(seconds))
        self._maybe_prefetch_next(idx, float(seconds), float(duration))
        if latched:
            # 位置没变但上一轮失败过：锁存不挡重试，冷却到期就补一次。
            self._prefetch_next(idx)

    def _start_prefetch_ticker(self) -> None:
        timer = getattr(self, "_prefetch_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(int(self.PREFETCH_TICK_MS))
            timer.timeout.connect(self._prefetch_tick)
            self._prefetch_timer = timer
        timer.start()

    def _stop_prefetch_ticker(self) -> None:
        timer = getattr(self, "_prefetch_timer", None)
        if timer is not None:
            timer.stop()

    def _prefetch_cache_ttl(self, video: str, audio: str) -> float:
        """预取链接的缓存时长：必须覆盖「提前 4 分钟」的预取窗口。

        普通链接按 max(默认 30s, 预取窗口 + 2 分钟缓冲) 缓存；带时效签名的
        链接按 URL 里的 expire/expires 推断剩余时长。此前固定按 30s 缓存，
        导致 4 分钟前取到的链接在点下一集时早已过期，预取实际无效。
        """
        window = float(self.PREFETCH_BEFORE_END_S) + float(
            self.PREFETCH_CACHE_MARGIN_S
        )
        try:
            from framework.stream_resolver import StreamResolver

            ttl = StreamResolver.cache_ttl_for_result(
                (video, audio), max(float(self._stream_cache_ttl), window)
            )
        except Exception:  # noqa: BLE001
            ttl = window
        if ttl > 0:
            return ttl
        return _signed_url_remaining_ttl((video, audio))

    def _warm_next_episode(self, ep_url: str, video: str, audio: str) -> None:
        """把预取结果预热进媒体代理：**分片落盘**为主，series memo 为辅。

        内嵌播放器不用惰性 `/e/` 播放列表（切集是我们自己换 media），所以
        真正有效的预热是「把下一集首几个分片提前落进代理磁盘缓存」——切集时
        播放器请求 `/c/<key>/<seg>` 直接命中本地文件，省掉 master/variant
        清单与首片的上游往返（omofuna 实测 3.1s + 2.5s）。

        series memo 只在**存在系列注册**时才有意义（投屏/历史路径可能注册），
        没有注册就跳过，不能因为 `_series_key` 为空就把分片预热一起挡掉。
        """
        if self._source is None:
            return
        if self._source_needs_merged():
            return
        idx = next(
            (i for i, ep in enumerate(self._episodes) if ep.url == ep_url), -1
        )
        if idx < 0:
            return
        try:
            from framework.media_proxy import MediaProxy, trusted_media_context

            headers = {}
            request_headers = getattr(self._source, "request_headers", None)
            if callable(request_headers):
                headers = request_headers() or {}
            ad_block = dict((self._source.raw or {}).get("ad_block") or {})
            proxy = MediaProxy.instance()
            sid = trusted_media_context(getattr(self._source, "source_id", ""))
            if self._series_key:
                proxy.warm_series_episode(
                    self._series_key, idx, video, audio, headers, ad_block,
                    source_id=sid,
                )
            proxy.prime_episode_segments(
                video, headers, ad_block,
                force_proxy=self._force_proxy_enabled(), source_id=sid,
                segments=self.PREFETCH_PRIME_SEGMENTS,
            )
        except Exception:  # noqa: BLE001 —— 纯优化，失败时照常走 resolver
            pass

    def _on_prefetch_done(self, ep_url, video, audio, err, quality, gen=None) -> None:
        self._prefetch_idx = -2
        self._prefetch_task = None
        if gen is not None and gen != self._stream_gen:
            return
        key = self._stream_cache_key(ep_url, quality)
        if err or not video:
            self._on_prefetch_failed(ep_url, quality, err)
            return
        self._prefetch_retry_at.pop(key, None)
        self._prefetch_hopeless.discard(key)
        ttl = self._prefetch_cache_ttl(video, audio)
        if ttl <= 0:
            self._prefetch_cache.pop(key, None)
            return
        self._prefetch_cache[key] = (time.monotonic() + ttl, video, audio)
        # 取到链接后：锁存位置保留（同位置不再重复判定），但重试额度归零——
        # 用户之后回拖进窗口还有完整额度；缓存/映射命中时 _prefetch_next 会
        # 直接短路，不会因为额度归零而多发请求。
        # 锁存键是**触发时的当前集**（其下一集才是 ep_url），所以比较要用 idx+1。
        for latch_key in [k for k in self._prefetch_state if k[1] == quality]:
            if self._prefetch_key_for_episode(int(latch_key[0]) + 1) == key:
                position = self._prefetch_state[latch_key][0]
                self._prefetch_state[latch_key] = (position, 0)
        self._prefetch_attempted.discard(key)
        # 覆盖整个预取窗口才预热代理：签名链接若会在窗口内过期，预热反而
        # 会让自动续播拿到过期地址。
        window = float(self.PREFETCH_BEFORE_END_S) + float(
            self.PREFETCH_CACHE_MARGIN_S
        )
        if ttl >= window:
            self._warm_next_episode(ep_url, video, audio)
        # 旁路把「下一集链接已就绪」告诉用户：播放位置记忆是自动的、看不见
        # 的，没有反馈就无法判断预取到底有没有工作。来源是探测器（无分集
        # 自动续播）时不提示，避免自动续播刷屏。
        idx = next((i for i, ep in enumerate(self._episodes) if ep.url == ep_url), -1)
        if idx >= 0 and not getattr(self, "_prefetch_from_probe", False):
            self._notify_prefetched(idx)

    def _on_prefetch_failed(self, ep_url: str, quality: str, err) -> None:
        """预取失败：按尝试次数指数退避，直到额度用完才彻底放弃该集。

        旧的「一次失败冷却 30 秒」在真正失败的站点上是空转：冷却结束后的
        下一次触发不会自动来到（进度回调可能已经跑完窗口）。这里把重试
        也放进锁存计数里，窗口内最多 PREFETCH_MAX_ATTEMPTS 次，且每次
        退避翻倍，避免在受限站点上反复打请求。
        """
        key = self._stream_cache_key(ep_url, quality)
        source_idx = next(
            (i for i, ep in enumerate(self._episodes) if ep.url == ep_url), -1
        )
        latch_key = (source_idx, quality) if source_idx >= 0 else None
        attempts = 0
        position = (0.0, 0.0)
        if latch_key is not None and latch_key in self._prefetch_state:
            position, attempts = self._prefetch_state[latch_key]
        attempts += 1
        if attempts >= self.PREFETCH_MAX_ATTEMPTS:
            self._prefetch_retry_at.pop(key, None)
            self._prefetch_hopeless.add(key)
            if latch_key is not None:
                self._prefetch_state[latch_key] = (position, attempts)
            self._show_prefetch_failure(ep_url, err)
            return
        delay = float(self.PREFETCH_RETRY_S) * (2 ** (attempts - 1))
        self._prefetch_retry_at[key] = time.monotonic() + delay
        if latch_key is not None:
            self._prefetch_state[latch_key] = (position, attempts)

    def _notify_prefetched(self, idx: int) -> None:
        """轻量反馈：下一集链接已提前取好（不打断播放，只在状态行提示）。"""
        title = ""
        if 0 <= idx < len(self._episodes):
            title = getattr(self._episodes[idx], "title", "") or f"第{idx + 1}集"
        try:
            self._show_status(f"下一集已就绪：{title}")
        except Exception:  # noqa: BLE001 —— 提示失败不影响播放
            pass

    def _show_prefetch_failure(self, ep_url: str, err) -> None:
        """预取彻底失败：只在用户停在片尾、马上要切集时才提示真正的手动点集。

        不弹对话框、不改播放状态——播放中的本集完全不受影响。
        """
        detail = _safe_error_text(err) if err else "无可用地址"
        log.info("[prefetch] 下一集链接预取放弃：%s（%s）", ep_url, detail)

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
        """「设置 → 重新播放当前集」：重建播放后端并重播当前集。

        用途是「播放出问题时手动重来一次」：结束旧 mpv 进程、新建 IPC 管道、
        重新取一次代理 URL——能解掉「解码器卡死 / 代理 token 失效 / 管道断开」
        这一类需要换实例才能恢复的故障。
        """
        if not self._current_play:
            # 选集态没有当前流 → 提示而非静默；_current_play 为空是
            # 「当前无流」的权威信号（见 _invalidate_current_stream）。
            self._show_status("请先选择要播放的集数" if self._selection_mode
                              else "当前没有正在播放的集")
            return
        # 重建实例：先释放旧后端（含代理租约），再由 _play 重新拉起
        player, self._player = self._player, None
        if player is not None:
            try:
                player.close()
            except Exception:  # noqa: BLE001 —— 旧实例已失效：继续重播
                log.warning("重建播放后端时关闭旧实例失败", exc_info=True)
        self._has_played = False
        self._show_status("正在重新播放当前集...")
        self._play(self._current_play, self._current_audio, self._current_title)

    def _source_network_caching_ms(self) -> int:
        """读源配置 media.hls.network_caching_ms（缺省 0 = 用分类默认值）。

        CDN 按连接限速的源需要更大的播放器网络缓冲（见 player_session 注释）。
        """
        try:
            hls = ((self._source.raw or {}).get("media") or {}).get("hls") or {}
            return int(hls.get("network_caching_ms") or 0)
        except Exception:  # noqa: BLE001 —— 配置缺失/损坏按默认缓冲走
            return 0

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
