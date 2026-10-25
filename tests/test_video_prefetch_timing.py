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

    view = VideoView(object())
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

    on_progress(0, {"seconds": 60.0, "duration": 600.0})
    assert calls == []

    on_progress(0, {"seconds": 360.0, "duration": 600.0})
    assert calls == [0]

    on_progress(0, {"seconds": 359.0, "duration": 600.0})
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
    """一次取流失败不能永久锁死预取，冷却后允许重试。"""
    from gui.pages.reader import video_view as module

    view = _view(3)
    view._current_idx = 0
    ep = view._episodes[1]
    key = view._stream_cache_key(ep.url, view._quality)

    view._on_prefetch_done(ep.url, "", "", "boom", view._quality, gen=0)
    assert key in view._prefetch_retry_at
    # 真实流程里 _prefetch_episode 会先把 key 记入 attempted；
    # 失败后仍保留该标记，只靠 retry_at 冷却控制重试。
    view._prefetch_attempted.add(key)

    started = []
    monkeypatch.setattr(
        module.QThreadPool, "globalInstance",
        lambda: SimpleNamespace(start=started.append),
    )
    # 冷却期内不再发起
    view._prefetch_episode(1)
    assert started == []

    # 冷却结束 → 允许重试
    view._prefetch_retry_at[key] = 0.0
    view._prefetch_episode(1)
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
