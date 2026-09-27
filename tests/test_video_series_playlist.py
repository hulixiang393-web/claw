# -*- coding: utf-8 -*-
"""video_view 惰性系列播放列表：装配顺序、resolver 契约、状态回填、注销。

真实组件：VideoView 本体 + PySide6 真实信号投递（后台线程 emit → 主线程槽）。
替身只留在两处**进程外**边界上——MediaProxy（真起 HTTP 线程）与
Content.fetch_video_streams（真回源），二者都不该在单测里真跑。
"""
import logging
import os
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest                                                   # noqa: E402
from PySide6.QtCore import QCoreApplication                     # noqa: E402

import framework.external_player as ep                            # noqa: E402
import framework.media_proxy as mp                                # noqa: E402


def _drain() -> None:
    """跑一遍事件循环，让跨线程 emit 的信号在主线程落地。"""
    QCoreApplication.processEvents()


def _emit_from_thread(fn, *args) -> None:
    """在后台线程调 fn（模拟握手/代理线程），等它返回。"""
    t = threading.Thread(target=fn, args=args)
    t.start()
    t.join()


def _video_detail(n):
    from framework.content import Chapter, Detail

    return Detail(source_id="demo", content_type="video",
                  url="http://example.com/v/1", title="作品", cover="",
                  chapters=[Chapter(f"第{i + 1}集 标题{i}", f"http://e/{i}",
                                    cover="") for i in range(n)])


def _view(detail, n):
    from gui.pages.reader.video_view import VideoView

    view = VideoView(object())
    view._source = None
    view._detail = detail
    view._episodes = detail.chapters
    view._current_idx = n
    return view


class _FakeProxy:
    """最小 MediaProxy 替身：记录 register/unregister 参数。"""

    def __init__(self, port=9999):
        self.port = port
        self.registered = []
        self.unregistered = []
        self._n = 0

    def register_series(self, resolver, on_play=None, count=0,
                        force_proxy=False):
        self._n += 1
        key = f"k{self._n}"
        self.registered.append({
            "key": key, "resolver": resolver, "on_play": on_play,
            "count": count, "force_proxy": force_proxy,
        })
        return key

    def series_episode_url(self, key, idx):
        return f"http://127.0.0.1:{self.port}/e/{key}/{idx}"

    def unregister_series(self, key):
        self.unregistered.append(key)


class _FakeContent:
    def __init__(self, video="https://cdn/v.m3u8"):
        self.calls = []
        self._video = video

    def fetch_video_streams(self, source, url, quality="best", merged=False):
        self.calls.append((url, quality, merged))
        return self._video, ""


class _RawSource:
    """带真实 raw 配置的源替身：ad_block / media.hls / Referer 都可配。

    「⚙外部播放器」与「当前流」类断言必须靠它才成立：源替身若只写字符串
    （view._source = "SRC"），(self._source.raw or {}) 会抛 AttributeError 被
    except 吞掉 → ad_block 恒为 {}，于是「真读了源配置」和「写死字面量 {}」
    在断言下无法区分（mutant 杀不掉）。
    """

    base_url = "http://src.example.com"

    def __init__(self, raw=None, headers=None):
        self.raw = raw or {}
        self._headers = headers or {}

    def request_headers(self):
        return dict(self._headers)


class _FakeProc:
    """模拟 external_player 的模块级 _last_proc。"""

    def __init__(self, running=True, on_terminate=None):
        self._running = running
        self._on_terminate = on_terminate

    def poll(self):
        return None if self._running else 0

    def terminate(self):
        if self._on_terminate is not None:
            self._on_terminate()


def _install_proxy(monkeypatch, proxy):
    monkeypatch.setattr(mp.MediaProxy, "instance", staticmethod(lambda: proxy))


# --------------------------------------------------------------------- #
# 装配：全集顺序 / 契约 / 降级
# --------------------------------------------------------------------- #
def test_build_series_playlist_full_order(_qapp, monkeypatch):
    detail = _video_detail(5)
    view = _view(detail, 2)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = object()
    out = view._build_series_playlist("https://cdn/cur.m3u8",
                                      "https://cdn/cur-audio.m4a",
                                      {"Referer": "r"}, {"enabled": True}, True)
    assert out is not None
    assert len(out) == 5
    assert out[0][0] == "http://127.0.0.1:9999/e/k1/0"
    assert out[4][0] == "http://127.0.0.1:9999/e/k1/4"
    # 全集严格 0..N-1 入列：url 列下标即集下标（VLC 里的播放顺序 = 集序）
    assert [u for u, _a, _t in out] == [
        f"http://127.0.0.1:9999/e/k1/{i}" for i in range(5)]
    # 系列路径不挂 input-slave：merged=True 已是合并流，双流外挂会黑屏
    # （调用方传进来的 audio 不得混进 episodes 的 audio 位）
    assert all(a == "" for _u, a, _t in out)
    # 源标题已含集号 → 不重复加前缀（否则播放列表里是「第1集 第1集 标题0」）
    assert out[0][2] == "第1集 标题0"
    assert out[2][2] == "第3集 标题2"
    assert view._series_key == "k1"
    assert view._series_urls == [u for u, _a, _t in out]
    # 注册参数：全集数 + force_proxy 透传
    assert proxy.registered[0]["count"] == 5
    assert proxy.registered[0]["force_proxy"] is True


def test_series_title_falls_back_to_episode_no(_qapp, monkeypatch):
    """源章节无标题 → 用「第NN集」兜底（1-based）。"""
    from framework.content import Chapter

    detail = _video_detail(0)
    detail.chapters = [Chapter("", "http://e/0", cover=""),
                       Chapter("   ", "http://e/1", cover="")]
    view = _view(detail, 0)
    _install_proxy(monkeypatch, _FakeProxy())
    view._source = object()
    out = view._build_series_playlist("v", "", {}, None, False)
    assert [t for _u, _a, t in out] == ["第1集", "第2集"]


def test_build_series_playlist_needs_two_episodes(_qapp, monkeypatch):
    _install_proxy(monkeypatch, _FakeProxy())
    view = _view(_video_detail(1), 0)
    view._source = object()
    assert view._build_series_playlist("v", "", {}, None, False) is None
    assert view._series_key == ""


def test_build_series_playlist_needs_source(_qapp, monkeypatch):
    """无播放源（season 页/未换源成功）→ 退回单集，不注册系列。"""
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view = _view(_video_detail(3), 0)          # _source 仍为 None
    assert view._build_series_playlist("v", "", {}, None, False) is None
    assert proxy.registered == []


def test_series_resolver_uses_merged_true(_qapp, monkeypatch):
    """resolver 复用 _FetchStreamTask 同一条调用：merged=True + 对应 ep.url。"""
    view = _view(_video_detail(3), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = "SRC"
    view._content = _FakeContent()
    view._quality = "best"
    view._build_series_playlist("v", "", {"Referer": "r"}, None, False)
    resolver = proxy.registered[0]["resolver"]
    assert resolver(1) == ("https://cdn/v.m3u8", "", {"Referer": "r"}, None)
    assert view._content.calls == [("http://e/1", "best", True)]


def test_series_resolver_raises_when_empty(_qapp, monkeypatch):
    """取流为空 → 抛异常（/e/ 回 502 且不 memo，允许重试）。"""
    view = _view(_video_detail(2), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = "SRC"
    view._content = _FakeContent(video="")
    view._build_series_playlist("v", "", {}, None, False)
    with pytest.raises(ValueError):
        proxy.registered[0]["resolver"](0)


def test_rebuild_unregisters_previous_series(_qapp, monkeypatch):
    """注册新系列前先注销旧系列（注册表不泄漏）。"""
    view = _view(_video_detail(3), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = "SRC"
    view._content = _FakeContent()
    view._build_series_playlist("v", "", {}, None, False)
    view._build_series_playlist("v", "", {}, None, False)
    assert proxy.unregistered == ["k1"]
    assert view._series_key == "k2"


# --------------------------------------------------------------------- #
# 状态回填：on_play → 主线程槽
# --------------------------------------------------------------------- #
def test_on_play_hops_from_proxy_thread_to_gui_thread(_qapp, monkeypatch):
    """on_play 在**代理线程**执行：只 emit 信号，槽必须落到主线程跑。

    钉的是线程归属本身：on_play 若绕过信号直接调槽，槽就会在代理线程执行
    （碰 Qt 对象 = 随时序崩），断言里的 ident 就会是后台线程。
    """
    view = _view(_video_detail(2), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = "SRC"
    view._content = _FakeContent()
    main_ident = threading.get_ident()
    seen = []
    view._external_now_playing.connect(
        lambda g, i, v, a: seen.append((g, i, v, a, threading.get_ident())))
    view._build_series_playlist("v", "", {}, None, False)
    _emit_from_thread(proxy.registered[0]["on_play"], 1,
                      "https://cdn/1.m3u8", "")
    _drain()
    assert seen == [(view._play_gen, 1, "https://cdn/1.m3u8", "", main_ident)]
    # 钉槽本身（不只是连着的 lambda）：它确实在主线程改了视图状态
    assert view._current_idx == 1


def test_on_external_now_playing_updates_state(_qapp):
    """槽：更新 _current_idx + 写 _stream_cache + 刷新选集菜单高亮/进度记忆。"""
    view = _view(_video_detail(4), 0)
    view._content = _FakeContent()
    emitted = []
    view.episode_changed.connect(lambda a: emitted.append(a))
    view._on_external_now_playing(view._play_gen, 2, "https://cdn/2.m3u8", "")
    assert view._current_idx == 2
    assert view._stream_cache[("http://e/2", "best")] == ("https://cdn/2.m3u8", "")
    assert view._current_play == "https://cdn/2.m3u8"
    assert view._current_audio == ""
    assert view._current_title == "第3集 标题2"
    # 真实菜单：第 3 项打勾（用户在 VLC 里点集后 App 侧高亮要跟上）
    assert [a.isChecked() for a in view.ep_menu.actions()] == [
        False, False, True, False]
    assert emitted and emitted[0][2] == "http://e/2"


def test_on_external_now_playing_ignores_bad_idx(_qapp):
    """越界下标 → 原样丢弃（不该崩，也不该把 _current_idx 带歪）。"""
    view = _view(_video_detail(2), 0)
    view._on_external_now_playing(view._play_gen, 9, "v", "")
    assert view._current_idx == 0
    assert view._stream_cache == {}


# --------------------------------------------------------------------- #
# 握手映射：重复投递 / 部分映射 / 类型契约
# --------------------------------------------------------------------- #
def test_playlist_ready_hop_records_item_ids(_qapp):
    """握手线程 emit 映射 → 主线程槽写 _vlc_item_ids（供切集不重开）。"""
    view = _view(_video_detail(3), 0)
    main_ident = threading.get_ident()
    seen = []
    view._vlc_playlist_ready.connect(
        lambda g, m: seen.append((g, m, threading.get_ident())))
    _emit_from_thread(view._vlc_playlist_ready.emit, view._play_gen,
                      {0: 5, 1: 6, 2: 7})
    _drain()
    assert seen == [(view._play_gen, {0: 5, 1: 6, 2: 7}, main_ident)]
    assert view._vlc_item_ids == {0: 5, 1: 6, 2: 7}


def test_playlist_ready_repeated_delivery_is_idempotent(_qapp):
    """握手每轮轮询都回调（3s 内可达数十次），本槽保存的必须**始终等于最后一次
    报告的映射**——不多不少。

    重复投递同一份：必须完全无副作用（幂等）。
    映射变小（VLC 重建播放列表，本轮只匹配上一部分集）或项 id 变了：必须跟到
    新值。合并式「只增不删」会把握手早已宣告作废的旧 id 永久留下——切集命令
    拿着死 id 发给 VLC，症状是「App 内点集永远没反应」。
    """
    view = _view(_video_detail(3), 0)
    for mapping in ({0: 5, 1: 6, 2: 7}, {0: 5, 1: 6, 2: 7},
                    {0: 5, 1: 6}, {0: 11, 1: 12, 2: 13}):
        view._vlc_playlist_ready.emit(view._play_gen, mapping)
        _drain()
        assert view._vlc_item_ids == mapping


def test_playlist_ready_accepts_partial_map(_qapp):
    """只匹配上一部分集时按原样收下：不臆造条目、不抛异常。"""
    view = _view(_video_detail(5), 0)
    view._vlc_playlist_ready.emit(view._play_gen, {0: 5, 1: 6})
    _drain()
    assert view._vlc_item_ids == {0: 5, 1: 6}


def test_playlist_ready_rejects_non_int_ids(_qapp, caplog):
    """非 int 的项 id → 丢弃本轮映射并告警，**不得**偷偷 int() 掉。

    id 已在 player_playlist_items 单点归一化；这里再出现非 int 说明上游
    guard 漏了。此时 int() 强转会把手真 VLC 的空映射伪装成可用映射
    （切集命令拿着假 id 发给 VLC，症状是「切集永远没反应」）。
    """
    view = _view(_video_detail(3), 0)
    view._vlc_playlist_ready.emit(view._play_gen, {0: 5})
    _drain()
    with caplog.at_level(logging.WARNING):
        view._vlc_playlist_ready.emit(view._play_gen, {1: "6"})
        _drain()
    assert view._vlc_item_ids == {0: 5}          # 坏映射整轮丢弃，好映射保留
    assert any("非 int" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------- #
# 注销：VLC 存活期间不注销 / App 退出先 terminate 再注销
# --------------------------------------------------------------------- #
def test_unregister_series_idempotent(_qapp, monkeypatch):
    view = _view(_video_detail(3), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._series_key = "k9"
    view._series_urls = ["a", "b", "c"]
    view._vlc_item_ids = {0: 1}
    view._vlc_ids_gen = 7
    view._unregister_series()
    view._unregister_series()
    assert proxy.unregistered == ["k9"]
    assert view._series_key == "" and view._series_urls == []
    assert view._vlc_item_ids == {}
    # 代数一起作废：映射没了，留着一个「对得上」的代数等于给旧 id 开后门
    assert view._vlc_ids_gen == -1


def test_stop_player_keeps_series_until_vlc_exits(_qapp, monkeypatch):
    """停播时：VLC 还在播就别注销（注销即 /e/ 404），退出后才回收。"""
    view = _view(_video_detail(3), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._series_key = "k9"
    monkeypatch.setattr(ep, "_last_proc", _FakeProc(running=True))
    view._stop_player()
    assert proxy.unregistered == []
    monkeypatch.setattr(ep, "_last_proc", _FakeProc(running=False))
    view._stop_player()
    assert proxy.unregistered == ["k9"]


def test_shutdown_video_releases_lease_then_unregisters(_qapp, monkeypatch):
    """App 退出：terminate 外部 VLC（连带释放代理租约）**再**注销系列。

    顺序反了就出现「VLC 还在请求 /e/ 但 key 已注销」；漏掉 terminate 则本次
    会话的代理租约永不释放，整个 App 生命周期内空闲看门狗失效。
    """
    view = _view(_video_detail(3), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    order = []
    monkeypatch.setattr(proxy, "unregister_series",
                        lambda key: order.append("unregister"))
    released = []
    monkeypatch.setattr(ep, "_release_proxy_lease", lambda lid: released.append(lid))
    monkeypatch.setattr(ep, "_last_proc",
                        _FakeProc(on_terminate=lambda: order.append("terminate")))
    monkeypatch.setattr(ep, "_lease_id", "LX")
    monkeypatch.setattr(ep, "_control_state", {"port": 1}, raising=False)
    monkeypatch.setattr(ep, "_playlist_items", [{"id": 1}], raising=False)
    view._series_key = "k9"
    view._series_urls = ["a", "b", "c"]
    view._external_active = True
    view.shutdown_video()
    assert released == ["LX"]                             # 硬性验收：App 退出放租约
    assert order == ["terminate", "unregister"]           # 顺序不能反
    assert view._series_key == "" and view._series_urls == []
    assert view._external_active is False


# --------------------------------------------------------------------- #
# 代数护栏：换源后陈旧回调不得落地（I1）
# --------------------------------------------------------------------- #
def test_stale_on_play_after_source_switch_is_dropped(_qapp, monkeypatch):
    """换源后旧系列残留的 on_play 必须丢弃：不得改 _current_idx / _stream_cache。

    复现链：VLC 还在播 → reload_detail 保留旧系列 → 用户在新源点集 → 不调用
    _play（无新代数）→ 旧系列被 VLC 点到时回调 on_play。若不丢弃：
    _stream_cache[(新源 ep.url, 画质)] = 旧源流地址，而 _load_episode 命中
    缓存后原样使用（无复验）→ App 拿 A 源的流去播 B 源的集，且持续到清缓存。
    """
    view = _view(_video_detail(3), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    monkeypatch.setattr(ep, "_last_proc", _FakeProc(running=True))
    monkeypatch.setattr(view, "_maybe_load_recommendations", lambda: None)
    view._source = "SRC1"
    view._content = _FakeContent()
    view._build_series_playlist("v", "", {}, None, False)
    stale_on_play = proxy.registered[0]["on_play"]

    view.reload_detail(_video_detail(2))
    assert view._series_key == "k1"        # 前提：VLC 还在播，旧系列仍注册

    stale_on_play(1, "https://cdn/OLD-S1.m3u8", "")
    _drain()
    assert view._current_idx == 0
    assert view._stream_cache == {}
    assert view._current_play == ""


def test_stale_playlist_ready_after_source_switch_is_dropped(_qapp, monkeypatch):
    """换源后旧握手线程残留的映射必须丢弃：不得写 _vlc_item_ids。"""
    view = _view(_video_detail(3), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    monkeypatch.setattr(ep, "_last_proc", _FakeProc(running=True))
    monkeypatch.setattr(view, "_maybe_load_recommendations", lambda: None)
    view._source = "SRC1"
    view._content = _FakeContent()
    seen = {}
    monkeypatch.setattr(ep, "open_with_player",
                        lambda url, **kw: seen.update(kw) or "ok")
    view._play("https://cdn/real0.m3u8", "", "第1集 标题0")
    stale_cb = seen["on_playlist_ready"]

    view.reload_detail(_video_detail(2))
    stale_cb({0: 11, 1: 12})
    _drain()
    assert view._vlc_item_ids == {}


def test_series_resolver_snapshots_source_and_episodes(_qapp, monkeypatch):
    """已注册系列按**注册时刻**的 source/episodes/quality 解析，不受换源影响。

    不快照的话：resolver 读的是当前 self._source/self._episodes，而它们已被
    换源整体替换、彼此自洽 → fetch_video_streams 成功返回**新源**的流，
    旧 VLC 于是播出新源的集（不报错，静默播错）。
    """
    from framework.content import Chapter

    view = _view(_video_detail(3), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = "SRC1"
    view._quality = "best"
    content = _FakeContent()
    view._content = content
    view._build_series_playlist("v", "", {}, None, False)
    resolver = proxy.registered[0]["resolver"]

    # 换源：视图状态整体切到 SRC2
    view._source = "SRC2"
    view._quality = "hd"
    view._episodes = [Chapter("新1", "http://new/1", cover=""),
                      Chapter("新2", "http://new/2", cover="")]

    assert resolver(1) == ("https://cdn/v.m3u8", "", {}, None)
    # 仍是 SRC1 的第 2 集 + best 画质，而非 SRC2 的第 2 集 + hd
    assert content.calls == [("http://e/1", "best", True)]


# --------------------------------------------------------------------- #
# _play 接线：惰性 URL 开播 + 真实流地址分类 + 回调走信号
# --------------------------------------------------------------------- #
def test_play_passes_series_playlist_and_real_url(_qapp, monkeypatch):
    """_play：全集入列、开播 MRL 用**当前集惰性 URL**、分类用**真实流地址**。"""
    view = _view(_video_detail(4), 2)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = object()
    view._content = _FakeContent()
    seen = {}

    def _open(url, **kw):
        seen.update(url=url, **kw)
        return "已用外部播放器打开"

    monkeypatch.setattr(ep, "open_with_player", _open)
    view._play("https://cdn/real2.m3u8", "https://cdn/real2-audio.m4a",
               "第3集 标题2")
    assert seen["url"] == "http://127.0.0.1:9999/e/k1/2"
    assert seen["start_idx"] == 2
    assert seen["classify_url"] == "https://cdn/real2.m3u8"
    assert [u for u, _a, _t in seen["episodes"]] == [
        f"http://127.0.0.1:9999/e/k1/{i}" for i in range(4)]
    assert all(a == "" for _u, a, _t in seen["episodes"])


def test_play_falls_back_to_single_url_without_series(_qapp, monkeypatch):
    """不足 2 集 → 无系列：开播 MRL/分类都用真实流地址（退回既有行为）。"""
    view = _view(_video_detail(1), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = object()
    view._content = _FakeContent()
    seen = {}

    def _open(url, **kw):
        seen.update(url=url, **kw)
        return "已用外部播放器打开"

    monkeypatch.setattr(ep, "open_with_player", _open)
    view._play("https://cdn/only.m3u8", "", "第1集 标题0")
    assert seen["url"] == "https://cdn/only.m3u8"
    assert seen["episodes"] is None
    assert seen["classify_url"] == "https://cdn/only.m3u8"
    assert proxy.registered == []


def test_play_playlist_ready_callback_hops_threads(_qapp, monkeypatch):
    """传给 open_with_player 的回调必须走信号：后台线程 emit → 主线程落地。

    直接把槽交给握手线程就等于让它在后台线程改视图状态——Qt 线程归属
    违规（症状是随机崩溃），而这里完全看不出来。
    """
    view = _view(_video_detail(3), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = object()
    view._content = _FakeContent()
    seen = {}
    monkeypatch.setattr(ep, "open_with_player",
                        lambda url, **kw: seen.update(url=url, **kw) or "ok")
    view._play("https://cdn/real0.m3u8", "", "第1集 标题0")
    _emit_from_thread(seen["on_playlist_ready"], {0: 11})
    assert view._vlc_item_ids == {}          # 还没投递：事件在队列里
    _drain()
    assert view._vlc_item_ids == {0: 11}     # 投递后在主线程落地


def test_load_new_work_drops_previous_series_callbacks(_qapp, monkeypatch):
    """换作品（load）也自增代数：上一部剧的惰性系列回调全部作废。

    load 与 reload_detail 是两条独立的换集路径（前者换作品、后者换源），
    少任何一条都会留下一个「换屏后旧剧仍在回调」的窗口。
    """
    view = _view(_video_detail(3), 0)
    monkeypatch.setattr(view, "_maybe_load_recommendations", lambda: None)
    stale_gen = view._play_gen
    view.load(object(), _video_detail(4))
    assert view._play_gen > stale_gen
    view._external_now_playing.emit(stale_gen, 1, "v", "")
    view._vlc_playlist_ready.emit(stale_gen, {0: 77})
    _drain()
    assert view._current_idx == 0
    assert view._vlc_item_ids == {}


def test_play_uses_returned_entry_as_start_url(_qapp, monkeypatch):
    """开播 MRL 取自**返回列表**当前集那一项，不回头读 _series_urls。

    两处下标必须恒等：_series_urls 对无 ep.url 的集仍是可用的代理 URL，而
    返回列表里那一项是空串。从 _series_urls 取会给 VLC 开一个点了就 502
    的死 MRL；从返回列表取则为空 → 干净地退回单集播放。
    """
    from framework.content import Chapter

    detail = _video_detail(0)
    detail.chapters = [Chapter("第1集", "http://e/0", cover=""),
                       Chapter("无URL集", "", cover=""),
                       Chapter("第3集", "http://e/2", cover="")]
    view = _view(detail, 1)                  # 当前集 = 无 URL 的第 2 集
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = object()
    view._content = _FakeContent()
    seen = {}
    monkeypatch.setattr(ep, "open_with_player",
                        lambda url, **kw: seen.update(url=url, **kw) or "ok")
    view._play("https://cdn/real1.m3u8", "", "第2集 无URL集")
    assert seen["url"] == "https://cdn/real1.m3u8"   # 不是代理死 URL
    assert seen["episodes"] is None                   # 退回单集
    # 系列仍注册着（无害：下次 _play/换集会回收），要防的死条目是它入列的那一项
    assert proxy.registered[0]["count"] == 3


def test_play_bumps_generation_and_drops_previous_session(_qapp, monkeypatch):
    """新起一次外播 = 新代数：上一会话的回调立刻作废。

    否则用户「重开播放器」时，上一轮的握手线程（最长还活 3s）回调会带着
    仍然有效的代数回来，把上一支播放列表的 id 写进 _vlc_item_ids，
    切集命令就发给了错的列表。
    """
    view = _view(_video_detail(3), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = object()
    view._content = _FakeContent()
    stale_gen = view._play_gen
    seen = {}
    monkeypatch.setattr(ep, "open_with_player",
                        lambda url, **kw: seen.update(url=url, **kw) or "ok")

    view._play("https://cdn/real0.m3u8", "", "第1集 标题0")
    assert view._play_gen > stale_gen                     # 代数确实前进了

    view._vlc_playlist_ready.emit(stale_gen, {0: 99})     # 上一会话的握手
    view._external_now_playing.emit(stale_gen, 1, "v", "")  # 上一会话的 on_play
    _drain()
    assert view._vlc_item_ids == {}                        # 两条都被丢弃
    assert view._current_idx == 0

    seen["on_playlist_ready"]({0: 11})                    # 本会话的仍要落地
    _drain()
    assert view._vlc_item_ids == {0: 11}


# --------------------------------------------------------------------- #
# 降级与入列卫生
# --------------------------------------------------------------------- #
def test_build_series_playlist_returns_none_when_proxy_unavailable(_qapp,
                                                                   monkeypatch):
    """代理起不来（instance 抛异常）→ 退回单集，且不留任何注册痕迹。"""
    def _boom():
        raise RuntimeError("代理起不来")

    monkeypatch.setattr(mp.MediaProxy, "instance", staticmethod(_boom))
    view = _view(_video_detail(3), 0)
    view._source = "SRC"
    assert view._build_series_playlist("v", "", {}, None, False) is None
    assert view._series_key == ""
    assert view._series_urls == []


def test_registered_series_is_released_when_url_building_fails(_qapp, monkeypatch):
    """建 URL 失败 → 已注册的系列必须注销，不能留成孤儿。

    注册表里的条目通过闭包持有视图的强引用，key 又没记进 _series_key 时
    本视图永远注销不掉它，只有代理侧 _SERIES_MAX=8 的 FIFO 能挤掉。
    """
    class _UrlFailProxy(_FakeProxy):
        def series_episode_url(self, key, idx):
            raise RuntimeError("建 URL 失败")

    proxy = _UrlFailProxy()
    _install_proxy(monkeypatch, proxy)
    view = _view(_video_detail(3), 0)
    view._source = "SRC"
    assert view._build_series_playlist("v", "", {}, None, False) is None
    assert proxy.registered[0]["key"] in proxy.unregistered
    assert view._series_key == "" and view._series_urls == []


def test_external_vlc_running_logs_when_symbol_unavailable(_qapp, monkeypatch,
                                                          caplog):
    """取不到 player_running（含 ImportError）→ 记 warning，不静默。

    静默按「已退出」处理会让 _stop_player 无条件注销系列，等于悄悄恢复
    「VLC 还在请求 /e/ → 404」这个故障，且现场毫无线索。
    """
    view = _view(_video_detail(2), 0)
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def _fake_import(name, *a, **kw):
        if name == "framework.external_player":
            raise ImportError("cannot import name 'player_running'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", _fake_import)
    with caplog.at_level(logging.WARNING):
        assert view._external_vlc_running() is False
    assert any("player_running" in r.getMessage() for r in caplog.records)


def test_empty_chapter_url_is_not_enqueued_in_vlc_playlist(_qapp, monkeypatch):
    """无 ep.url 的分集不占 VLC 播放列表位置（也不会留下永久 502 的死条目）。

    惰性路径下入列的是 `/e/` 代理 URL（**永不为空**），所以 external_player
    里 `if not entry[0]: continue` 那道守卫对我们不生效——空的是上游的
    ep.url。这里传空 URL 让 _fit_series 跳过该集：它按**入列位置**记 idx，
    跳过不挪位，故其余分集的集下标仍与 App 侧严格对齐（Task 7 依赖此对齐）。
    """
    from framework.content import Chapter

    detail = _video_detail(0)
    detail.chapters = [Chapter("第1集", "http://e/0", cover=""),
                       Chapter("无URL集", "", cover=""),
                       Chapter("第3集", "http://e/2", cover="")]
    view = _view(detail, 0)
    _install_proxy(monkeypatch, _FakeProxy())
    view._source = "SRC"
    out = view._build_series_playlist("v", "", {}, None, False)
    assert out[1][0] == ""          # 空 URL 入列 → 由 _fit_series 跳过

    def _resolve(url):
        raise AssertionError(f"代理 URL 不该再解析：{url}")

    items, truncated = ep._fit_series(out, 0, _resolve)
    assert [i for i, _m in items] == [0, 2]   # 死条目不入列，且下标不挪位
    assert truncated is False


# --------------------------------------------------------------------- #
# App 内切集：外播会话用 vlc_id 指挥 VLC（不重开进程、进度不丢）
# --------------------------------------------------------------------- #
def test_select_episode_uses_vlc_goto_when_mapped(_qapp, monkeypatch):
    """有映射 → 发 pl_play，不重开播放器（进度不丢、窗口不闪）。"""
    view = _view(_video_detail(5), 0)
    view._external_active = True
    view._vlc_item_ids = {0: 10, 1: 11, 2: 12}
    view._vlc_ids_gen = view._play_gen          # C5
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    opened = []
    monkeypatch.setattr(ep, "open_with_player",
                        lambda *a, **k: opened.append(a) or "已用外部播放器打开")
    view._load_episode = lambda idx: opened.append(("load", idx))

    view._select_episode(2)
    assert got == [12]
    assert opened == []              # 没有重开
    assert view._current_idx == 2    # 乐观更新
    # 真实选集菜单：乐观更新后第 3 项打勾（UI 高亮必须跟上，否则菜单指向旧集）
    assert [a.isChecked() for a in view.ep_menu.actions()] == [
        False, False, True, False, False]


def test_select_episode_rejects_mapping_from_stale_generation(_qapp, monkeypatch):
    """C5：映射属于旧会话（切源后残留）→ 不得用它指挥 VLC，回落重开。

    _stop_player 在 VLC 仍活着时**故意**保留旧注册与旧映射，故映射可能来自
    上一支播放列表；拿它发 pl_play 会让 VLC 跳到上一个源的集。

    钉「没有发过 pl_play」用**记录**而不是「一调用就抛」：_try_external_goto
    兜底的 `except Exception` 会把 AssertionError 一起吞掉再回落重开，那样
    无论是否误发这条测试都通过（= 没有证据）。
    """
    view = _view(_video_detail(5), 0)
    view._external_active = True
    view._vlc_item_ids = {0: 10, 1: 11}
    view._vlc_ids_gen = view._play_gen - 1     # 陈旧：上一个会话
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    loaded = []
    view._load_episode = lambda idx: loaded.append(idx)
    view._refresh_ep_menu = lambda: None
    view._select_episode(1)
    assert got == []                # 陈旧映射不得用来指挥 VLC
    assert loaded == [1]            # 回落重开路径
    assert view._current_idx == 0   # 也不能乐观更新（VLC 根本没被指挥）


def test_playlist_ready_slot_records_generation_of_mapping(_qapp):
    """C5：映射与它的代数必须同时落账，供 _try_external_goto 校验。"""
    view = _view(_video_detail(3), 0)
    gen = view._play_gen
    view._vlc_playlist_ready.emit(gen, {0: 7, 1: 8})
    _drain()
    assert view._vlc_item_ids == {0: 7, 1: 8}
    assert view._vlc_ids_gen == gen
    # 陈旧代数不落账（既有守卫行为，且不得覆盖已记录的代数）
    view._vlc_playlist_ready.emit(gen - 1, {0: 99})
    _drain()
    assert view._vlc_item_ids == {0: 7, 1: 8}
    assert view._vlc_ids_gen == gen


def test_select_episode_falls_back_when_goto_fails(_qapp, monkeypatch):
    """pl_play 失败 → 回落现有重开路径。"""
    view = _view(_video_detail(5), 0)
    view._external_active = True
    view._vlc_item_ids = {0: 10, 1: 11}
    view._vlc_ids_gen = view._play_gen
    monkeypatch.setattr(ep, "player_goto", lambda i: False)
    loaded = []
    view._load_episode = lambda idx: loaded.append(idx)
    view._refresh_ep_menu = lambda: None
    view._select_episode(1)
    assert loaded == [1]
    assert view._current_idx == 0    # 失败就不许乐观更新


def test_select_episode_falls_back_without_mapping(_qapp, monkeypatch):
    """无映射（握手未完成/失败）→ 回落重开。"""
    view = _view(_video_detail(5), 0)
    view._external_active = True
    view._vlc_item_ids = {}
    view._vlc_ids_gen = view._play_gen      # 会话对，但握手没交出映射
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    loaded = []
    view._load_episode = lambda idx: loaded.append(idx)
    view._refresh_ep_menu = lambda: None
    view._select_episode(3)
    assert got == []
    assert loaded == [3]


def test_select_episode_normal_when_not_external(_qapp, monkeypatch):
    """非外播会话 → 行为完全不变（内嵌/单集路径不受影响）。"""
    view = _view(_video_detail(5), 0)
    view._external_active = False
    view._vlc_item_ids = {0: 10, 1: 11}
    view._vlc_ids_gen = view._play_gen
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    loaded = []
    view._load_episode = lambda idx: loaded.append(idx)
    view._refresh_ep_menu = lambda: None
    view._select_episode(1)
    assert got == []
    assert loaded == [1]


def test_next_prev_ep_go_through_vlc(_qapp, monkeypatch):
    """上一集/下一集在映射可用时指挥 VLC，不重开（经 _select_episode 转发）。"""
    view = _view(_video_detail(5), 1)
    view._external_active = True
    view._vlc_item_ids = {0: 10, 1: 11, 2: 12}
    view._vlc_ids_gen = view._play_gen
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    view._load_episode = lambda idx: got.append(("load", idx))
    view._refresh_ep_menu = lambda: None
    view._on_next_ep()
    view._on_prev_ep()
    # 从第 2 集（idx 1）出发：下一集 → idx 2（id 12），上一集 → 回到 idx 1（id 11）
    assert got == [12, 11]


def test_handle_key_prev_uses_pl_previous(_qapp, monkeypatch):
    """回归：P 键必须发 pl_previous（VLC 没有 pl_prev，此前从未生效）。"""
    from PySide6.QtCore import Qt, QEvent
    from PySide6.QtGui import QKeyEvent

    view = _view(_video_detail(3), 0)
    view._external_active = True
    view._player = None
    sent = []
    monkeypatch.setattr(ep, "player_command",
                        lambda c, v="", item_id=None: sent.append(c) or True)
    view._handle_key(QKeyEvent(QEvent.KeyPress, Qt.Key_P, Qt.NoModifier))
    assert sent == ["pl_previous"]
    view._handle_key(QKeyEvent(QEvent.KeyPress, Qt.Key_N, Qt.NoModifier))
    assert sent == ["pl_previous", "pl_next"]


# --------------------------------------------------------------------- #
# 「⚙外部播放器」入口：接系列路径 + 新会话代数 + 握手回调适配
# --------------------------------------------------------------------- #
def _ext_view(detail, idx, caching_ms=0, ad_block=None):
    """装好「⚙外部播放器」所需状态的视图。

    源用 _RawSource 而非字符串：ad_block 真从源配置读（C4 证据必须能区分
    「读了配置」和「写死 {}」）。
    """
    view = _view(detail, idx)
    view._source = _RawSource({"ad_block": ad_block or {}})
    view._content = _FakeContent()
    view._current_play = "https://cdn/cur.m3u8"
    view._current_audio = ""
    view._current_title = f"第{idx + 1}集"
    view._force_proxy_enabled = lambda: False
    view._source_network_caching_ms = lambda: caching_ms
    return view


def test_open_external_uses_series_playlist(_qapp, monkeypatch):
    """「⚙外部播放器」入口也走系列路径（此前完全不传 episodes）。"""
    view = _ext_view(_video_detail(4), 1, caching_ms=30000,
                     ad_block={"urls": ["ad-segment"]})
    _install_proxy(monkeypatch, _FakeProxy())
    monkeypatch.setattr(ep, "_locate_vlc", lambda: r"C:\vlc.exe")
    captured = {}

    def _open(url, **kw):
        captured["url"] = url
        captured.update(kw)
        return "已用外部播放器打开"

    monkeypatch.setattr(ep, "open_with_player", _open)
    view._open_external()
    assert captured["episodes"] is not None
    assert len(captured["episodes"]) == 4
    assert captured["url"] == "http://127.0.0.1:9999/e/k1/1"   # 从第 2 集开播
    assert captured["classify_url"] == "https://cdn/cur.m3u8"   # C3：用真实流分类
    assert captured["start_idx"] == 1
    assert captured["caching_ms"] == 30000
    assert captured["ad_block"] == {"urls": ["ad-segment"]}   # C4：真从源配置传下去


def test_open_external_playlist_ready_adapts_generation(_qapp, monkeypatch):
    """C2：交出去的是**单参**适配 lambda：emit 信号 + **捕获**的代数。

    两个陷阱都在这里：
    1. 把裸槽 `self._on_vlc_playlist_ready` 交出去——握手线程按单参调用会直接
       炸；即便签名碰巧对上了，也等于绕开代数守卫（每轮陈旧握手都被应用）。
    2. 写成 `lambda m: emit(self._play_gen, m)`（emit 时才读）——恒等匹配，
       守卫静默失效。靠「会话作废后旧回调仍被丢弃」钉死。
    """
    view = _ext_view(_video_detail(4), 1)
    _install_proxy(monkeypatch, _FakeProxy())
    monkeypatch.setattr(ep, "_locate_vlc", lambda: r"C:\vlc.exe")
    seen = {}
    monkeypatch.setattr(ep, "open_with_player",
                        lambda url, **kw: seen.update(url=url, **kw) or "ok")
    view._open_external()
    ready = seen["on_playlist_ready"]
    assert ready.__code__.co_argcount == 1        # 握手线程的调用契约：单参映射
    _emit_from_thread(ready, {0: 11})
    assert view._vlc_item_ids == {}                # 还没投递：事件在队列里
    _drain()
    assert view._vlc_item_ids == {0: 11}           # 投递后在主线程落地
    assert view._vlc_ids_gen == view._play_gen

    stale = seen["on_playlist_ready"]              # 会话作废（换源/换作品）后
    view._play_gen += 1
    stale({0: 99})
    _drain()
    assert view._vlc_item_ids == {0: 11}           # 旧回调不得覆盖（代数是捕获的）
    assert view._vlc_ids_gen != view._play_gen     # 故 _try_external_goto 闸门已关


def test_open_external_bumps_generation(_qapp, monkeypatch):
    """C1：⚙外部播放器是新会话 → 必须自增代数，否则旧系列回调将被误认有效。"""
    view = _ext_view(_video_detail(4), 1)
    _install_proxy(monkeypatch, _FakeProxy())
    monkeypatch.setattr(ep, "_locate_vlc", lambda: r"C:\vlc.exe")
    captured = {}
    monkeypatch.setattr(ep, "open_with_player",
                        lambda url, **kw: captured.update(url=url, **kw) or "ok")

    before = view._play_gen
    view._open_external()
    assert view._play_gen == before + 1
    # 上一会话在途的回调此刻必须作废（这正是 C1 的场景：VLC 还开着）
    view._vlc_playlist_ready.emit(before, {0: 99})
    view._external_now_playing.emit(before, 1, "https://cdn/OLD.m3u8", "")
    _drain()
    assert view._vlc_item_ids == {}
    assert view._current_play == "https://cdn/cur.m3u8"   # 旧流没写进来
    # 本会话的回调带着**新**代数 → 落地
    captured["on_playlist_ready"]({0: 11})
    _drain()
    assert view._vlc_item_ids == {0: 11}
    assert view._vlc_ids_gen == before + 1


def test_open_external_falls_back_for_empty_url_episode(_qapp, monkeypatch):
    """C3：当前集无 ep.url（列表里该项为 ""）→ 退回单集真实地址，不得播 /e/。"""
    from framework.content import Chapter

    detail = _video_detail(0)
    detail.chapters = [Chapter("第1集", "http://e/0", cover=""),
                       Chapter("无URL集", "", cover=""),
                       Chapter("第3集", "http://e/2", cover="")]
    view = _ext_view(detail, 1)              # 当前集 = 无 URL 的第 2 集
    _install_proxy(monkeypatch, _FakeProxy())
    monkeypatch.setattr(ep, "_locate_vlc", lambda: r"C:\vlc.exe")
    captured = {}
    monkeypatch.setattr(ep, "open_with_player",
                        lambda url, **kw: captured.update(url=url, **kw) or "ok")
    view._open_external()
    assert captured["url"] == "https://cdn/cur.m3u8"     # 不是点了就 502 的 /e/
    assert captured["episodes"] is None                  # 退回单集
    assert captured["classify_url"] == "https://cdn/cur.m3u8"


# --------------------------------------------------------------------- #
# Review 轮：换源后「当前流」三元组必须作废（Important）
# --------------------------------------------------------------------- #
def _playing_old_source(monkeypatch, n=3):
    """造一个「VLC 正在播旧源第 1 集」的视图（走真实 _play，不手工赋值）。

    ep._last_proc 置 running=True → player_running() 为真 → reload_detail 里的
    _stop_player() 不注销旧系列（正是「VLC 还开着」的真实前提）。
    """
    view = _view(_video_detail(n), 0)
    view._source = _RawSource({"ad_block": {}})
    view._content = _FakeContent(video="https://cdn/OLD.m3u8")
    view._force_proxy_enabled = lambda: False
    view._source_network_caching_ms = lambda: 0
    _install_proxy(monkeypatch, _FakeProxy())
    monkeypatch.setattr(ep, "_last_proc", _FakeProc(running=True))
    monkeypatch.setattr(ep, "_locate_vlc", lambda: r"C:\vlc.exe")
    monkeypatch.setattr(ep, "open_with_player", lambda url, **kw: "已用外部播放器打开")
    view._play("https://cdn/OLD.m3u8", "https://cdn/OLD-audio.m4a", "第1集 标题0")
    return view


def test_reload_detail_invalidates_current_stream(_qapp, monkeypatch):
    """换源后 _current_play/_current_audio/_current_title 必须一并作废。

    reload_detail 开头已 _stop_player()（旧流早就不放了），却只清了
    _stream_cache / _detail_url_for_play / _has_played / _selection_mode，唯独
    漏了「当前流」三元组。留着的话，界面上摆的是 B 源的集，「当前流」却还是
    A 源的 → ⚙外部播放器、▶、复制流地址三处都会把 A 源的流当 B 源的当前集
    交出去/复制出去（静默播错，App 里完全看不出来）。

    _current_audio 必须同清：它只与 _current_play 成对读（_toggle_play_pause /
    _open_external 都传），单清其一反而会凑出「新源视频 + 旧源音轨」的组合。
    """
    view = _playing_old_source(monkeypatch)
    assert view._current_play == "https://cdn/OLD.m3u8"       # 前提：正在播旧源
    assert view._current_audio == "https://cdn/OLD-audio.m4a"
    assert view._current_title == "第1集 标题0"

    view.reload_detail(_video_detail(2))                      # 换源 → 2 集（选集态）
    assert view._current_play == ""
    assert view._current_audio == ""
    assert view._current_title == ""


def test_open_external_after_source_switch_launches_nothing(_qapp, monkeypatch):
    """换源后 ⚙外部播放器不得把**旧源**的流交给 VLC（陈旧回退不可达）。

    选集态语义（有意为之，非副作用）：⚙ 的契约是「把**当前集**交外部播放器」
    （帮助文案：手动重新拉起播放器），选集态压根没有当前集 → 无可交之物。
    选集态下要开播请用 ▶／点集卡（_toggle_play_pause 会按高亮集取流，
    顺带拿到全集播放列表），比「外播一个猜出来的集」更对。
    """
    view = _playing_old_source(monkeypatch)
    launched = []
    monkeypatch.setattr(ep, "open_with_player",
                        lambda url, **kw: launched.append(url) or "ok")

    view.reload_detail(_video_detail(2))
    view._open_external()
    assert launched == []          # 旧源流没被交出去，也没退化成播放新源


def test_toggle_play_after_source_switch_loads_highlighted_episode(_qapp, monkeypatch):
    """换源后 ▶按当前高亮集取流，不得重播旧源的流（同一个根因的第二个出口）。"""
    view = _playing_old_source(monkeypatch)
    played = []
    monkeypatch.setattr(view, "_load_episode", lambda i: played.append(i))

    view.reload_detail(_video_detail(2))
    view._current_idx = 1
    view._toggle_play_pause()
    assert played == [1]           # 走「按高亮集取流」，不是 _play(旧源流)


def test_ad_block_reaches_both_entry_points(_qapp, monkeypatch):
    """C4 证据补强：源配了非空 ad_block → _play 与 ⚙ 两个入口都必须真传下去。

    原来只有 ⚙ 一处、且源替身没有 raw，ad_block 恒为 {}，「读了源配置」与
    「写死字面量 {}」无法区分。这里两个入口都断言真值。
    """
    detail = _video_detail(4)
    view = _view(detail, 1)
    view._source = _RawSource({"ad_block": {"urls": ["ad-segment"]}})
    view._content = _FakeContent()
    view._force_proxy_enabled = lambda: False
    view._source_network_caching_ms = lambda: 0
    _install_proxy(monkeypatch, _FakeProxy())
    monkeypatch.setattr(ep, "_locate_vlc", lambda: r"C:\vlc.exe")
    captured = {}
    monkeypatch.setattr(ep, "open_with_player",
                        lambda url, **kw: captured.update(url=url, **kw) or "ok")

    view._play("https://cdn/cur.m3u8", "", "第2集")
    assert captured["ad_block"] == {"urls": ["ad-segment"]}    # _play 入口

    captured.clear()
    view._open_external()
    assert captured["ad_block"] == {"urls": ["ad-segment"]}    # ⚙ 入口


def test_try_external_goto_passes_item_id_unchanged(_qapp, monkeypatch):
    """_try_external_goto 必须把 item_id **原样**转发（不得再 int() 强转）。

    已知取舍：item_id 是 VLC item id（int），强转无害；但若 libvlc 某天给出
    非数字 id，这一行就是整条链路上唯一的 TypeError 源。on_playlist_ready 已
    校验过 id 必须是 int，故这里强转是纯冗余 → 删。
    """
    view = _view(_video_detail(3), 0)
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    view._play_gen = 7
    view._vlc_ids_gen = 7
    view._vlc_item_ids = {0: 41}
    view._current_idx = 0
    view._external_active = True

    assert view._try_external_goto(0) is True
    assert got == [41]              # item_id 原样转发

    got.clear()
    assert view._try_external_goto(2) is False    # 未映射 → 不发命令
    assert got == []
