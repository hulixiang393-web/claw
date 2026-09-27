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
        lambda i, v, a: seen.append((i, v, a, threading.get_ident())))
    view._build_series_playlist("v", "", {}, None, False)
    _emit_from_thread(proxy.registered[0]["on_play"], 1,
                      "https://cdn/1.m3u8", "")
    _drain()
    assert seen == [(1, "https://cdn/1.m3u8", "", main_ident)]


def test_on_external_now_playing_updates_state(_qapp):
    """槽：更新 _current_idx + 写 _stream_cache + 刷新选集菜单高亮/进度记忆。"""
    view = _view(_video_detail(4), 0)
    view._content = _FakeContent()
    emitted = []
    view.episode_changed.connect(lambda a: emitted.append(a))
    view._on_external_now_playing(2, "https://cdn/2.m3u8", "")
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
    view._on_external_now_playing(9, "v", "")
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
        lambda m: seen.append((m, threading.get_ident())))
    _emit_from_thread(view._vlc_playlist_ready.emit, {0: 5, 1: 6, 2: 7})
    _drain()
    assert seen == [({0: 5, 1: 6, 2: 7}, main_ident)]
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
        view._vlc_playlist_ready.emit(mapping)
        _drain()
        assert view._vlc_item_ids == mapping


def test_playlist_ready_accepts_partial_map(_qapp):
    """只匹配上一部分集时按原样收下：不臆造条目、不抛异常。"""
    view = _view(_video_detail(5), 0)
    view._vlc_playlist_ready.emit({0: 5, 1: 6})
    _drain()
    assert view._vlc_item_ids == {0: 5, 1: 6}


def test_playlist_ready_rejects_non_int_ids(_qapp, caplog):
    """非 int 的项 id → 丢弃本轮映射并告警，**不得**偷偷 int() 掉。

    id 已在 player_playlist_items 单点归一化；这里再出现非 int 说明上游
    guard 漏了。此时 int() 强转会把手真 VLC 的空映射伪装成可用映射
    （切集命令拿着假 id 发给 VLC，症状是「切集永远没反应」）。
    """
    view = _view(_video_detail(3), 0)
    view._vlc_playlist_ready.emit({0: 5})
    _drain()
    with caplog.at_level(logging.WARNING):
        view._vlc_playlist_ready.emit({1: "6"})
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
    view._unregister_series()
    view._unregister_series()
    assert proxy.unregistered == ["k9"]
    assert view._series_key == "" and view._series_urls == []
    assert view._vlc_item_ids == {}


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
