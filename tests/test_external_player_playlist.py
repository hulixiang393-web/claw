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
import logging

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
    # 租约同样要复位：模块级状态会跨用例串味（上个用例漏释放的 lease 会被
    # 本用例开头的 _terminate_previous 释放掉，断言 released 就对不上了）。
    monkeypatch.setattr(ep, "_lease_id", "", raising=False)
    # 默认不碰真的 MediaProxy：不装代理的单测没必要实例化单例（真租约还会在
    # 单例里留下永久条目，把空闲看门狗焊死，波及同进程的 media_proxy 用例）。
    # 租约断言的用例各自换自己的记录器。
    monkeypatch.setattr(ep, "_acquire_proxy_lease", lambda: "L", raising=False)
    monkeypatch.setattr(ep, "_release_proxy_lease", lambda lid: None,
                        raising=False)
    # 默认不真的起看守线程：_FakeProc.wait() 立刻返回 0，真线程会在断言中途
    # 释放租约并清空 _lease_id（竞态）。看守逻辑由 test_watch_proc_* 单独覆盖。
    monkeypatch.setattr(ep, "_watch_proc", lambda proc, lid: None,
                        raising=False)
    # raising=False：_start_playlist_sync 在 Task 5 之前不存在，默认
    # raising=True 会 AttributeError 让本任务全部测试报错。
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


def test_handshake_failure_does_not_fall_back_to_browser(monkeypatch):
    """握手抛异常**不得**把已成功的拉起降级成浏览器兜底。

    Popen 已成功、VLC 已在播。异常若冒进启动 try 的 except，会额外弹一个浏览器
    窗口、把返回串换成「已在浏览器中打开」，还把真实报错吃掉——用户看到的现象
    与真实故障完全对不上号。
    """
    procs = _install(monkeypatch)
    opened = []
    monkeypatch.setattr(ep.webbrowser, "open", lambda u: opened.append(u))
    monkeypatch.setattr(
        ep, "_start_playlist_sync",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        raising=False)
    eps = [(f"https://cdn.example.com/hls/{c}.m3u8", "", f"第{i + 1}集")
           for i, c in enumerate("abc")]
    msg = ep.open_with_player(eps[2][0], episodes=eps)
    assert opened == []
    assert msg == "已用外部播放器打开"
    assert len(procs) == 1          # 播放器确实拉起了，不是走了浏览器


def test_explicit_start_idx_beats_url_match(monkeypatch):
    """显式 start_idx 优先于 url 命中项（省掉载荷性的 URL 字符串比对）。"""
    procs = _install(monkeypatch)
    seen = {}
    monkeypatch.setattr(ep, "_start_playlist_sync",
                        lambda *a, **k: seen.update(args=a), raising=False)
    eps = [(f"https://cdn.example.com/hls/{c}.m3u8", "", f"第{i + 1}集")
           for i, c in enumerate("abc")]
    # url 指向第 1 集，调用方却明确说从第 3 集开播 → 以 start_idx 为准
    ep.open_with_player(eps[0][0], episodes=eps, start_idx=2)
    args = procs[0].args
    assert "--no-playlist-autostart" in args      # 非首项 → 握手定位
    assert [a for a in args if a.startswith("P:")] == [
        "P:https://cdn.example.com/hls/a.m3u8#第1集",
        "P:https://cdn.example.com/hls/b.m3u8#第2集",
        "P:https://cdn.example.com/hls/c.m3u8#第3集",
    ]                                              # 全集仍在列
    assert seen["args"][0] == 2                   # 握手拿到的也是第 3 集


def test_start_idx_out_of_range_warns_and_falls_back(monkeypatch, caplog):
    """start_idx 越界 → 记 warning 并退回 url 命中项，不抛异常。

    退回 url 命中（第 2 集）而不是硬夹到首/末集：url 命中是**已验证**过的位置。
    必须钉住**具体下标**（握手拿到的 1）：只断言 flag 存在的话，夹到 len-1
    （第 3 集）同样有 flag，测不出「退到了错的那一集」。
    """
    procs = _install(monkeypatch)
    seen = {}
    monkeypatch.setattr(ep, "_start_playlist_sync",
                        lambda *a, **k: seen.update(args=a), raising=False)
    eps = [(f"https://cdn.example.com/hls/{c}.m3u8", "", f"第{i + 1}集")
           for i, c in enumerate("abc")]
    with caplog.at_level(logging.WARNING):
        ep.open_with_player(eps[1][0], episodes=eps, start_idx=99)
    args = procs[0].args
    assert "--no-playlist-autostart" in args      # 退回第 2 集 → 仍需握手定位
    assert any("越界" in r.getMessage() for r in caplog.records)
    assert seen["args"][0] == 1                  # url 命中的第 2 集，不是 len-1


def test_unlocatable_start_warns_and_plays_episode1(monkeypatch, caplog):
    """既无 start_idx、url 也命中不到 → 记 warning，退回第 1 集 autostart。

    静默退回第 1 集正是本任务要消灭的故障（从第 N 集开播却播第 1 集），
    行为不变但必须留痕。
    """
    procs = _install(monkeypatch)
    eps = [(f"https://cdn.example.com/hls/{c}.m3u8", "", f"第{i + 1}集")
           for i, c in enumerate("abc")]
    with caplog.at_level(logging.WARNING):
        ep.open_with_player("https://cdn.example.com/hls/zzz.m3u8",
                            episodes=eps)
    args = procs[0].args
    assert "--no-playlist-autostart" not in args  # 退回第 1 集 → 原生 autostart
    assert len([a for a in args if a.startswith("P:")]) == 3   # 全集仍入列
    assert any("未能定位起始集" in r.getMessage() for r in caplog.records)


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


def test_series_all_urls_empty_falls_back_to_single(monkeypatch):
    """episodes 非空但每集都缺流 → 退回单集路径。

    不能拿 0 条 MRL 拉起 VLC：那会开一个空播放器并回报「已用外部播放器打开」。
    """
    procs = _install(monkeypatch)
    eps = [("", "", "第01集"), ("", "", "第02集")]
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8", episodes=eps)
    args = procs[0].args
    assert args[8] == "P:https://cdn.example.com/hls/a.m3u8"
    assert len(args) == 9
    assert "--no-random" not in args


def test_title_sanitized():
    assert ep._sanitize_title("第01集 标题#带井号", 0) == "第01集 标题 带井号"
    assert ep._sanitize_title("第01集\n换行\t制表", 0) == "第01集 换行 制表"
    assert ep._sanitize_title("", 4) == "第5集"          # 空 → 集号兜底
    assert ep._sanitize_title("  ", 0) == "第1集"
    assert ep._sanitize_title("123 数字开头", 0).startswith("集")
    assert not ep._sanitize_title("9abc", 0)[0].isdigit()


def test_mrl_strips_fragment_from_url():
    """URL 里的字面 # 必须砍掉。

    不砍则 MRL 在第一个 # 处被截断，余下片段被 VLC 当成 title/chapter 解析，
    实际指向的资源随之丢失（VLC 会去播那个片段）。
    """
    assert ep._mrl_with_title("http://x/a#frag", "第1集", 0) == "http://x/a#第1集"
    assert ep._mrl_with_title("http://x/a#b#c", "第1集", 0) == "http://x/a#第1集"


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
    items, truncated = ep._fit_series(eps, 4, lambda u: u)
    assert truncated is False
    assert [i for i, _ in items] == list(range(10))   # 全集，不是 4..9
    assert items[0][0] == 0 and len(items) == 10


def test_fit_series_degrades_to_forward_window_on_overflow():
    """装不下 → 降级为「当前集往后」的窗口；超长剧集选很靠后的集也能播。"""
    long_title = "标题很长很长很长很长很长很长很长很长很长很长很长"
    many = [(f"u{i}", "", f"第{i + 1}集 {long_title}") for i in range(400)]
    items, trunc = ep._fit_series(many, 0, lambda u: u)
    assert trunc is True
    assert len(items) < 400 and items[0][0] == 0
    assert len(items) <= ep._SERIES_MAX_MRL
    # 当前集在已解析范围之外 → 必须以当前集为首重新取窗口，且不丢当前集
    items2, trunc2 = ep._fit_series(many, 390, lambda u: u)
    assert trunc2 is True
    assert items2[0][0] == 390
    assert all(i >= 390 for i, _ in items2)


def test_fit_series_budget_caps_respected():
    """两个上限都真实生效：条数上限与总字符上限各自能触发截断。"""
    # 条数上限
    many = [(f"u{i}", "", f"第{i + 1}集") for i in range(ep._SERIES_MAX_MRL + 50)]
    items, trunc = ep._fit_series(many, 0, lambda u: u)
    assert trunc is True and len(items) == ep._SERIES_MAX_MRL
    # 字符上限：条数很少但每条超长
    fat = [(f"u{i}", "", "标" * 20000) for i in range(10)]
    items2, trunc2 = ep._fit_series(fat, 0, lambda u: u)
    assert trunc2 is True and len(items2) < 10


def test_fit_series_cmd_budget_binds_before_count_cap():
    """条目少而每条超长时，**字符预算**先于条数上限触发（两者双重限制）。"""
    long_title = "标" * 200
    many = [(f"u{i}", "", f"第{i + 1}集 {long_title}") for i in range(400)]
    items, truncated = ep._fit_series(many, 0, lambda u: u)
    assert truncated is True
    assert 0 < len(items) < ep._SERIES_MAX_MRL   # 预算先触发（未被条数上限截断）
    assert sum(len(m) for _i, m in items) <= ep._SERIES_MAX_CMD
    assert [i for i, _m in items] == list(range(len(items)))  # 连续无空洞


def test_fit_series_keeps_at_least_one_oversized_item():
    """单条就超字符上限也必须保留（当前集能播 > 命令行长）。"""
    fat = [("u0", "", "标" * (ep._SERIES_MAX_CMD + 100))]
    items, trunc = ep._fit_series(fat, 0, lambda u: u)
    assert len(items) == 1 and trunc is False


def test_series_entry_missing_fields_falls_back():
    """残缺条目（只有 url / url+audio）→ 不抛异常，标题兜底为「第N集」。"""
    items, truncated = ep._fit_series([("u0",), ("u1", "u1a")], 0,
                                      lambda u: u)
    assert items == [(0, "u0#第1集"), (1, "u1#第2集")]
    assert truncated is False
    # 空元组（调用方给了垃圾数据）整条跳过，不 IndexError；下标按原位保留
    # → 集号兜底跟着真实集位走。
    assert ep._fit_series([(), ("u0",)], 0, lambda u: u)[0] == [
        (1, "u0#第2集")]


def test_fit_series_rederive_keeps_original_numbering():
    """降级重取窗口时，集号兜底按**原始集位**编号，不按切片位置。

    切片重取会把「第 396 集」重新编号成「第 1 集」——列表里出现重名集号，
    用户根本看不出自己在第几集。
    """
    many = [(f"u{i}", "", "") for i in range(400)]   # 标题全空 → 走集号兜底
    items, trunc = ep._fit_series(many, 395, lambda u: u)
    assert trunc is True
    assert [i for i, _m in items] == list(range(395, 400))
    assert items[0][1] == "u395#第396集"            # 不是切片里的「第1集」
    assert items[-1][1] == "u399#第400集"


def test_fit_series_resolves_non_local_urls_only():
    """非本机 URL 才经 resolve 包代理；本机 /e/ URL 原样（不二次代理）。"""
    seen = []
    eps = [("http://127.0.0.1:9/e/k/0", "", "第01集"),
           ("https://cdn.example.com/b.m3u8", "", "第02集")]
    items, _ = ep._fit_series(eps, 0, lambda u: seen.append(u) or "P:" + u)
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
    """force_proxy=True → 每次包代理都带上（主 MRL/音频轨/系列列表那集）。

    play_url 仍要算出来定缓冲下限（主媒体 + 主音频轨 2 次），系列列表里那条
    再解析一次 —— 但**不再顺带解析它自己的音频轨**：系列路径丢弃 audio
    （见设计 §7，merged 流另挂 input-slave 会黑屏），多解析一次就白铸一个
    没人用的代理 token。故共 3 次调用，音频轨 URL 只出现 1 次。
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
    ]
    assert [f for _u, f in seen] == [True] * 3


def test_force_proxy_default_false(monkeypatch):
    """不传 force_proxy（默认）→ 与旧行为一致：无强制代理标志。"""
    seen = []
    procs = _install(monkeypatch, proxy=lambda u, *a, **k: (
        seen.append(k.get("force_proxy")) or "P:" + u))
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8",
                        headers={"Referer": "https://fake.example/"})
    assert seen == [False]


# ---------------------------------------------------------------------- #
# 启动握手：playlist.json → {集下标: vlc_id} + 非首集 pl_play 定位
# ---------------------------------------------------------------------- #
def test_item_matches_exact_and_fragment():
    """mrl 传的是**已剥离标题片段**的 MRL；VLC 报告的 uri 带不带片段都算命中。

    反向（mrl 带片段、uri 不带）不算命中：调用方（_handshake_worker）传进来
    的永远是剥好的 base，两种都认只会让「恰好同前缀」的别的集被误命中。
    """
    assert ep._item_matches({"uri": "http://x/e/k/3#第4集"}, "http://x/e/k/3")
    assert ep._item_matches({"uri": "http://x/e/k/3"}, "http://x/e/k/3")
    assert not ep._item_matches({"uri": "http://x/e/k/4"}, "http://x/e/k/3")
    assert not ep._item_matches({}, "http://x/e/k/3")
    assert not ep._item_matches({"uri": ""}, "http://x/e/k/3")
    # 片段前缀必须带 "#" 分界：否则 k/3 会把 k/30 一起命中（错一集）
    assert not ep._item_matches({"uri": "http://x/e/k/30#第31集"},
                                "http://x/e/k/3")


def test_handshake_goto_current_episode(monkeypatch):
    """非首集开播：握手轮询到起始项后发 pl_play&id=<该集 id>。

    **映射必须按 uri 匹配得出**（不得按下标推算）：全集 1..N 入列时起始项在
    列内中段，`mapping[start_idx + off]` 那种写法会把每个 id 整体错位一位。
    这里给的是「第 2 集在列首项之后一位」这一最小反例。
    """
    got = []
    calls = {"n": 0}

    def _items(timeout=2.0, refresh=False):
        calls["n"] += 1
        if calls["n"] < 2:      # 第一次 VLC 还没建好列表
            return []
        return [{"id": 11, "uri": "http://x/e/k/0"},
                {"id": 12, "uri": "http://x/e/k/1"}]

    monkeypatch.setattr(ep, "player_playlist_items", _items)
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    ready = {}
    series = [(0, "http://x/e/k/0#第1集"), (1, "http://x/e/k/1#第2集")]
    ep._handshake_worker(1, series, lambda m: ready.update(m))
    assert got == [12]              # 定位到第 2 集（不是列表首项的 11）
    assert ready == {0: 11, 1: 12}  # 下标 0/1，不是 1/2


def test_handshake_first_episode_no_goto(monkeypatch):
    """起始项就是第 0 集 → 只建映射，不发 pl_play（VLC 原生 autostart 已对）。"""
    monkeypatch.setattr(ep, "player_playlist_items",
                        lambda timeout=2.0, refresh=False: [
                            {"id": 21, "uri": "http://x/e/k/0"}])
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    ready = {}
    ep._handshake_worker(0, [(0, "http://x/e/k/0#第1集")],
                        lambda m: ready.update(m))
    assert got == []
    assert ready == {0: 21}


def _serve_playlist(monkeypatch, payload):
    """让真的 player_playlist_items 读到 payload（只 stub HTTP 与控制态）。

    握手的 id 归一化发生在 player_playlist_items 里，所以「字符串 id 能用」
    这类断言必须走真函数——直接 stub 掉它会把归一化那段代码从测试里摘出去。
    """
    class _R:
        status_code = 200

        def json(self):
            return payload

    monkeypatch.setattr(ep, "_control_state",
                        {"host": "127.0.0.1", "port": 8090, "password": "pw"})
    monkeypatch.setattr(ep, "_playlist_items", [])
    monkeypatch.setattr(ep.requests, "get",
                        lambda *a, **k: _R())


def test_handshake_coerces_string_item_ids(monkeypatch):
    """真 VLC 的 id 是**字符串**（httprequests.lua: result.id=tostring(item.id)）。

    握手必须把 "12" 当成 12 用。若按「非 int 就跳过」处理，映射恒空、pl_play
    永不发出——而非首集开播恰恰带着 --no-playlist-autostart，结果是 VLC 窗口
    黑着什么都不播。
    """
    monkeypatch.setattr(ep, "_HANDSHAKE_TIMEOUT", 0.05)
    monkeypatch.setattr(ep, "_HANDSHAKE_INTERVAL", 0.01)
    _serve_playlist(monkeypatch, [
        {"id": "11", "uri": "http://x/e/k/0"},
        {"id": "12", "uri": "http://x/e/k/1"}])
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    ready = {}
    ep._handshake_worker(1, [(0, "http://x/e/k/0"), (1, "http://x/e/k/1")],
                        lambda m: ready.update(m))
    assert ready == {0: 11, 1: 12}   # 字符串 id 归一化成 int
    assert got == [12]                # 传给 pl_play 的也是 int，不是 "12"


def test_handshake_skips_unusable_item_ids(monkeypatch):
    """转不成 int 的 id（None / 非数字串）既不进映射、也不拿去 pl_play。

    这类 id 发给 pl_play&id= 会静默失败；更糟的是映射里留下假 id 后 Task 7 会
    认为该集「可切」而不回落重开 VLC。注意是**转不成**才跳过，不是「非 int 就
    跳过」——真 VLC 发的就是字符串。
    """
    monkeypatch.setattr(ep, "_HANDSHAKE_TIMEOUT", 0.05)
    monkeypatch.setattr(ep, "_HANDSHAKE_INTERVAL", 0.01)
    _serve_playlist(monkeypatch, [
        {"id": "abc", "uri": "http://x/e/k/0"},
        {"id": None, "uri": "http://x/e/k/1"}])
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    ready = {}
    ep._handshake_worker(1, [(0, "http://x/e/k/0"), (1, "http://x/e/k/1")],
                        lambda m: ready.update(m))
    assert ready == {}      # 映射里没有假 id
    assert got == []        # 也没拿假 id 去 pl_play


def test_handshake_timeout_does_not_raise(monkeypatch):
    """VLC 一直没就绪 → 超时降级（不抛、不阻塞调用方）。"""
    monkeypatch.setattr(ep, "_HANDSHAKE_TIMEOUT", 0.05)
    monkeypatch.setattr(ep, "_HANDSHAKE_INTERVAL", 0.01)
    polls = {"n": 0}

    def _items(timeout=2.0, refresh=False):
        polls["n"] += 1
        return []

    monkeypatch.setattr(ep, "player_playlist_items", _items)
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    ep._handshake_worker(1, [(1, "http://x/e/k/1")], lambda m: got.append(m))
    # 真的轮询到超时才收手：一次就放弃的话映射/定位都失去了「等 VLC 就绪」的意义
    assert polls["n"] >= 2
    assert got == []       # 空映射不回调、不发 pl_play


def test_handshake_timeout_logs_warning(monkeypatch, caplog):
    """握手超时要留日志。

    静默超时是唯一「屏幕黑着什么都不播」的故障，没日志就只能靠猜；design doc
    的握手条款也明确要求超时/获取失败记日志。
    """
    monkeypatch.setattr(ep, "_HANDSHAKE_TIMEOUT", 0.05)
    monkeypatch.setattr(ep, "_HANDSHAKE_INTERVAL", 0.01)
    # 有列表项、但一个都没匹配上——比「列表压根是空的」更值得怀疑
    _serve_playlist(monkeypatch, [
        {"id": "7", "uri": "http://x/other/k/9"},
        {"id": "8", "uri": "http://x/other/k/10"},
        {"id": "9", "uri": "http://x/other/k/11"}])
    monkeypatch.setattr(ep, "player_goto", lambda i: True)
    with caplog.at_level(logging.WARNING, logger="framework.external_player"):
        ep._handshake_worker(1, [(1, "http://x/e/k/1")], None)
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warns) == 1
    text = warns[0].getMessage()
    assert "握手超时" in text    # 说清是握手超时，不是别的告警
    assert "3 项" in text        # 读到几项：3
    assert "0 项" in text        # 匹配上几项：0（现场据此分辨是「没列表」还是「对不上」）
    assert "--no-playlist-autostart" in text   # 点明「黑窗」这个真实后果


def test_handshake_timeout_logs_matched_count(monkeypatch, caplog):
    """超时时「匹配上几项」要报真实数字，不能写死 0。

    超时也可能发生在**匹配到了一部分、只是没匹配到起始集**时（显式 start_idx
    指向的集被 _fit_series 跳过）。这时报 0 项会把排查引向错误方向。
    """
    monkeypatch.setattr(ep, "_HANDSHAKE_TIMEOUT", 0.05)
    monkeypatch.setattr(ep, "_HANDSHAKE_INTERVAL", 0.01)
    # 下标 0 能匹配上，但 start_idx=5 不在 series 里 → 匹配 1 项、定位不到
    _serve_playlist(monkeypatch, [
        {"id": "3", "uri": "http://x/e/k/0"},
        {"id": "4", "uri": "http://x/other/k/9"}])
    monkeypatch.setattr(ep, "player_goto", lambda i: True)
    with caplog.at_level(logging.WARNING, logger="framework.external_player"):
        ep._handshake_worker(5, [(0, "http://x/e/k/0")], None)
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warns) == 1
    text = warns[0].getMessage()
    assert "1 项" in text     # 真实匹配数是 1
    assert "0 项" not in text  # 不能写死 0


def test_handshake_duplicate_mrl_first_index_wins(monkeypatch):
    """series 里同一条 MRL 入列两次 → 每个 VLC 项认领一个下标，先到先得。

    两条相同的 MRL 会在 VLC 里变成两项。内层循环原本的 break 只保证「单个
    VLC 项不认领两个下标」，但**后一个 VLC 项仍会覆盖前一个认领的下标**
    （start_id 也跟着变成最后那个）。加 claimed 守卫后是 5→下标0、6→下标1，
    两集各自可切；否则下标0 的 id 会被静默改写成 6。
    """
    monkeypatch.setattr(ep, "_HANDSHAKE_TIMEOUT", 0.05)
    monkeypatch.setattr(ep, "_HANDSHAKE_INTERVAL", 0.01)
    _serve_playlist(monkeypatch, [
        {"id": 5, "uri": "http://x/e/k/1"},
        {"id": 6, "uri": "http://x/e/k/1"}])
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    ready = {}
    ep._handshake_worker(0, [(0, "http://x/e/k/1"), (1, "http://x/e/k/1")],
                        lambda m: ready.update(m))
    assert ready == {0: 5, 1: 6}   # 两项各认领一个下标，没有 {0: 6} 的覆盖
    assert got == []               # start_idx=0 是首集：只建映射不发 pl_play


def test_start_playlist_sync_runs_in_background(monkeypatch):
    """_start_playlist_sync 不阻塞调用方（握手在线程里跑）。

    参数必须是 (start_idx, **整列** series, 回调)：传「窗口首项的 MRL」会把
    第 1 集当成起始项，pl_play 定位整体错位。
    """
    import threading as _th
    import time as _t
    seen = {}

    def _fake(start_idx, series, cb):
        seen["thread"] = _th.current_thread() is not _th.main_thread()
        seen["args"] = (start_idx, series, cb)

    monkeypatch.setattr(ep, "_handshake_worker", _fake)
    series = [(0, "m0"), (1, "m1"), (2, "m2")]

    def _cb(mapping):
        return None

    ep._start_playlist_sync(2, series, _cb)
    for _ in range(50):
        if seen:
            break
        _t.sleep(0.02)
    assert seen.get("thread") is True
    assert seen.get("args") == (2, series, _cb)


# ---------------------------------------------------------------------- #
# 代理租约：VLC 存活期间禁止空闲看门狗回收；每条退出路径都必须释放
# ---------------------------------------------------------------------- #
def test_open_acquires_lease_and_watches_proc(monkeypatch):
    """拉起成功后：登记代理租约 + 起等待线程；_terminate_previous 释放租约。

    必须把 _watch_proc 换掉再断言：_FakeProc.wait() 立刻返回 0，真线程会在
    断言中途释放租约并把 _lease_id 清空 → 变成竞态测试。
    """
    procs = _install(monkeypatch)
    lease = {"n": 0, "released": []}
    watched = []

    def _acq():
        lease["n"] += 1
        return f"L{lease['n']}"

    monkeypatch.setattr(ep, "_acquire_proxy_lease", _acq)
    monkeypatch.setattr(ep, "_release_proxy_lease",
                        lambda lid: lease["released"].append(lid))
    monkeypatch.setattr(ep, "_watch_proc",
                        lambda proc, lid: watched.append((proc, lid)))
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    assert lease["n"] == 1
    assert ep._lease_id == "L1"
    # 看守线程必须盯着**这次拉起的那个进程**（盯错进程 = 提前释放租约）
    assert watched == [(procs[0], "L1")]
    ep._terminate_previous()
    assert lease["released"] == ["L1"]
    assert ep._lease_id == ""


def test_open_releases_lease_when_popen_fails(monkeypatch):
    """拉起失败降级浏览器 → 没有进程存活、没人替我们释放，当场释放。

    这是「取租约早于 Popen」带来的第四条退出路径：漏掉它就是一次永久泄漏
    → 空闲看门狗在整个 App 会话内失效。
    """
    _install(monkeypatch)
    released = []
    watched = []
    monkeypatch.setattr(ep, "_acquire_proxy_lease", lambda: "LF")
    monkeypatch.setattr(ep, "_release_proxy_lease",
                        lambda lid: released.append(lid))
    monkeypatch.setattr(ep, "_watch_proc",
                        lambda proc, lid: watched.append(lid))
    opened = []
    monkeypatch.setattr(ep.webbrowser, "open", lambda u: opened.append(u))
    monkeypatch.setattr(ep.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError("popen failed")))
    msg = ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    assert msg == "已在浏览器中打开"
    assert opened == ["https://cdn.example.com/hls/a.m3u8"]
    assert watched == []          # 没有进程 → 不起看守线程
    assert released == ["LF"]     # 但租约必须已释放
    assert ep._lease_id == ""


def test_lease_taken_before_any_proxy_url(monkeypatch):
    """租约必须在**第一个代理 URL 被铸造之前**取。

    顺序反了（先铸 token 再取租约）就有几毫秒窗口：空闲看门狗恰好在窗口内
    判空闲 → stop() → _tokens.clear() → 整列 MRL 全部 404。
    """
    _install(monkeypatch, proxy=lambda u, *a, **k: "P:" + u)
    log = []

    def _acq():
        log.append(("lease", True))
        return "L1"

    monkeypatch.setattr(ep, "_acquire_proxy_lease", _acq)
    monkeypatch.setattr(ep, "_watch_proc", lambda proc, lid: None)

    def _proxy(u, *a, **k):
        log.append(("url", u))
        return "P:" + u

    monkeypatch.setattr(ep, "proxy_url_for", _proxy)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8",
                        episodes=[("https://cdn.example.com/hls/a.m3u8", "", "A")])
    kinds = [k for k, _v in log]
    assert "lease" in kinds and "url" in kinds
    assert kinds.index("lease") < kinds.index("url")


def test_watch_proc_releases_lease_on_exit(monkeypatch):
    """VLC 自行退出（用户关窗口）→ 等待线程立即释放租约。"""
    released = []
    monkeypatch.setattr(ep, "_release_proxy_lease",
                        lambda lid: released.append(lid))
    monkeypatch.setattr(ep, "_lease_id", "LX")

    class _P:
        def wait(self, timeout=None):
            return 0

    ep._watch_proc(_P(), "LX")
    assert released == ["LX"]
    assert ep._lease_id == ""


def test_watch_proc_releases_even_if_wait_raises(monkeypatch):
    """句柄失效（wait 抛异常）也必须释放租约，否则永久泄漏。

    释放放在 wait 的 finally 语意外面：wait 一抛就跳过释放 = 看门狗整个
    App 会话内失效。
    """
    released = []
    monkeypatch.setattr(ep, "_release_proxy_lease",
                        lambda lid: released.append(lid))
    monkeypatch.setattr(ep, "_lease_id", "LBAD")

    class _P:
        def wait(self, timeout=None):
            raise OSError("handle gone")

    ep._watch_proc(_P(), "LBAD")
    assert released == ["LBAD"]
    assert ep._lease_id == ""


def test_watch_proc_keeps_newer_lease_intact(monkeypatch):
    """旧进程退出晚于新实例拉起 → 不得清掉**新**租约。

    _lease_id 只记当前会话那个 id：旧看守醒来把新租约清空，等于换源后立刻
    失去看门狗保护。
    """
    released = []
    monkeypatch.setattr(ep, "_release_proxy_lease",
                        lambda lid: released.append(lid))
    monkeypatch.setattr(ep, "_lease_id", "NEW")

    class _P:
        def wait(self, timeout=None):
            return 0

    ep._watch_proc(_P(), "OLD")
    assert released == ["OLD"]   # 自己的租约照放
    assert ep._lease_id == "NEW"  # 但不能顺手清掉新会话的


def test_terminate_previous_releases_old_lease_before_new(monkeypatch):
    """验收 2：播放器重启必须先释放**旧**租约，且旧 id 不残留。

    顺序反了（旧租约被新租约覆盖后才释放）就会永久泄漏一个 lease id →
    空闲看门狗在整个 App 会话内失效。
    """
    released = []
    monkeypatch.setattr(ep, "_release_proxy_lease",
                        lambda lid: released.append(lid))

    class _P:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

    old = _P()
    monkeypatch.setattr(ep, "_last_proc", old)
    monkeypatch.setattr(ep, "_lease_id", "OLD")
    ep._terminate_previous()
    assert released == ["OLD"]
    assert ep._lease_id == ""
    assert old.terminated is True

    # 换新实例：新租约必须在旧租约已释放之后才登记
    lease = {"n": 0}
    _install(monkeypatch)
    # _install 会把 _release_proxy_lease 换成空实现，重新挂回记录器
    monkeypatch.setattr(ep, "_release_proxy_lease",
                        lambda lid: released.append(lid))

    def _acq():
        lease["n"] += 1
        return f"NEW{lease['n']}"

    monkeypatch.setattr(ep, "_acquire_proxy_lease", _acq)
    monkeypatch.setattr(ep, "_watch_proc", lambda proc, lid: None)
    ep.open_with_player("https://cdn.example.com/hls/b.m3u8")
    assert released == ["OLD"]        # 拉新实例不释放别人的租约
    assert ep._lease_id == "NEW1"
    ep._terminate_previous()
    assert released == ["OLD", "NEW1"]
    assert ep._lease_id == ""
