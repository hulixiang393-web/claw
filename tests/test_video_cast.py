# -*- coding: utf-8 -*-
"""播放界面投屏接入与双向控制（Task 5）。

真实组件：VideoView 本体 + PySide6 真实信号投递（后台线程 emit → 主线程槽）。
替身只留在三条**进程外**边界上：
- MediaProxy（LAN 端点/取 URL）；
- Content.fetch_video_streams（真回源）；
- CastDialog（由并行的 Task 4 提供，测试注入假模块）与 CastSession（SOAP 网络）。
"""
import os
import sys
import threading
import time
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest                                                   # noqa: E402
from PySide6.QtCore import QCoreApplication, QThreadPool       # noqa: E402

import framework.cast_session as cs                            # noqa: E402
import framework.external_player as ep                         # noqa: E402
import framework.media_proxy as mp                             # noqa: E402

_VIEWS = []  # 本轮用例建过的视图：收尾时统一收掉后台轮询任务
_CAST_WAIT = 15.0  # 后台 start/取流完成等待窗口（全量套件高负载下仍留足余量）


def _drain() -> None:
    """跑一遍事件循环，让跨线程 emit 的信号在主线程落地。"""
    QCoreApplication.processEvents()


def _wait_for(pred, timeout: float = 5.0) -> bool:
    """轮询等待后台任务 + 信号落地（只等目标条件，不等整个线程池）。"""
    deadline = time.monotonic() + timeout
    while True:
        QCoreApplication.processEvents()
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def _stop_cast_polls():
    """用例收尾：停掉本例启动的投屏轮询，别让后台线程跨用例空转。"""
    yield
    for view in _VIEWS:
        try:
            view._stop_cast()
        except Exception:  # noqa: BLE001 —— 清理失败不影响断言结果
            pass
    _VIEWS.clear()


def _video_detail(n):
    from framework.content import Chapter, Detail

    return Detail(source_id="demo", content_type="video",
                  url="http://example.com/v/1", title="作品", cover="",
                  chapters=[Chapter(f"第{i + 1}集 标题{i}", f"http://e/{i}",
                                    cover="") for i in range(n)])


class _RawSource:
    base_url = "http://src.example.com"

    def __init__(self, raw=None, headers=None):
        self.raw = raw or {}
        self._headers = headers or {}

    def request_headers(self):
        return dict(self._headers)


class _FakeContent:
    def __init__(self, video="https://cdn/v.m3u8", err=None):
        self.calls = []
        self._video = video
        self._err = err

    def fetch_video_streams(self, source, url, quality="best", merged=False):
        self.calls.append((url, quality, merged))
        if self._err:
            raise RuntimeError(self._err)
        return self._video, ""


class _FakeProxy:
    def __init__(self):
        self.lan_started = []
        self.lan_stopped = []
        self._lan_error = None

    def start_lan_endpoint(self, bind_host):
        self.lan_started.append(bind_host)
        return "http://192.168.1.10:4321"

    def build_url(self, media_url, headers=None, ad_block=None,
                  force_proxy=False, public_base=None):
        return f"{public_base}/s/tok"

    def stop_lan_endpoint(self):
        self.lan_stopped.append(True)

    def release_token(self, url):
        pass


class _FakeSession:
    """CastSession 替身：记录调用与 start 所在线程，可配置 start/轮询结果。"""

    def __init__(self, start_error=None, poll_error=None,
                 poll_state_value="PLAYING"):
        self.calls = []
        self.start_error = start_error
        self.poll_error = poll_error
        self.poll_state_value = poll_state_value
        self.current_episode_index = None
        self.on_local_player_stop = None
        self.start_thread_id = None  # start() 在哪个线程执行（验证不占 GUI 线程）

    def start(self, renderer, episode_index, media_url, title, headers,
              ad_block, force_proxy):
        self.start_thread_id = threading.get_ident()
        self.calls.append(("start", renderer, episode_index, media_url, title,
                           headers, ad_block, force_proxy))
        if self.start_error is not None:
            raise self.start_error
        self.current_episode_index = episode_index

    def stop(self):
        self.calls.append(("stop",))

    def play(self):
        self.calls.append(("play",))

    def pause(self):
        self.calls.append(("pause",))

    def seek(self, seconds):
        self.calls.append(("seek", seconds))

    def set_episode(self, episode_index, media_url, title, headers,
                    ad_block, force_proxy):
        self.calls.append(("set_episode", episode_index, media_url, title))
        self.current_episode_index = episode_index

    def poll_state(self):
        self.calls.append(("poll_state",))
        if self.poll_error is not None:
            raise self.poll_error
        return {"state": self.poll_state_value, "position": 60, "duration": 120,
                "track": 1, "uri": "u"}


def _install_session(monkeypatch, session):
    """替换 CastSession 构造：返回 fake，并捕获会话回调（本地停播入口）。"""
    captured = {}

    def _factory(proxy, client_factory, on_local_player_stop=None):
        captured["on_local_player_stop"] = on_local_player_stop
        session.on_local_player_stop = on_local_player_stop
        return session

    monkeypatch.setattr(cs, "CastSession", _factory)
    return captured


def _install_proxy(monkeypatch, proxy=None):
    proxy = proxy if proxy is not None else _FakeProxy()
    monkeypatch.setattr(mp.MediaProxy, "instance", staticmethod(lambda: proxy))
    return proxy


class _LocalPool:
    """把 video_view 模块内的 QThreadPool 名字替换为本用例专用池的外壳。

    全量套件里全局线程池（16 槽）会被更早用例遗留的取流/封面下载任务占用，
    本用例的取流任务排队即超出等待窗口 → `test_cast_fetches_stream_when_cache_misses`
    约 2/3 概率超时（task-6 第一轮 flaky）。专用池消除跨用例全局状态依赖，
    线程执行 + 跨线程信号排队的异步链路本身完全不变（不削弱被测行为）。
    """

    def __init__(self, pool):
        self._pool = pool

    def globalInstance(self):
        return self._pool


def _use_local_pool(monkeypatch):
    """取流任务改走本用例专用 QThreadPool，返回该池（供诊断/清理）。"""
    from gui.pages.reader import video_view as vv

    pool = QThreadPool()
    monkeypatch.setattr(vv, "QThreadPool", _LocalPool(pool))
    return pool


def _renderer(rendering_control_url="http://192.168.1.20:80/rc"):
    from framework.dlna import DlnaRenderer

    return DlnaRenderer(
        udn="uuid:tv-1", friendly_name="客厅电视",
        location="http://192.168.1.20:80/root.xml",
        av_transport_control_url="http://192.168.1.20:80/avt",
        rendering_control_url=rendering_control_url,
        service_type="urn:schemas-upnp-org:service:AVTransport:1",
    )


def _install_dialog(monkeypatch, accepted=True, renderer=None):
    """注入假 gui.components.cast_dialog（Task 4 并行开发，测试不依赖它）。

    替身必须复刻真实接口：CastDeviceDialog(title, parent) + exec() +
    selected_device()（未选中抛 RuntimeError）。
    """
    module = types.ModuleType("gui.components.cast_dialog")
    seen = {"titles": [], "parents": []}

    class _Dialog:
        def __init__(self, title, parent=None):
            seen["titles"].append(title)
            seen["parents"].append(parent)
            self._renderer = renderer

        def exec(self):
            return accepted

        def selected_device(self):
            if self._renderer is None:
                raise RuntimeError("未选中投屏设备")
            return self._renderer

    module.CastDeviceDialog = _Dialog
    monkeypatch.setitem(sys.modules, "gui.components.cast_dialog", module)
    return seen


def test_cast_dialog_contract_matches_real_component(_qapp):
    """接口契约：video_view 用的类名/构造顺序必须与真实 cast_dialog 一致。

    Task 5 与 Task 4 并行开发曾错配成 CastDialog(parent, title) + .renderer
    （假模块注入下测试全绿、真机上 ImportError），此测试锁住真实边界。
    """
    import inspect

    import gui.components.cast_dialog as cd
    from gui.pages.reader.video_view import VideoView

    assert hasattr(cd, "CastDeviceDialog"), "video_view 依赖的对话框类名"
    params = list(inspect.signature(cd.CastDeviceDialog.__init__).parameters)
    assert params[1:3] == ["current_episode_title", "parent"], "构造参数顺序"
    assert hasattr(cd.CastDeviceDialog, "selected_device"), "选中设备访问器"
    src = inspect.getsource(VideoView._open_cast_dialog)
    assert "from gui.components.cast_dialog import CastDeviceDialog" in src
    assert "CastDeviceDialog(title, self)" in src      # 参数顺序：标题在前
    assert "dialog.selected_device()" in src           # 访问器而非 .renderer 属性
    assert "dialog.renderer" not in src


def _view(detail, idx=0, content=None):
    from gui.pages.reader.video_view import VideoView

    view = VideoView(object())
    view._source = _RawSource({"ad_block": {"urls": ["ad"]}},
                              headers={"Referer": "http://src.example.com/"})
    view._detail = detail
    view._episodes = detail.chapters
    view._current_idx = idx
    view._content = content if content is not None else _FakeContent()
    view._update_cast_button()
    _VIEWS.append(view)
    return view


def _start_cast(view, monkeypatch, session=None, renderer=None, accepted=True,
                idx=None):
    """走真实点击链路把投屏跑起来，返回 (session, captured, dialog_seen)。

    session.start() 在后台线程执行（spec §7），完成经信号回主线程装配——
    这里等到「会话已装配或已进 error」再返回，调用方拿到的是启动已落定的状态。
    """
    session = session if session is not None else _FakeSession()
    captured = _install_session(monkeypatch, session)
    _install_proxy(monkeypatch)
    if idx is None:
        idx = view._current_idx
    ep = view._episodes[idx]
    view._stream_cache[(ep.url, view._quality)] = (f"https://cdn/e{idx}.m3u8", "")
    seen = _install_dialog(monkeypatch, accepted=accepted,
                           renderer=renderer if renderer is not None else _renderer())
    view._on_cast_clicked()
    assert _wait_for(lambda: view._cast_session is not None
                     or view._cast_state == "error",
                     timeout=_CAST_WAIT), "投屏启动未在超时内落定"
    return session, captured, seen


# --------------------------------------------------------------------- #
# 1. 投屏按钮：初始态与禁用条件
# --------------------------------------------------------------------- #
def test_cast_button_disabled_without_source(_qapp):
    from gui.pages.reader.video_view import VideoView

    view = VideoView(object())
    view._update_cast_button()
    assert view.cast_btn.isEnabled() is False
    assert view.cast_btn.toolTip() == "投屏"


def test_cast_button_disabled_without_current_episode(_qapp):
    view = _view(_video_detail(0), idx=-1)
    view._update_cast_button()
    assert view.cast_btn.isEnabled() is False


def test_cast_button_enabled_with_source_and_current_episode(_qapp):
    view = _view(_video_detail(3), idx=1)
    view._update_cast_button()
    assert view.cast_btn.isEnabled() is True
    assert view.cast_btn.toolTip() == "投屏"


def test_cast_button_disabled_while_preparing(_qapp):
    view = _view(_video_detail(3), idx=0)
    view._cast_state = "preparing_stream"
    view._update_cast_button()
    assert view.cast_btn.isEnabled() is False
    view._cast_state = "connecting"
    view._update_cast_button()
    assert view.cast_btn.isEnabled() is False


def test_cast_button_stays_available_in_external_vlc_mode(_qapp):
    """外部 VLC 模式下投屏按钮仍可用（与内嵌进度/音量的禁用互不影响）。"""
    view = _view(_video_detail(3), idx=0)
    view._external_active = True
    view._update_cast_button()
    assert view.cast_btn.isEnabled() is True


# --------------------------------------------------------------------- #
# 2. 点击投屏：取流 → 选设备 → start（严格顺序与缓存命中）
# --------------------------------------------------------------------- #
def test_cast_uses_current_episode_index_and_stream_cache(_qapp, monkeypatch):
    view = _view(_video_detail(5), idx=2)
    session, _captured, seen = _start_cast(view, monkeypatch)

    assert view._content.calls == []                    # 缓存命中：不重复取流
    assert seen["titles"] == ["第3集 标题2"]              # 对话框拿到当前集标题
    start = session.calls[0]
    assert start[0] == "start"
    assert start[1].friendly_name == "客厅电视"
    assert start[2] == 2                                 # 用 _current_idx
    assert start[3] == "https://cdn/e2.m3u8"
    assert start[5] == {"Referer": "http://src.example.com/"}
    assert start[6] == {"urls": ["ad"]}
    assert view._cast_active is True
    assert "停止投屏" in view.cast_btn.toolTip()


def test_cast_fetches_stream_when_cache_misses(_qapp, monkeypatch):
    pool = _use_local_pool(monkeypatch)  # 隔离全量套件的全局线程池占用
    view = _view(_video_detail(3), idx=1,
                 content=_FakeContent("https://cdn/fresh.m3u8"))
    session = _FakeSession()
    _install_session(monkeypatch, session)
    _install_proxy(monkeypatch)
    seen = _install_dialog(monkeypatch, renderer=_renderer())

    view._on_cast_clicked()                              # 缓存未命中 → 取流
    assert view._cast_state == "preparing_stream"        # 还在等后台取流
    assert seen["titles"] == []                          # 未开对话框（信号未落地）

    if not _wait_for(lambda: bool(seen["titles"]), timeout=_CAST_WAIT):
        pytest.fail(
            "取流任务未在超时内完成（专用池已隔离全局池；诊断："
            f"content.calls={view._content.calls}"
            f" pending={view._cast_stream_pending}"
            f" task={view._cast_stream_task}"
            f" state={view._cast_state}"
            f" local_pool_active={pool.activeThreadCount()}"
            f" global_pool_active={QThreadPool.globalInstance().activeThreadCount()}）"
        )
    assert view._content.calls == [("http://e/1", "best", True)]
    assert seen["titles"] == ["第2集 标题1"]
    assert _wait_for(lambda: bool(session.calls),
                     timeout=_CAST_WAIT), "后台 start 未提交"
    assert session.calls[0][0] == "start"
    assert session.calls[0][3] == "https://cdn/fresh.m3u8"
    assert view._stream_cache[("http://e/1", "best")] == (
        "https://cdn/fresh.m3u8", "")
    assert view._cast_active is True


def test_cast_dialog_cancel_keeps_idle_and_starts_nothing(_qapp, monkeypatch):
    view = _view(_video_detail(3), idx=0)
    session = _FakeSession()
    _install_session(monkeypatch, session)
    _install_proxy(monkeypatch)
    _install_dialog(monkeypatch, accepted=False)
    view._stream_cache[("http://e/0", "best")] = ("https://cdn/e0.m3u8", "")
    view._on_cast_clicked()
    assert session.calls == []
    assert view._cast_active is False
    assert view._cast_state == "idle"
    assert view.cast_btn.isEnabled() is True
    assert view.cast_btn.toolTip() == "投屏"


def test_cast_start_failure_keeps_local_player_and_reports_error(_qapp, monkeypatch):
    view = _view(_video_detail(3), idx=1)
    view._external_active = True
    terminated = []
    monkeypatch.setattr(ep, "_terminate_previous",
                        lambda: terminated.append(threading.get_ident()))
    session = _FakeSession(start_error=RuntimeError("电视拒绝了 URI"))
    _start_cast(view, monkeypatch, session=session)

    assert terminated == []                              # 本地 VLC 没被停
    assert view._cast_active is False
    assert view._cast_state == "error"
    assert "投屏失败" in view.play_label.text()
    assert "电视拒绝了 URI" in view.cast_btn.toolTip()


def test_local_player_stopped_only_after_tv_reports_playing(_qapp, monkeypatch):
    """start 保留本地 VLC；仅会话回调（电视 PLAYING）在主线程停本地播放。"""
    view = _view(_video_detail(3), idx=0)
    view._external_active = True
    terminated = []
    monkeypatch.setattr(ep, "_terminate_previous",
                        lambda: terminated.append(threading.get_ident()))
    # 轮询恒报 STOPPED：后台轮询不会先触发本地停播，停播只走下面的手动回调
    _session, captured, _seen = _start_cast(
        view, monkeypatch, session=_FakeSession(poll_state_value="STOPPED"))

    assert terminated == []                              # start 之后本地仍在播
    callback = captured["on_local_player_stop"]
    assert callback is not None
    worker = threading.Thread(target=callback)           # 模拟轮询线程回调
    worker.start()
    worker.join()
    assert terminated == []                              # 尚未落地（排队中）
    _drain()
    assert terminated == [threading.get_ident()]         # 回到主线程才执行
    assert view._external_active is False


def test_cast_start_network_runs_in_background_thread(_qapp, monkeypatch):
    """spec §7：session.start()（LAN+SetAVTransportURI+Play）不占 GUI 线程。"""
    view = _view(_video_detail(3), idx=0)
    session = _FakeSession()
    _start_cast(view, monkeypatch, session=session)

    assert session.start_thread_id is not None
    assert session.start_thread_id != threading.get_ident()  # 不在主线程执行


def test_stop_cast_during_start_discards_late_result(_qapp, monkeypatch):
    """启动在途时 _stop_cast（换源/关页）作废它：迟到结果只补 stop、不复活状态。"""
    view = _view(_video_detail(3), idx=0)
    session = _FakeSession()
    _install_session(monkeypatch, session)
    _install_proxy(monkeypatch)
    _install_dialog(monkeypatch, renderer=_renderer())
    ep0 = view._episodes[0]
    view._stream_cache[(ep0.url, view._quality)] = ("https://cdn/e0.m3u8", "")

    view._on_cast_clicked()                              # 提交后台 start（在途）
    assert view._cast_state == "connecting"
    view._stop_cast()                                    # 此刻作废在途启动
    assert view._cast_state == "idle"
    assert view._cast_session is None

    assert _wait_for(lambda: ("stop",) in session.calls,
                     timeout=_CAST_WAIT), "陈旧启动未补 stop"
    assert view._cast_session is None                    # 迟到结果不复活会话
    assert view._cast_active is False
    assert view._cast_state == "idle"
    assert view._cast_poll_task is None
    assert view._cast_start_task is None


# --------------------------------------------------------------------- #
# 2b. 取流失败状态（T5-3）与代际守卫（T5-4）
# --------------------------------------------------------------------- #
def test_cast_stream_failure_marks_error_and_allows_retry(_qapp, monkeypatch):
    """首次投屏取流失败 → error 态 + 原因可见 + 按钮可点重试（非静默 idle）。"""
    _use_local_pool(monkeypatch)
    view = _view(_video_detail(3), idx=0, content=_FakeContent(err="解析失败"))
    session = _FakeSession()
    _install_session(monkeypatch, session)
    _install_proxy(monkeypatch)
    _install_dialog(monkeypatch, renderer=_renderer())

    view._on_cast_clicked()                              # 缓存未命中 → 取流失败
    assert _wait_for(lambda: view._cast_state == "error",
                     timeout=_CAST_WAIT), "取流失败未进 error 态"
    assert view._cast_error == "解析失败"
    assert view.cast_btn.isEnabled() is True             # 可重试
    assert "投屏失败" in view.cast_btn.toolTip()
    assert "解析失败" in view.cast_btn.toolTip()
    assert session.calls == []                           # 未启动会话
    assert view._cast_active is False


def test_episode_switch_stream_failure_keeps_cast_state(_qapp, monkeypatch):
    """投屏中切集取流失败（spec §6.5）：保留原集与当前播放状态，只提示错误。

    失败分支若把 _cast_state 误置 idle，_cast_toggle_play_pause 会把
    「应暂停」误判为「播放」（cast_state != "playing" → play()）。
    """
    view = _view(_video_detail(5), idx=1)
    session = _FakeSession()
    view._cast_session = session
    view._cast_active = True
    view._cast_state = "playing"
    view._cast_stream_pending = (2, "episode")

    view._on_cast_stream_ready("http://e/2", "", "", "取流挂了", "best",
                               view._cast_gen)
    assert view._cast_state == "playing"                 # 不被误置 idle
    assert view._cast_active is True
    assert ("stop",) not in session.calls                # 不自动关投屏
    assert "取流挂了" in view.play_label.text()           # 错误可见
    view._cast_toggle_play_pause()
    assert ("pause",) in session.calls                   # 「应暂停」不误判成播放


def test_stale_stream_result_never_consumes_new_pending(_qapp, monkeypatch):
    """旧取流任务迟到：不得消费/覆盖新 pending，否则卡死 preparing_stream。"""
    _use_local_pool(monkeypatch)
    view = _view(_video_detail(3), idx=2)
    _install_session(monkeypatch, _FakeSession())
    _install_proxy(monkeypatch)
    seen = _install_dialog(monkeypatch, renderer=_renderer())
    view._cast_state = "preparing_stream"

    view._cast_resolve_stream(1, "start")                # 第一次取流在途
    gen1 = view._cast_gen
    view._cast_resolve_stream(2, "start")                # 再次取流（pending 换新）
    gen2 = view._cast_gen
    assert gen2 != gen1
    assert view._cast_stream_pending == (2, "start")

    # 陈旧任务先回：代数不符 → 只进缓存，pending/任务引用原样保留
    view._on_cast_stream_ready("http://e/1", "https://cdn/old.m3u8", "", None,
                               "best", gen1)
    assert view._cast_stream_pending == (2, "start")
    assert view._cast_stream_task is not None
    assert seen["titles"] == []

    # 当代任务后回：正常消费 pending 并续接投屏流程
    view._on_cast_stream_ready("http://e/2", "https://cdn/new.m3u8", "", None,
                               "best", gen2)
    assert view._cast_stream_pending is None
    assert view._cast_stream_task is None
    assert seen["titles"] == ["第3集 标题2"]


# --------------------------------------------------------------------- #
# 3. cast_active 控制路由到 CastSession
# --------------------------------------------------------------------- #
def test_play_pause_seek_route_to_cast_session(_qapp):
    view = _view(_video_detail(5), idx=1)
    session = _FakeSession()
    view._cast_session = session
    view._cast_active = True
    view._cast_duration = 120

    view._cast_state = "playing"
    view._toggle_play_pause()
    assert ("pause",) in session.calls

    view._cast_state = "paused"
    view._toggle_play_pause()
    assert ("play",) in session.calls

    view.progress.setValue(500)                          # 120s 的 50% = 60s
    view._on_cast_progress_released()
    assert ("seek", 60) in session.calls


def test_prev_next_episode_route_to_set_episode(_qapp):
    view = _view(_video_detail(5), idx=1)
    session = _FakeSession()
    view._cast_session = session
    view._cast_active = True
    for i in range(5):
        view._stream_cache[(f"http://e/{i}", "best")] = (f"https://cdn/e{i}.m3u8", "")

    view._on_next_ep()
    assert ("set_episode", 2, "https://cdn/e2.m3u8", "第3集 标题2") in session.calls
    assert view._current_idx == 2

    view._on_prev_ep()
    assert ("set_episode", 1, "https://cdn/e1.m3u8", "第2集 标题1") in session.calls
    assert view._current_idx == 1


def test_controls_keep_local_behaviour_when_not_casting(_qapp, monkeypatch):
    """未投屏：播放/选集走原有本地路径，不碰 CastSession（会话对象在场也一样）。"""
    view = _view(_video_detail(3), idx=0)
    session = _FakeSession()
    view._cast_session = session          # 即便句柄在场，未投屏就不该路由
    view._cast_active = False
    loaded = []
    plays = []
    monkeypatch.setattr(view, "_load_episode", lambda i: loaded.append(i))
    monkeypatch.setattr(view, "_play", lambda v, a, t: plays.append((v, a, t)))
    view._current_play = "https://cdn/cur.m3u8"

    view._select_episode(1)
    assert loaded == [1]

    view._current_idx = 1
    view._toggle_play_pause()
    assert plays == [("https://cdn/cur.m3u8", "", "")]
    assert session.calls == []


def test_volume_stays_disabled_during_cast(_qapp, monkeypatch):
    """RenderingControl 音量链路无实现 → 投屏期间音量控件保持禁用（诚实于现状）。

    有 RC 的设备启用一个拖动无效的死控件比禁用更糟（T5-5）：tooltip 说明
    「暂未实现」；无 RC 设备提示「不支持」。补链路后可恢复按 RC 启用。
    """
    view = _view(_video_detail(3), idx=0)
    _start_cast(view, monkeypatch, renderer=_renderer("http://rc"))
    assert view.vol_slider.isEnabled() is False
    assert view.vol_btn.isEnabled() is False
    assert "未实现" in view.vol_slider.toolTip()

    view2 = _view(_video_detail(3), idx=0)
    _start_cast(view2, monkeypatch, renderer=_renderer(None))
    assert view2.vol_slider.isEnabled() is False
    assert view2.vol_btn.isEnabled() is False
    assert "不支持" in view2.vol_slider.toolTip()


# --------------------------------------------------------------------- #
# 4. 后台轮询 → Qt signal → 主线程更新 UI
# --------------------------------------------------------------------- #
def test_poll_state_updates_progress_time_and_play_button(_qapp):
    from gui.pages.reader.video_view import _fmt_hms

    view = _view(_video_detail(3), idx=0)
    view._cast_session = _FakeSession()
    view._cast_active = True
    view._cast_device = "客厅电视"
    view._cast_duration = 0

    view._on_cast_state({"state": "PLAYING", "position": 60, "duration": 120,
                         "track": 1, "uri": "u"})
    assert view.progress.value() == 500
    assert view.time_label.text() == f"{_fmt_hms(60)} / {_fmt_hms(120)}"
    assert view.play_btn.text() == "⏸"
    assert view._cast_state == "playing"
    assert "客厅电视" in view.cast_btn.toolTip()

    view._on_cast_state({"state": "PAUSED_PLAYBACK", "position": 60,
                         "duration": 120, "track": 1, "uri": "u"})
    assert view.play_btn.text() == "▶"
    assert view._cast_state == "paused"


def test_poll_task_emits_then_fails_after_three_consecutive_errors(_qapp):
    from gui.pages.reader.video_view import _CastPollTask

    session = _FakeSession(poll_error=OSError("network down"))
    task = _CastPollTask(session, max_failures=3)
    seen = []
    task.signals.poll_failed.connect(seen.append)

    assert task.tick() is True
    assert task.tick() is True
    assert task.running is True
    assert task.tick() is False                           # 第 3 次 → 停止
    assert task.running is False
    assert seen == ["network down"]


def test_poll_task_resets_failure_count_on_success(_qapp):
    from gui.pages.reader.video_view import _CastPollTask

    class _Flaky:
        def __init__(self):
            self.n = 0

        def poll_state(self):
            self.n += 1
            if self.n in (1, 2):
                raise OSError("blip")
            return {"state": "PLAYING", "position": 0, "duration": 10,
                    "track": 1, "uri": "u"}

    task = _CastPollTask(_Flaky(), max_failures=3)
    assert task.tick() is True      # 失败 1
    assert task.tick() is True      # 失败 2
    assert task.tick() is True      # 成功 → 清零
    assert task.tick() is True      # 失败 1（重新计数）
    assert task.running is True


def test_poll_worker_only_emits_signal_and_never_touches_widgets(_qapp):
    """worker 边界：只 emit；控件更新全在主线程槽里完成（信号落地前 UI 不动）。"""
    from gui.pages.reader.video_view import _CastPollTask

    view = _view(_video_detail(3), idx=0)
    view._cast_session = _FakeSession()
    view._cast_active = True
    view._cast_device = "客厅电视"
    task = _CastPollTask(view._cast_session, max_failures=3)
    task.signals.state_changed.connect(view._on_cast_state)

    worker = threading.Thread(target=task.tick)
    worker.start()
    worker.join()
    # 线程已结束、事件未处理 → 控件纹丝未动：证明改控件的不是 worker
    assert view.progress.value() == 0
    assert view.play_btn.text() == "▶"

    _drain()                                              # 队列投递到主线程
    assert view.progress.value() == 500
    assert view.time_label.text() == "00:01:00 / 00:02:00"
    assert view.play_btn.text() == "⏸"


def test_three_poll_failures_mark_error_and_cleanup_session(_qapp, monkeypatch):
    view = _view(_video_detail(3), idx=0)
    session = _FakeSession()
    _start_cast(view, monkeypatch, session=session)
    task = view._cast_poll_task
    assert task is not None

    view._on_cast_poll_failed("与电视失去连接")
    assert view._cast_state == "error"
    assert view._cast_active is False
    assert view._cast_poll_task is None
    assert ("stop",) in session.calls
    assert "投屏失败" in view.play_label.text()
    assert "与电视失去连接" in view.cast_btn.toolTip()


# --------------------------------------------------------------------- #
# 4b. 8 秒 PLAYING 超时（spec 6.4 步骤 6，T5-I1）
# --------------------------------------------------------------------- #
def test_playing_timeout_stops_cast_keeps_local_and_allows_retry(_qapp, monkeypatch):
    """启动后 8 秒电视仍未 PLAYING → 投屏失败：关会话/轮询、保留本地 VLC、可重试。"""
    view = _view(_video_detail(3), idx=0)
    view._external_active = True
    terminated = []
    monkeypatch.setattr(ep, "_terminate_previous",
                        lambda: terminated.append(threading.get_ident()))
    # 轮询恒报 STOPPED：电视接受 Play 但状态不进 PLAYING（3 次失败判据不触发）
    session = _FakeSession(poll_state_value="STOPPED")
    _start_cast(view, monkeypatch, session=session)
    task = view._cast_poll_task
    assert task is not None
    assert view._cast_playing_timer.isActive() is True   # 启动即开始 8s 倒计时
    assert view._cast_playing_timer.interval() == 8000

    view._on_cast_playing_timeout()                      # 模拟 8 秒到期

    assert view._cast_state == "error"
    assert view._cast_error == "电视未在 8 秒内开始播放"
    assert view._cast_active is False
    assert view._cast_session is None
    assert view._cast_poll_task is None
    assert task.running is False
    assert ("stop",) in session.calls                    # 会话/端点已收
    assert terminated == []                              # 本地 VLC 保留
    assert "投屏失败" in view.play_label.text()
    assert "8 秒" in view.cast_btn.toolTip()
    assert view.cast_btn.isEnabled() is True             # 可点重试
    assert view._cast_playing_timer.isActive() is False


def test_playing_timer_stops_once_tv_plays(_qapp, monkeypatch):
    """电视进 PLAYING（或其后被暂停）即撤掉 8s 超时定时器，不再判失败。"""
    view = _view(_video_detail(3), idx=0)
    session = _FakeSession(poll_state_value="STOPPED")
    _start_cast(view, monkeypatch, session=session)
    assert view._cast_playing_timer.isActive() is True

    view._on_cast_state({"state": "PLAYING", "position": 0, "duration": 10,
                         "track": 1, "uri": "u"})
    assert view._cast_playing_timer.isActive() is False
    assert view._cast_state == "playing"

    view._on_cast_playing_timeout()                      # 迟到的超时触发必须无害
    assert view._cast_session is not None                # 已 PLAYING 不判失败
    assert view._cast_state == "playing"


# --------------------------------------------------------------------- #
# 5. 生命周期清理
# --------------------------------------------------------------------- #
def test_stop_cast_cleans_session_poll_and_resets_button(_qapp, monkeypatch):
    view = _view(_video_detail(3), idx=0)
    session, _c, _s = _start_cast(view, monkeypatch)
    task = view._cast_poll_task
    assert task is not None and task.running is True

    view._on_cast_clicked()                              # 第二次点击 = 停止投屏

    assert task.running is False
    assert view._cast_poll_task is None
    assert ("stop",) in session.calls
    assert view._cast_active is False
    assert view._cast_state == "idle"
    assert view.cast_btn.toolTip() == "投屏"
    assert view.play_btn.text() == "▶"


def _casting(monkeypatch, n=3):
    view = _view(_video_detail(n), idx=0)
    session, _c, _s = _start_cast(view, monkeypatch)
    assert view._cast_active is True
    monkeypatch.setattr(view, "_maybe_load_recommendations", lambda: None)
    monkeypatch.setattr(ep, "_terminate_previous", lambda: None)
    return view, session


def test_load_new_work_stops_cast_session(_qapp, monkeypatch):
    view, session = _casting(monkeypatch)
    view.load(object(), _video_detail(4))
    assert ("stop",) in session.calls
    assert view._cast_active is False


def test_reload_detail_stops_cast_session(_qapp, monkeypatch):
    view, session = _casting(monkeypatch)
    view.reload_detail(_video_detail(2))
    assert ("stop",) in session.calls
    assert view._cast_active is False


def test_stop_playback_stops_cast_session(_qapp, monkeypatch):
    view, session = _casting(monkeypatch)
    view.stop_playback()
    assert ("stop",) in session.calls
    assert view._cast_active is False


def test_shutdown_video_stops_cast_session(_qapp, monkeypatch):
    view, session = _casting(monkeypatch)
    view.shutdown_video()
    assert ("stop",) in session.calls
    assert view._cast_active is False


def test_stop_cast_is_idempotent(_qapp, monkeypatch):
    view = _view(_video_detail(3), idx=0)
    session, _c, _s = _start_cast(view, monkeypatch)
    view._stop_cast()
    view._stop_cast()
    assert session.calls.count(("stop",)) == 1
    assert view._cast_session is None
