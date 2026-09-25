# -*- coding: utf-8 -*-
"""外部播放器：多集播放列表（VLC 内 N/P 切上下集）+ 旧进程清理。

不真正拉起 VLC——mock _locate_vlc / proxy_url_for / subprocess.Popen，
只断言命令参数（args）组装顺序、代理覆盖与进程清理（terminate）行为。
"""
import framework.external_player as ep

_VLC = r"C:\Program Files\VideoLAN\VLC\vlc.exe"

# VLC HTTP 控制接口参数块（放全局选项区、MRL 之前）——与实现同步。
_CONTROL_ARGS = [
    "--extraintf=http", "--http-host=127.0.0.1",
    "--http-port=8090", "--http-password=TESTPWD",
]


class _FakeProc:
    """模拟 Popen 返回对象：记录 args、统计 terminate 调用。"""

    def __init__(self):
        self.args = []
        self.terminated = 0

    def terminate(self):
        self.terminated += 1


def _install(monkeypatch, proxy=None) -> list:
    """mock 播放器探测/代理/拉起；返回每次 Popen 收到的 _FakeProc 列表。"""
    if proxy is None:
        proxy = lambda u, *a, **k: "P:" + u
    procs = []

    def _popen(args, **kw):
        fp = _FakeProc()
        fp.args = list(args)
        procs.append(fp)
        return fp

    monkeypatch.setattr(ep, "_locate_vlc", lambda: _VLC)
    monkeypatch.setattr(ep, "proxy_url_for", proxy)
    monkeypatch.setattr(ep.subprocess, "Popen", _popen)
    monkeypatch.setattr(ep, "_pick_http_port", lambda: 8090)      # 端口固定，方便精确断言
    monkeypatch.setattr(ep.secrets, "token_hex", lambda n: "TESTPWD")
    monkeypatch.setattr(ep, "_last_proc", None)
    monkeypatch.setattr(ep, "_control_state", None)
    return procs


def test_episodes_none_single_mrl(monkeypatch):
    """episodes=None（老调用）：只一个 MRL，不追加多余项。"""
    procs = _install(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    args = procs[0].args
    assert args[0] == _VLC
    assert args[1] == "--no-video-title-show"
    assert args[2] == "--no-drop-late-frames"
    assert args[3].startswith("--network-caching=")
    # VLC HTTP 控制接口参数在全局选项区、MRL 之前
    assert args[4:8] == _CONTROL_ARGS
    assert args[8] == "P:https://cdn.example.com/hls/a.m3u8"
    assert len(args) == 9
    # 控制状态已写入（供 player_command 转发按键）
    assert ep._control_state == {
        "host": "127.0.0.1", "port": 8090, "password": "TESTPWD",
    }


def test_episodes_order_current_first(monkeypatch):
    """episodes 传入：全局选项在前 → 当前集 MRL 首位 + 逐集音频 slave 跟其 URL。"""
    seen = []

    def _proxy(u, *a, **k):
        seen.append(u)
        return "P:" + u

    procs = _install(monkeypatch, proxy=_proxy)
    ep.open_with_player(
        "https://cdn.example.com/hls/a.m3u8",
        audio="https://cdn.example.com/hls/a-a.m3u8",
        episodes=[
            ("https://cdn.example.com/hls/a.m3u8", "https://cdn.example.com/hls/a-a.m3u8", "第1集"),
            ("https://cdn.example.com/hls/b.m3u8", "https://cdn.example.com/hls/b-a.m3u8", "第2集"),
            ("https://cdn.example.com/hls/c.m3u8", "", "第3集"),
        ],
    )
    args = procs[0].args
    assert args[0] == _VLC  # 全局选项保持在所有 MRL 之前
    assert args[1] == "--no-video-title-show"
    assert args[2] == "--no-drop-late-frames"
    assert args[3].startswith("--network-caching=")
    assert args[4:8] == _CONTROL_ARGS  # 控制接口参数也在全局选项区
    # 当前集放首位（主 MRL）+ 其音频 input-slave
    assert args[8] == "P:https://cdn.example.com/hls/a.m3u8"
    assert args[9] == ":input-slave=P:https://cdn.example.com/hls/a-a.m3u8"
    # 其余逐集追加：URL 后紧跟该集音频 slave（无音频则不加）
    assert args[10:13] == [
        "P:https://cdn.example.com/hls/b.m3u8",
        ":input-slave=P:https://cdn.example.com/hls/b-a.m3u8",
        "P:https://cdn.example.com/hls/c.m3u8",
    ]
    assert len(args) == 13
    # 所有上游 URL（主媒体 + 音频 + 各集）一律走了同一代理拼接
    assert seen == [
        "https://cdn.example.com/hls/a.m3u8",
        "https://cdn.example.com/hls/a-a.m3u8",
        "https://cdn.example.com/hls/b.m3u8",
        "https://cdn.example.com/hls/b-a.m3u8",
        "https://cdn.example.com/hls/c.m3u8",
    ]


def test_episodes_empty_url_skipped(monkeypatch):
    """列表内某集缺流（URL 为空）→ 整集跳过，不影响其余集。"""
    seen = []

    def _proxy(u, *a, **k):
        seen.append(u)
        return "P:" + u

    procs = _install(monkeypatch, proxy=_proxy)
    ep.open_with_player(
        "https://cdn.example.com/hls/a.m3u8",
        episodes=[
            ("https://cdn.example.com/hls/a.m3u8", "", "第1集"),
            ("", "https://cdn.example.com/hls/b-a.m3u8", "第2集"),  # 缺集
            ("https://cdn.example.com/hls/c.m3u8", "", "第3集"),
        ],
    )
    args = procs[0].args
    assert "P:https://cdn.example.com/hls/b.m3u8" not in args
    assert args[8] == "P:https://cdn.example.com/hls/a.m3u8"
    assert args[9] == "P:https://cdn.example.com/hls/c.m3u8"
    assert len(args) == 10
    assert seen == [
        "https://cdn.example.com/hls/a.m3u8",
        "https://cdn.example.com/hls/c.m3u8",
    ]


def test_second_call_terminates_previous(monkeypatch):
    """再次打开会先 terminate 上一次的 Popen，当前实例不受影响。"""
    procs = _install(monkeypatch)
    ep.open_with_player(
        "https://cdn.example.com/hls/a.m3u8",
        episodes=[
            ("https://cdn.example.com/hls/a.m3u8", "", "A"),
            ("https://cdn.example.com/hls/b.m3u8", "", "B"),
        ],
    )
    ep.open_with_player(
        "https://cdn.example.com/hls/b.m3u8",
        episodes=[("https://cdn.example.com/hls/b.m3u8", "", "B")],
    )
    assert len(procs) == 2
    assert procs[0].terminated == 1  # 第一次实例被第二次打开先 terminate
    assert procs[1].terminated == 0  # 当前实例在播，不被 terminate


def test_terminate_race_safe(monkeypatch):
    """旧进程已自行退出 → terminate 抛异常被吞，不影响第二次拉起（幂等）。"""
    procs = _install(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    assert len(procs) == 1
    procs[0].terminate = lambda *a, **k: (_ for _ in ()).throw(OSError("gone"))
    ep.open_with_player("https://cdn.example.com/hls/b.m3u8")
    assert len(procs) == 2
    assert procs[1].args[-1] == "P:https://cdn.example.com/hls/b.m3u8"


def test_terminate_clears_control_state(monkeypatch):
    """关旧播放器时清空控制会话：新开后会按新端口/密码重建。"""
    procs = _install(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    assert ep._control_state is not None

    def _next_port():
        return 8091

    def _next_pwd(n):
        return "NEWPWD"

    monkeypatch.setattr(ep, "_pick_http_port", _next_port)
    monkeypatch.setattr(ep.secrets, "token_hex", _next_pwd)
    ep.open_with_player("https://cdn.example.com/hls/b.m3u8")
    assert procs[0].terminated == 1
    assert procs[1].args[7] == "--http-password=NEWPWD"  # 重建后的新会话
    assert ep._control_state["port"] == 8091
    assert ep._control_state["password"] == "NEWPWD"


def test_force_proxy_forwarded_to_proxy(monkeypatch):
    """force_proxy=True → proxy_url_for 每次调用都收到 force_proxy=True。"""
    seen = []

    def _proxy(u, *a, **k):
        seen.append(k.get("force_proxy"))
        return "P:" + u

    procs = _install(monkeypatch, proxy=_proxy)
    ep.open_with_player(
        "https://cdn.example.com/hls/a.m3u8",
        audio="https://cdn.example.com/hls/a-a.m3u8",
        headers={"Referer": "https://fake.example/"},
        episodes=[
            ("https://cdn.example.com/hls/a.m3u8", "https://cdn.example.com/hls/a-a.m3u8", "第1集"),
            ("https://cdn.example.com/hls/b.m3u8", "", "第2集"),
        ],
        force_proxy=True,
    )
    # 主 MRL(1) + 音频轨(1) + episodes 从第 2 项起每集(1) 均强制代理
    assert seen == [True, True, True]


def test_force_proxy_default_false(monkeypatch):
    """不传 force_proxy（默认）→ 与旧行为一致：无强制代理标志。"""
    seen = []

    def _proxy(u, *a, **k):
        seen.append(k.get("force_proxy"))
        return "P:" + u

    procs = _install(monkeypatch, proxy=_proxy)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8",
                        headers={"Referer": "https://fake.example/"})
    assert seen == [False]  # 默认 False，明确传给代理但值为 False