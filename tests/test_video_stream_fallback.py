import threading
import time
from types import SimpleNamespace

import pytest

from framework.content import Chapter, Content, Detail
from framework.stream_resolver import StreamResolver
from gui.pages.reader.video_view import VideoView, _FetchStreamTask


@pytest.fixture
def streaming_content():
    content = Content(None, None, None)
    source = SimpleNamespace(
        source_id="demo", base_url="https://site/", content_type="video",
        raw={"api_endpoints": {"episode": {"engine": "ytdlp"}},
             "ad_block": {"enabled": False}},
    )
    calls = []

    def fetch_streams(url, fmt, timeout=None):
        calls.append((url, fmt))
        return f"https://cdn/video-{len(calls)}.mp4", ""

    content._ytdlp = SimpleNamespace(fetch_streams=fetch_streams)
    content._stream_resolver = StreamResolver(content, cache_ttl=5)
    return content, source, calls


@pytest.fixture
def view(_qapp, streaming_content):
    content, source, _ = streaming_content
    widget = VideoView(content)
    widget._source = source
    widget._episodes = [Chapter("episode", "https://site/episode")]
    widget._detail = Detail("demo", "video", "https://site/work", chapters=widget._episodes)
    widget._current_idx = 0
    yield widget
    widget._hide_timer.stop()
    widget._cast_playing_timer.stop()
    widget.close()


def test_real_content_ytdlp_is_refetched_after_resolver_ttl(streaming_content, monkeypatch):
    content, source, calls = streaming_content
    now = [100.0]
    monkeypatch.setattr("framework.stream_resolver.time.monotonic", lambda: now[0])
    assert content.fetch_video_streams(source, "https://site/episode", merged=True) == (
        "https://cdn/video-1.mp4", ""
    )
    now[0] = 104.0
    assert content.fetch_video_streams(source, "https://site/episode", merged=True)[0].endswith("video-1.mp4")
    now[0] = 106.0
    assert content.fetch_video_streams(source, "https://site/episode", merged=True)[0].endswith("video-2.mp4")
    assert len(calls) == 2


def test_gui_cannot_extend_resolver_cache_lifetime(view, streaming_content, monkeypatch):
    content, source, _ = streaming_content
    now = [100.0]
    monkeypatch.setattr("framework.stream_resolver.time.monotonic", lambda: now[0])
    pair = content.fetch_video_streams(source, "https://site/episode", quality="best", merged=True)
    now[0] = 104.0
    view._on_prefetch_done("https://site/episode", *pair, None, "best", view._stream_gen)
    key = view._stream_cache_key("https://site/episode", "best")
    assert view._stream_cache_get(key) == pair
    now[0] = 106.0
    assert view._stream_cache_get(key) is None


def test_gui_cannot_reuse_expired_signature(view, streaming_content, monkeypatch):
    content, source, _ = streaming_content
    now = [100.0]
    monkeypatch.setattr("framework.stream_resolver.time.monotonic", lambda: now[0])
    monkeypatch.setattr("framework.stream_resolver.time.time", lambda: 1700000000 + now[0])
    content._ytdlp.fetch_streams = lambda *a, **kw: ("https://cdn/v.mp4?expire=1700000102", "")
    pair = content.fetch_video_streams(source, "https://site/episode", quality="best", merged=True)
    view._on_prefetch_done("https://site/episode", *pair, None, "best", view._stream_gen)
    now[0] = 103.0
    assert view._stream_cache_get(view._stream_cache_key("https://site/episode", "best")) is None


def test_fetch_tasks_share_real_resolver_cache(streaming_content, _qapp):
    content, source, calls = streaming_content
    results = []
    for _ in range(2):
        task = _FetchStreamTask(content, source, "https://site/episode")
        task.signals.finished.connect(lambda *args: results.append(args))
        task.run()
    assert len(results) == 2
    assert all(result[1:4] == ("https://cdn/video-1.mp4", "", None) for result in results)
    assert len(calls) == 1


def test_retry_invalidates_resolver_cache(view, streaming_content, monkeypatch):
    content, source, calls = streaming_content
    content.fetch_video_streams(source, "https://site/episode", quality="best", merged=True)
    received = []
    monkeypatch.setattr(view, "_load_episode", lambda idx: received.append(
        content.fetch_video_streams(source, "https://site/episode", quality="best", merged=True)
    ))
    view._retry_play()
    assert received == [("https://cdn/video-2.mp4", "")]
    assert len(calls) == 2


@pytest.mark.parametrize("callback", ["_on_stream_loaded", "_on_prefetch_done"])
def test_stale_local_results_never_play_or_cache(view, callback, monkeypatch):
    played = []
    monkeypatch.setattr(view, "_request_play", lambda *args: played.append(args))
    view._stream_gen += 1
    getattr(view, callback)("https://site/episode", "https://cdn/old.mp4", "", None, "best", 0)
    assert view._stream_cache == {}
    assert played == []


def test_stale_cast_result_never_caches_or_consumes_new_pending(view):
    view._cast_gen = 2
    view._cast_stream_pending = (0, "start")
    view._on_cast_stream_ready("https://site/episode", "https://cdn/old.mp4", "", None, "best", 1)
    assert view._stream_cache == {}
    assert view._cast_stream_pending == (0, "start")


def test_stopped_view_rejects_late_local_result(view, monkeypatch):
    played = []
    monkeypatch.setattr(view, "_request_play", lambda *args: played.append(args))
    monkeypatch.setattr(view, "_stop_player", lambda: None)
    generation = view._stream_gen
    view.stop_playback()
    view._on_stream_loaded("https://site/episode", "https://cdn/old.mp4", "", None, "best", generation)
    assert view._stream_cache == {}
    assert played == []


def test_cancelled_task_discards_late_ytdlp_result(streaming_content, _qapp):
    content, source, _ = streaming_content
    started = threading.Event()
    release = threading.Event()

    def fetch_streams(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return "https://cdn/late.mp4", ""

    content._ytdlp.fetch_streams = fetch_streams
    task = _FetchStreamTask(content, source, "https://site/episode")
    worker = threading.Thread(target=task.run)
    worker.start()
    try:
        assert started.wait(1)
        task.cancel()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert content._stream_resolver._cache == {}


def test_locked_source_failure_does_not_switch_source(view, monkeypatch):
    emitted = []
    view._locked_source_id = "demo"
    view._current_sid = "demo"
    view._source_list = [{"sid": "demo"}, {"sid": "backup"}]
    view.source_changed.connect(emitted.append)
    view._on_stream_loaded("https://site/episode", "", "", "failed", "best", view._stream_gen)
    assert emitted == []
    assert "获取播放流失败" in view.play_label.text()


def test_cancel_probe_restores_progress_text_and_names(_qapp):
    view = VideoView(object())
    try:
        detail = SimpleNamespace(source_list=[{"sid": "a", "name": "线路 A"}], url="detail")
        view._detail = detail
        view._populate_source_combo(detail)
        view.probe_btn.setText("第 1/1，当前源：线路 A")
        view.source_combo.setItemText(0, "线路 A（可播放）")
        view._source_actions[0].setText("线路 A（可播放）")
        view._probe_task = object()
        view._cancel_probe_task()
        view._clear_probe_state()
        assert view.probe_btn.text() == "检测播放源"
        assert view.source_combo.itemText(0) == "线路 A"
        assert view._source_actions[0].text() == "线路 A"
    finally:
        view.close()


def test_task_budget_error_is_readable_and_does_not_leak_urls(streaming_content, _qapp):
    content, source, _ = streaming_content
    from framework.stream_resolver import StreamCancelledError

    def expired(*args, **kwargs):
        raise StreamCancelledError("stream resolution deadline exhausted")

    content.fetch_video_streams = expired
    task = _FetchStreamTask(content, source, "https://site/episode?token=secret")
    results = []
    task.signals.finished.connect(lambda *args: results.append(args))
    task.run()
    assert "超时" in results[0][3]
    assert "secret" not in results[0][3]


def test_retry_clears_gui_resolver_and_video_html_cache(view, streaming_content, monkeypatch):
    content, source, _ = streaming_content
    episode_url = "https://site/episode"
    content.fetch_video_streams(source, episode_url, quality="best", merged=True)
    view._stream_cache_set(view._stream_cache_key(episode_url, "best"), "https://cdn/old.mp4", "")
    cache_key = (source.source_id, episode_url)
    content._video_html_cache[cache_key] = "old html"
    content._detail_html_cache[cache_key] = (time.time() + 60, "old html")
    monkeypatch.setattr(view, "_load_episode", lambda idx: None)

    view._retry_play()

    assert view._stream_cache == {}
    assert content._stream_resolver._cache == {}
    assert cache_key not in content._video_html_cache
    assert cache_key not in content._detail_html_cache


def test_task_error_redacts_signed_url(streaming_content, _qapp):
    content, source, _ = streaming_content

    def failed(*args, **kwargs):
        raise RuntimeError("failed https://cdn/video.mp4?token=secret&sig=private")

    content.fetch_video_streams = failed
    task = _FetchStreamTask(content, source, "https://site/episode")
    results = []
    task.signals.finished.connect(lambda *args: results.append(args))
    task.run()

    assert "secret" not in results[0][3]
    assert "private" not in results[0][3]
    assert "?token=" not in results[0][3]
