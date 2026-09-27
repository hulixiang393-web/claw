# -*- coding: utf-8 -*-
"""外部播放器：全集播放列表契约（顺序/标题/flag/分类/裁剪）+ 旧进程清理。

不真正拉起 VLC——mock _locate_vlc / proxy_url_for / subprocess.Popen，
只断言命令参数（args）组装顺序、代理覆盖与进程清理（terminate）行为。

**episodes 契约（有意破坏）**：episodes 是**全集完整有序列表**，
episodes[i] = 第 i 集（0-based），episodes[0] **不再被跳过**；
url 只用于定位起始项与分类。**正常路径全集按 1..N 严格入列**——当前集在列内
中段（从第 5 集开播时第 1..4 集也在列里，用户能在 VLC 里往回选），定位靠启动
握手；只有全集超出 _SERIES_MAX_MRL 条数 / _SERIES_MAX_CMD 总字符数时，才降级为
「当前集往后」的窗口。
"""
import framework.external_player as ep

_VLC = r"C:\Program Files\VideoLAN\VLC\vlc.exe"

_CONTROL_ARGS = [
    "--extraintf=http", "--http-host=127.0.0.1",
    "--http-port=8090", "--http-password=TESTPWD",
]


class _FakeProc:
    """模拟 Popen 返回对象：记录 args、统计 terminate 调用。"""

    def __init__(self):
        self.args = []
        self.terminated = 0
        self.waited = 0

    def terminate(self):
        self.terminated += 1

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.waited += 1
        return 0


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
    monkeypatch.setattr(ep, "_pick_http_port", lambda: 8090)
    monkeypatch.setattr(ep.secrets, "token_hex", lambda n: "TESTPWD")
    monkeypatch.setattr(ep, "_last_proc", None)
    monkeypatch.setattr(ep, "_control_state", None)
    monkeypatch.setattr(ep, "_playlist_items", [])
    # raising=False：本任务还不实现 _start_playlist_sync（Task 5 才加），
    # 默认 raising=True 会 AttributeError 让本任务全部测试报错。
    monkeypatch.setattr(ep, "_start_playlist_sync", lambda *a, **k: None,
                        raising=False)
    return procs


def test_episodes_none_single_mrl(monkeypatch):
    """episodes=None（老调用）：只一个 MRL，不追加多余项，无系列 flag。"""
    procs = _install(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    args = procs[0].args
    assert args[0] == _VLC
    assert args[1] == "--no-video-title-show"
    assert args[2] == "--no-drop-late-frames"
    assert args[3].startswith("--network-caching=")
    assert args[4:8] == _CONTROL_ARGS
    assert args[8] == "P:https://cdn.example.com/hls/a.m3u8"
    assert len(args) == 9
    assert "--no-random" not in args
    assert "--no-playlist-autostart" not in args
    assert ep._control_state == {
        "host": "127.0.0.1", "port": 8090, "password": "TESTPWD",
    }


def test_episodes_full_series_in_order_with_titles(monkeypatch):
    """全集按 0..N-1 顺序入列（**含当前集之前的集**），每条带 #第NN集 标题。

    从第 3 集开播时，第 1、2 集**仍须在列**——用户在 VLC 里可以往回选，
    App 侧切集也依赖整列的 id 映射。定位当前集是启动握手的职责。
    """
    procs = _install(monkeypatch)
    eps = [(f"https://cdn.example.com/hls/{c}.m3u8", "", f"第{i + 1}集 章节{c}")
           for i, c in enumerate("abc")]
    ep.open_with_player(eps[2][0], episodes=eps)
    args = procs[0].args
    assert args[4:8] == _CONTROL_ARGS
    assert args[8] == "--no-random"
    # 起始项是第 3 集（非首项）→ 需 no-playlist-autostart + 握手定位
    assert args[9] == "--no-playlist-autostart"
    assert args[10:] == [
        "P:https://cdn.example.com/hls/a.m3u8#第1集 章节a",
        "P:https://cdn.example.com/hls/b.m3u8#第2集 章节b",
        "P:https://cdn.example.com/hls/c.m3u8#第3集 章节c",
    ]
    # 系列路径不挂 input-slave，也不另加主 MRL
    assert not any(a.startswith(":input-slave=") for a in args)
    assert not any(a.startswith("P:") and "#" not in a for a in args)


def test_handshake_receives_start_idx_and_whole_series(monkeypatch):
    """握手按 (start_idx, 入列全体 window, 回调) 收参。

    全集入列后当前集在**中段**：传 window[0][1]（旧契约的 window 恒从当前集起）
    会把第 1 集的 MRL 当成起始项，pl_play 定位到第 1 集——整个握手定位错位。
    """
    procs = _install(monkeypatch)
    seen = {}
    monkeypatch.setattr(ep, "_start_playlist_sync",
                        lambda *a, **k: seen.update(args=a), raising=False)
    eps = [(f"https://cdn.example.com/hls/{c}.m3u8", "", f"第{i + 1}集")
           for i, c in enumerate("abc")]
    ready = lambda mapping: None  # noqa: E731 —— 仅占位，握手在 Task 5 实现
    ep.open_with_player(eps[2][0], episodes=eps, on_playlist_ready=ready)
    start_idx, series, cb = seen["args"]
    assert start_idx == 2
    assert series == [
        (0, "P:https://cdn.example.com/hls/a.m3u8#第1集"),
        (1, "P:https://cdn.example.com/hls/b.m3u8#第2集"),
        (2, "P:https://cdn.example.com/hls/c.m3u8#第3集"),
    ]
    assert cb is ready
    # 传的必须就是真正入列的那些 MRL（args 去掉全局选项/flag 的尾部）
    assert [m for _i, m in series] == procs[0].args[10:]


def test_no_handshake_without_episodes(monkeypatch):
    """episodes 为 None 或空列表 → 不起握手（没有播放列表要对齐）。"""
    _install(monkeypatch)
    seen = []
    monkeypatch.setattr(ep, "_start_playlist_sync",
                        lambda *a, **k: seen.append(a), raising=False)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    ep.open_with_player("https://cdn.example.com/hls/b.m3u8", episodes=[])
    assert seen == []


def test_first_episode_no_autostart_flag(monkeypatch):
    """起始项 == episodes[0] → 不加 --no-playlist-autostart（走原生 autostart）。"""
    procs = _install(monkeypatch)
    eps = [("https://cdn.example.com/hls/a.m3u8", "", "第01集"),
           ("https://cdn.example.com/hls/b.m3u8", "", "第02集")]
    ep.open_with_player(eps[0][0], episodes=eps)
    args = procs[0].args
    assert "--no-playlist-autostart" not in args
    assert "--no-random" in args
    # episodes[0] **不再被跳过**：全集（含首项）按序入列
    assert args[-2:] == [
        "P:https://cdn.example.com/hls/a.m3u8#第01集",
        "P:https://cdn.example.com/hls/b.m3u8#第02集",
    ]


def test_episodes_empty_url_skipped(monkeypatch):
    """列表内某集缺流（URL 为空）→ 整集跳过，不影响其余集。"""
    procs = _install(monkeypatch)
    eps = [("https://cdn.example.com/hls/a.m3u8", "", "第01集"),
           ("", "", "第02集"),
           ("https://cdn.example.com/hls/c.m3u8", "", "第03集")]
    ep.open_with_player(eps[0][0], episodes=eps)
    args = procs[0].args
    assert [a for a in args if a.startswith("P:")] == [
        "P:https://cdn.example.com/hls/a.m3u8#第01集",
        "P:https://cdn.example.com/hls/c.m3u8#第03集",
    ]


def test_title_sanitized():
    assert ep._sanitize_title("第01集 标题#带井号", 0) == "第01集 标题 带井号"
    assert ep._sanitize_title("第01集\n换行\t制表", 0) == "第01集 换行 制表"
    assert ep._sanitize_title("", 4) == "第5集"          # 空 → 集号兜底
    assert ep._sanitize_title("  ", 0) == "第1集"
    assert ep._sanitize_title("123 数字开头", 0).startswith("集")
    assert not ep._sanitize_title("9abc", 0)[0].isdigit()


def test_classify_url_used_for_caching(monkeypatch):
    """classify_url 非空 → --network-caching 取自真实流地址（ikanpp 30s 保住）。

    刻意**不传** caching_ms：30000 只能来自 classify(classify_url)，
    否则这条断言证明不了 classify_url 真的被用了。
    """
    procs = _install(monkeypatch)
    seen = {}

    def _classify(target):
        seen["target"] = target
        from types import SimpleNamespace
        return SimpleNamespace(kind="hls", buffer_ms=30000)

    import framework.media_tuner as mt
    monkeypatch.setattr(mt, "classify", _classify)
    eps = [("http://127.0.0.1:9/e/k/0", "", "第01集")]
    ep.open_with_player(eps[0][0], episodes=eps,
                        classify_url="https://cdn.example.com/real/x.m3u8")
    assert seen["target"] == "https://cdn.example.com/real/x.m3u8"
    assert procs[0].args[3] == "--network-caching=30000"


def test_classify_url_empty_falls_back_to_url(monkeypatch):
    """classify_url 为空 → 仍按 url 分类（老行为不变）。

    用 HLS URL：_resolve 对 .m3u8 一律包代理，故 play_url != url 命中「多一跳
    → 缓冲 8s 起」分支（分类给的 2000 被加码吃掉）。
    """
    procs = _install(monkeypatch)
    seen = {}

    def _classify(target):
        seen["target"] = target
        from types import SimpleNamespace
        return SimpleNamespace(kind="mp4", buffer_ms=2000)

    import framework.media_tuner as mt
    monkeypatch.setattr(mt, "classify", _classify)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    assert seen["target"] == "https://cdn.example.com/hls/a.m3u8"
    assert procs[0].args[3] == "--network-caching=8000"  # 走代理多一跳 → 8s 起


def test_local_proxy_episode_not_double_proxied(monkeypatch):
    """惰性 /e/ URL 已是代理 URL → 不得再包一层代理。"""
    seen = []
    procs = _install(monkeypatch, proxy=lambda u, *a, **k: (
        seen.append(u) or "P:" + u))
    eps = [("http://127.0.0.1:9/e/k/0", "", "第01集"),
           ("http://127.0.0.1:9/e/k/1", "", "第02集")]
    ep.open_with_player(eps[0][0], episodes=eps,
                        headers={"Referer": "https://x/"})
    assert seen == []          # 一层代理都没加
    assert procs[0].args[-2:] == [
        "http://127.0.0.1:9/e/k/0#第01集",
        "http://127.0.0.1:9/e/k/1#第02集",
    ]
    # 顺带锁缓冲下限：url 本身就是代理 URL（play_url == url），"多一跳"判不
    # 出来，只能靠 _is_local_proxy_url(url) 兜底，否则 /e/ 会掉回 2500ms。
    assert procs[0].args[3] == "--network-caching=8000"


def test_oversized_series_truncated(monkeypatch):
    """上千集不撑爆 Windows 命令行：按 _SERIES_MAX_MRL 裁剪且不抛异常。"""
    procs = _install(monkeypatch)
    eps = [(f"http://127.0.0.1:9/e/k/{i}", "", f"第{i + 1}集 很长很长很长标题")
           for i in range(1200)]
    msg = ep.open_with_player(eps[0][0], episodes=eps)
    assert msg.startswith("已用外部播放器打开")
    assert "截断" in msg                  # 截断必须提示，否则用户以为全集都在
    args = procs[0].args
    mrl_count = sum(1 for a in args if "/e/k/" in a)
    assert mrl_count == ep._SERIES_MAX_MRL   # 填到条数上限为止
    assert sum(len(a) for a in args) < 32000  # 远低于 CreateProcess 的 32767


def test_fit_series_full_series_kept_when_it_fits():
    """装得下 → 全集 0..N-1 入列，当前集之前的集**也在列内**。"""
    eps = [(f"u{i}", "", f"第{i + 1}集") for i in range(10)]
    items, truncated = ep._fit_series(eps, 4, lambda u, a: u)
    assert truncated is False
    assert [i for i, _ in items] == list(range(10))   # 全集，不是 4..9
    assert items[0][0] == 0 and len(items) == 10


def test_fit_series_degrades_to_forward_window_on_overflow():
    """装不下 → 降级为「当前集往后」的窗口；超长剧集选很靠后的集也能播。"""
    long_title = "标题很长很长很长很长很长很长很长很长很长很长很长"
    many = [(f"u{i}", "", f"第{i + 1}集 {long_title}") for i in range(400)]
    items, trunc = ep._fit_series(many, 0, lambda u, a: u)
    assert trunc is True
    assert len(items) < 400 and items[0][0] == 0
    assert len(items) <= ep._SERIES_MAX_MRL
    # 当前集在已解析范围之外 → 必须以当前集为首重新取窗口，且不丢当前集
    items2, trunc2 = ep._fit_series(many, 390, lambda u, a: u)
    assert trunc2 is True
    assert items2[0][0] == 390
    assert all(i >= 390 for i, _ in items2)


def test_fit_series_budget_caps_respected():
    """两个上限都真实生效：条数上限与总字符上限各自能触发截断。"""
    # 条数上限
    many = [(f"u{i}", "", f"第{i + 1}集") for i in range(ep._SERIES_MAX_MRL + 50)]
    items, trunc = ep._fit_series(many, 0, lambda u, a: u)
    assert trunc is True and len(items) == ep._SERIES_MAX_MRL
    # 字符上限：条数很少但每条超长
    fat = [(f"u{i}", "", "标" * 20000) for i in range(10)]
    items2, trunc2 = ep._fit_series(fat, 0, lambda u, a: u)
    assert trunc2 is True and len(items2) < 10


def test_fit_series_cmd_budget_binds_before_count_cap():
    """条目少而每条超长时，**字符预算**先于条数上限触发（两者双重限制）。"""
    long_title = "标" * 200
    many = [(f"u{i}", "", f"第{i + 1}集 {long_title}") for i in range(400)]
    items, truncated = ep._fit_series(many, 0, lambda u, a: u)
    assert truncated is True
    assert 0 < len(items) < ep._SERIES_MAX_MRL   # 预算先触发（未被条数上限截断）
    assert sum(len(m) for _i, m in items) <= ep._SERIES_MAX_CMD
    assert [i for i, _m in items] == list(range(len(items)))  # 连续无空洞


def test_fit_series_keeps_at_least_one_oversized_item():
    """单条就超字符上限也必须保留（当前集能播 > 命令行长）。"""
    fat = [("u0", "", "标" * (ep._SERIES_MAX_CMD + 100))]
    items, trunc = ep._fit_series(fat, 0, lambda u, a: u)
    assert len(items) == 1 and trunc is False


def test_series_entry_missing_fields_falls_back():
    """残缺条目（只有 url / url+audio）→ 不抛异常，标题兜底为「第N集」。"""
    items, truncated = ep._fit_series([("u0",), ("u1", "u1a")], 0,
                                      lambda u, a: u)
    assert items == [(0, "u0#第1集"), (1, "u1#第2集")]
    assert truncated is False
    # 空元组（调用方给了垃圾数据）整条跳过，不 IndexError；下标按原位保留
    # → 集号兜底跟着真实集位走。
    assert ep._fit_series([(), ("u0",)], 0, lambda u, a: u)[0] == [
        (1, "u0#第2集")]


def test_fit_series_resolves_non_local_urls_only():
    """非本机 URL 才经 resolve 包代理；本机 /e/ URL 原样（不二次代理）。"""
    seen = []
    eps = [("http://127.0.0.1:9/e/k/0", "", "第01集"),
           ("https://cdn.example.com/b.m3u8", "", "第02集")]
    items, _ = ep._fit_series(eps, 0, lambda u, a: seen.append(u) or "P:" + u)
    assert seen == ["https://cdn.example.com/b.m3u8"]
    assert items[0][1] == "http://127.0.0.1:9/e/k/0#第01集"
    assert items[1][1] == "P:https://cdn.example.com/b.m3u8#第02集"


def test_second_call_terminates_previous(monkeypatch):
    """再次打开会先 terminate 上一次的 Popen，当前实例不受影响。"""
    procs = _install(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8",
                        episodes=[("https://cdn.example.com/hls/a.m3u8", "", "A"),
                                  ("https://cdn.example.com/hls/b.m3u8", "", "B")])
    ep.open_with_player("https://cdn.example.com/hls/b.m3u8",
                        episodes=[("https://cdn.example.com/hls/b.m3u8", "", "B")])
    assert len(procs) == 2
    assert procs[0].terminated == 1
    assert procs[1].terminated == 0


def test_terminate_race_safe(monkeypatch):
    """旧进程已自行退出 → terminate 抛异常被吞，不影响第二次拉起（幂等）。"""
    procs = _install(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    procs[0].terminate = lambda *a, **k: (_ for _ in ()).throw(OSError("gone"))
    ep.open_with_player("https://cdn.example.com/hls/b.m3u8")
    assert len(procs) == 2
    assert procs[1].args[-1] == "P:https://cdn.example.com/hls/b.m3u8"


def test_terminate_clears_control_state(monkeypatch):
    """关旧播放器时清空控制会话：新开后会按新端口/密码重建。"""
    procs = _install(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    assert ep._control_state is not None
    monkeypatch.setattr(ep, "_pick_http_port", lambda: 8091)
    monkeypatch.setattr(ep.secrets, "token_hex", lambda n: "NEWPWD")
    ep.open_with_player("https://cdn.example.com/hls/b.m3u8")
    assert procs[0].terminated == 1
    assert procs[1].args[7] == "--http-password=NEWPWD"
    assert ep._control_state["port"] == 8091
    assert ep._control_state["password"] == "NEWPWD"


def test_force_proxy_forwarded_to_proxy(monkeypatch):
    """force_proxy=True → 每次包代理都带上（主 MRL/音频轨/系列窗口当前集）。

    play_url 仍要算出来定缓冲下限（主媒体 + 主音频轨 2 次），系列列表里那条
    再解析一次（连同它自己的音频轨——系列路径丢弃 audio，见设计 §7），
    4 次调用都必须带 force_proxy。
    """
    seen = []
    procs = _install(monkeypatch, proxy=lambda u, *a, **k: (
        seen.append((u, k.get("force_proxy"))) or "P:" + u))
    ep.open_with_player(
        "https://cdn.example.com/hls/a.m3u8",
        audio="https://cdn.example.com/hls/a-a.m3u8",
        headers={"Referer": "https://fake.example/"},
        episodes=[("https://cdn.example.com/hls/a.m3u8",
                   "https://cdn.example.com/hls/a-a.m3u8", "第1集")],
        force_proxy=True,
    )
    assert [u for u, _f in seen] == [
        "https://cdn.example.com/hls/a.m3u8",
        "https://cdn.example.com/hls/a-a.m3u8",
        "https://cdn.example.com/hls/a.m3u8",
        "https://cdn.example.com/hls/a-a.m3u8",
    ]
    assert [f for _u, f in seen] == [True] * 4


def test_force_proxy_default_false(monkeypatch):
    """不传 force_proxy（默认）→ 与旧行为一致：无强制代理标志。"""
    seen = []
    procs = _install(monkeypatch, proxy=lambda u, *a, **k: (
        seen.append(k.get("force_proxy")) or "P:" + u))
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8",
                        headers={"Referer": "https://fake.example/"})
    assert seen == [False]
