# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _detail(n):
    from framework.content import Chapter, Detail

    return Detail(
        source_id="demo", content_type="video", url="http://example.com/v/1",
        title="work", cover="", chapters=[
            Chapter(f"ep{i + 1}", f"http://e/{i}", cover="") for i in range(n)
        ],
    )


def _view(n):
    from gui.pages.reader.video_view import VideoView

    # 发信号需要 QObject 宿主：VideoView() 无 parent 时会挂到父级窗口链上，
    # offscreen 下也能正常 emit（不要用 object()，那不是 QObject）。
    view = VideoView(None)
    view._detail = _detail(n)
    view._episodes = view._detail.chapters
    view._source = SimpleNamespace(source_id="demo", content_type="video")
    return view


def test_stream_loaded_does_not_prefetch_immediately(_qapp, monkeypatch):
    view = _view(3)
    view._current_idx = 0
    played = []
    prefetched = []
    monkeypatch.setattr(view, "_request_play", lambda *a: played.append(a))
    monkeypatch.setattr(view, "_prefetch_next", lambda idx=-1: prefetched.append(idx))

    view._on_stream_loaded("http://e/0", "video-url", "", None, "best", gen=0)

    assert played
    assert prefetched == []


def test_prefetch_triggers_only_within_four_minutes_of_end(_qapp, monkeypatch):
    view = _view(3)
    view._current_idx = 0
    calls = []
    monkeypatch.setattr(view, "_prefetch_next", lambda idx=-1: calls.append(idx))
    options = view._progress_options()
    on_progress = options["on_progress"]

    # 预取窗口 = 片尾前 4 分钟：600s 的片子在 360s 起要预取下一集，
    # 359s（剩 241s）还在窗口外，361s（剩 239s）已进窗口。
    on_progress(0, {"seconds": 60.0, "duration": 600.0})
    assert calls == []

    on_progress(0, {"seconds": 361.0, "duration": 600.0})
    assert calls == [0]

    # 同一集在同一个播放位置只预取一次：进度在窗口内来回晃（暂停/回拖两下）
    # 不重复取流，否则既压站又可能抢本集带宽。
    on_progress(0, {"seconds": 359.0, "duration": 600.0})
    on_progress(0, {"seconds": 362.0, "duration": 600.0})
    assert calls == [0]


def test_cached_episode_load_does_not_prefetch_immediately(_qapp, monkeypatch):
    view = _view(3)
    view._current_idx = 0
    ep = view._episodes[0]
    view._stream_cache_set(view._stream_cache_key(ep.url, view._quality),
                           "video-url", "")
    played = []
    prefetched = []
    monkeypatch.setattr(view, "_play", lambda *a: played.append(a))
    monkeypatch.setattr(view, "_prefetch_next", lambda idx=-1: prefetched.append(idx))

    view._load_episode(0)

    assert played
    assert prefetched == []


def test_prefetch_resolves_link_without_playback(_qapp, monkeypatch):
    from gui.pages.reader import video_view as module

    view = _view(3)
    view._current_idx = 0
    started = []
    monkeypatch.setattr(module.QThreadPool, "globalInstance",
                        lambda: SimpleNamespace(start=started.append))
    played = []
    monkeypatch.setattr(view, "_request_play", lambda *a: played.append(a))
    monkeypatch.setattr(view, "_play", lambda *a: played.append(a))

    view._prefetch_episode(1)
    assert len(started) == 1
    assert played == []
    assert view._prefetch_idx == 1

    view._prefetch_idx = -2
    view._prefetch_episode(1)
    assert len(started) == 1
    assert played == []


class _RecordingContent:
    def __init__(self):
        self.calls = []

    def fetch_video_streams(self, source, url, quality="", merged=False):
        self.calls.append((url, quality, merged))
        return ("https://cdn/next.mp4", "")


def test_prefetch_task_resolves_url_without_merge(_qapp):
    from gui.pages.reader.video_tasks import _FetchStreamTask

    content = _RecordingContent()
    task = _FetchStreamTask(content, object(), "http://e/1", "best",
                            gen=0, prefetch=True)
    task.run()

    assert content.calls == [("http://e/1", "best", False)]


def test_playback_task_still_requests_merge(_qapp):
    from gui.pages.reader.video_tasks import _FetchStreamTask

    content = _RecordingContent()
    task = _FetchStreamTask(content, object(), "http://e/1", "best", gen=0)
    task.run()

    assert content.calls == [("http://e/1", "best", True)]


def test_prefetched_link_is_reused_for_non_ytdlp(_qapp, monkeypatch):
    view = _view(3)
    view._current_idx = 0
    ep = view._episodes[1]
    view._prefetch_cache[view._stream_cache_key(ep.url, view._quality)] = (
        "https://cdn/next.mp4", "")
    played = []
    monkeypatch.setattr(view, "_play", lambda *a: played.append(a))
    started = []
    monkeypatch.setattr("gui.pages.reader.video_view.QThreadPool",
                        SimpleNamespace(globalInstance=lambda: SimpleNamespace(
                            start=started.append)))

    view._load_episode(1)

    assert played and played[0][0] == "https://cdn/next.mp4"
    assert started == []
    assert view._prefetch_cache == {}


def test_prefetch_cache_survives_four_minute_window(_qapp, monkeypatch):
    """预取结果必须在 4 分钟窗口内仍可命中（旧实现只缓存 30s，等于无效）。"""
    import time

    view = _view(3)
    view._current_idx = 0
    ep = view._episodes[1]
    key = view._stream_cache_key(ep.url, view._quality)
    monkeypatch.setattr(view, "_warm_next_episode", lambda *a: None)

    view._on_prefetch_done(ep.url, "https://cdn/next.mp4", "", None,
                           view._quality, gen=0)

    stored = view._prefetch_cache[key]
    assert stored[0] - time.monotonic() >= view.PREFETCH_BEFORE_END_S
    # 模拟预取后已经过了 3 分钟（剩余 60s）：仍必须命中
    view._prefetch_cache[key] = (time.monotonic() + 60.0,
                                 "https://cdn/next.mp4", "")
    assert view._prefetch_cache_get(key) == ("https://cdn/next.mp4", "")


def test_prefetch_cache_uses_signed_url_expiry(_qapp, monkeypatch):
    """带时效签名的链接按 expire 计算 TTL；无过期信息则不缓存。"""
    import time

    view = _view(3)
    view._current_idx = 0
    ep = view._episodes[1]
    key = view._stream_cache_key(ep.url, view._quality)
    monkeypatch.setattr(view, "_warm_next_episode", lambda *a: None)

    future = int(time.time()) + 300
    signed = f"https://cdn/next.mp4?token=abc&expire={future}"
    view._on_prefetch_done(ep.url, signed, "", None, view._quality, gen=0)
    assert key in view._prefetch_cache
    assert view._prefetch_cache_get(key) == (signed, "")

    view._prefetch_cache.clear()
    view._on_prefetch_done(
        ep.url, "https://cdn/next.mp4?token=abc", "", None, view._quality, gen=0
    )
    assert key not in view._prefetch_cache


def test_prefetch_completion_warms_series_memo_when_ttl_covers_window(_qapp, monkeypatch):
    """预取完成要预热媒体代理 memo；签名链接覆盖不了窗口时不能预热。"""
    import time

    view = _view(3)
    view._current_idx = 0
    view._series_key = "series-key"
    ep = view._episodes[1]
    warmed: list = []
    monkeypatch.setattr(view, "_warm_next_episode", lambda *a: warmed.append(a))

    view._on_prefetch_done(ep.url, "https://cdn/next.mp4", "", None,
                           view._quality, gen=0)
    assert len(warmed) == 1

    warmed.clear()
    future = int(time.time()) + 60  # 60s 后过期 < 4 分钟窗口
    short_signed = f"https://cdn/next.mp4?expire={future}"
    view._on_prefetch_done(ep.url, short_signed, "", None, view._quality, gen=0)
    assert warmed == []


def test_prefetch_failure_schedules_retry(_qapp, monkeypatch):
    """一次取流失败不能永久锁死预取，冷却后允许重试；额度用完才彻底放弃。"""
    from gui.pages.reader import video_view as module

    view = _view(3)
    view._current_idx = 0
    ep = view._episodes[1]
    key = view._stream_cache_key(ep.url, view._quality)

    view._on_prefetch_done(ep.url, "", "", "boom", view._quality, gen=0)
    assert key in view._prefetch_retry_at

    started = []
    monkeypatch.setattr(
        module.QThreadPool, "globalInstance",
        lambda: SimpleNamespace(start=started.append),
    )
    # 冷却期内不再发起
    view._prefetch_next(0)
    assert started == []

    # 冷却结束 → 允许重试（唯一入口 _prefetch_next 负责清冷却标记）
    view._prefetch_retry_at[key] = 0.0
    view._prefetch_next(0)
    assert len(started) == 1
    assert key not in view._prefetch_retry_at

    # 连续失败到额度上限 → 放弃该集，之后不再发请求。
    # 额度记在「本集窗口锁存」上，这里按真实流程先锁存一次再连续失败。
    view._prefetch_idx = -2
    view._prefetch_latch_at(0, 400.0, 600.0)
    for _ in range(view.PREFETCH_MAX_ATTEMPTS):
        view._on_prefetch_done(ep.url, "", "", "boom", view._quality, gen=0)
    assert key in view._prefetch_hopeless
    view._prefetch_retry_at.pop(key, None)
    view._prefetch_idx = -2
    view._prefetch_next(0)
    assert len(started) == 1

    # 换集/换源重置后重新可试
    view._cancel_stream_tasks()
    assert key not in view._prefetch_hopeless
    started.clear()
    view._prefetch_next(0)
    assert len(started) == 1


def test_prefetched_link_ignored_for_ytdlp(_qapp, monkeypatch):
    from gui.pages.reader import video_view as module

    view = _view(3)
    view._source = SimpleNamespace(
        source_id="demo", content_type="video",
        raw={"api_endpoints": {"episode": {"engine": "ytdlp"}}},
    )
    view._current_idx = 0
    ep = view._episodes[1]
    key = view._stream_cache_key(ep.url, view._quality)
    view._prefetch_cache[key] = ("https://cdn/next.mp4", "")
    view._content = _RecordingContent()
    started = []
    monkeypatch.setattr(module.QThreadPool, "globalInstance",
                        lambda: SimpleNamespace(start=started.append))
    monkeypatch.setattr(view, "_play", lambda *a: None)

    assert view._source_needs_merged() is True
    view._load_episode(1)

    assert len(started) == 1
    assert view._prefetch_cache.get(key) == ("https://cdn/next.mp4", "")


# ---------------------------------------------------------------------- #
# 2026-10-28 预取完善：锁存 / 巡检 / 串行 / 不抢本集
# ---------------------------------------------------------------------- #
def test_prefetch_tick_retries_after_cooldown(_qapp, monkeypatch):
    """失败冷却结束后由巡检补一次重试（进度回调此时可能已跑完窗口）。"""
    import framework.external_player as player

    view = _view(3)
    view._current_idx = 0
    ep = view._episodes[1]
    key = view._stream_cache_key(ep.url, view._quality)
    view._prefetch_state[(0, view._quality)] = ((400.0, 600.0), 1)
    view._prefetch_retry_at[key] = 0.0  # 冷却已到期
    monkeypatch.setattr(player, "player_session", lambda: {"ok": True})
    monkeypatch.setattr(
        player, "player_status",
        lambda timeout=1.0: {"seconds": 400.0, "duration": 600.0},
    )
    calls = []
    monkeypatch.setattr(view, "_prefetch_next", lambda idx=-1: calls.append(idx))
    view._prefetch_tick()
    assert calls == [0]


def test_prefetch_tick_silent_outside_window(_qapp, monkeypatch):
    """窗口外巡检不预取，也不发任何请求。"""
    import framework.external_player as player

    view = _view(3)
    view._current_idx = 0
    monkeypatch.setattr(player, "player_session", lambda: {"ok": True})
    monkeypatch.setattr(
        player, "player_status",
        lambda timeout=1.0: {"seconds": 30.0, "duration": 600.0},
    )
    calls = []
    monkeypatch.setattr(view, "_prefetch_next", lambda idx=-1: calls.append(idx))
    view._prefetch_tick()
    assert calls == []


def test_prefetch_tick_stops_without_player_session(_qapp, monkeypatch):
    """没有外播会话（未播放/已关窗）时巡检直接返回，不读状态。"""
    import framework.external_player as player

    view = _view(3)
    view._current_idx = 0
    monkeypatch.setattr(player, "player_session", lambda: None)
    read = []
    monkeypatch.setattr(player, "player_status",
                        lambda timeout=1.0: read.append(1) or None)
    calls = []
    monkeypatch.setattr(view, "_prefetch_next", lambda idx=-1: calls.append(idx))
    view._prefetch_tick()
    assert calls == [] and read == []


def test_prefetch_tick_rewinds_latch_after_seek_back(_qapp, monkeypatch):
    """用户回拖后旧锁存必须作废，否则回拖进窗口也不再预取。

    600s 的片子：从 580s（窗口内）回拖到 400s（仍在窗口内但位置变化），
    锁存若不失效就会一直挡着。
    """
    import framework.external_player as player

    view = _view(3)
    view._current_idx = 0
    view._prefetch_state[(0, view._quality)] = ((580.0, 600.0), 0)
    monkeypatch.setattr(player, "player_session", lambda: {"ok": True})
    monkeypatch.setattr(
        player, "player_status",
        lambda timeout=1.0: {"seconds": 400.0, "duration": 600.0},
    )
    calls = []
    monkeypatch.setattr(view, "_prefetch_next", lambda idx=-1: calls.append(idx))
    view._prefetch_tick()
    assert calls == [0]
    assert view._prefetch_state[(0, view._quality)][0][0] == 400.0


def test_prefetch_is_serial(_qapp, monkeypatch):
    """串行是硬约束：上一路没落定时绝不能再发一路（否则抢本集带宽）。"""
    from gui.pages.reader import video_view as module

    view = _view(3)
    view._current_idx = 0
    started = []
    monkeypatch.setattr(module.QThreadPool, "globalInstance",
                        lambda: SimpleNamespace(start=started.append))
    view._prefetch_next(0)
    assert len(started) == 1
    episode_url = view._episodes[1].url
    # 在拉期间无论谁再触发都不发
    view._prefetch_latch_at(0, 400.0, 600.0)
    view._prefetch_next(0)
    view._maybe_prefetch_next(0, 500.0, 600.0)
    assert len(started) == 1
    # 落定（并发回了流）之后允许再拉——已缓存的会短路，这里先清缓存
    view._prefetch_idx = -2
    view._prefetch_state.clear()
    view._invalidate_stream_cache(episode_url, view._quality)
    view._prefetch_next(0)
    assert len(started) == 2


def test_series_memo_hit_skips_prefetch(_qapp, monkeypatch):
    """媒体代理已有该集映射 → 播放端不会再跑 resolver，预取必须跳过。"""
    from gui.pages.reader import video_view as module

    view = _view(3)
    view._current_idx = 0
    view._series_key = "series-key"
    monkeypatch.setattr(view, "_series_memo_ready", lambda idx: True)
    started = []
    monkeypatch.setattr(module.QThreadPool, "globalInstance",
                        lambda: SimpleNamespace(start=started.append))
    view._prefetch_next(0)
    assert started == []


def test_stream_loaded_cancels_same_episode_prefetch(_qapp, monkeypatch):
    """本集取流回来时，若预取恰好在拉同一集，必须取消而不是再解析一遍。"""
    view = _view(3)
    view._current_idx = 1
    ep = view._episodes[1]
    cancelled = []

    class _Task:
        _url = ep.url

        def cancel(self):
            cancelled.append(1)

    view._prefetch_task = _Task()
    view._prefetch_idx = 1
    monkeypatch.setattr(view, "_request_play", lambda *a: None)
    view._on_stream_loaded(ep.url, "https://cdn/cur.mp4", "", None, view._quality,
                           gen=view._stream_gen)
    assert cancelled == [1]
    assert view._prefetch_idx == -2


def test_success_resets_retry_budget(_qapp, monkeypatch):
    """取到真链接后把锁存的重试额度清零，用户回拖进窗口还能再预取。"""
    view = _view(3)
    view._current_idx = 0
    ep = view._episodes[1]
    view._prefetch_latch_at(0, 500.0, 600.0)
    view._prefetch_state[(0, view._quality)] = ((500.0, 600.0), 2)
    monkeypatch.setattr(view, "_warm_next_episode", lambda *a: None)
    view._on_prefetch_done(ep.url, "https://cdn/next.mp4", "", None,
                           view._quality, gen=0)
    assert view._prefetch_state[(0, view._quality)][1] == 0
    assert view._prefetch_hopeless == set()
