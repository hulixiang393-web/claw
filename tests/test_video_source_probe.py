import threading
import time
from types import SimpleNamespace

import pytest

from framework.stream_resolver import StreamCancelledError
from gui.pages.reader.video_view import VideoView, _ProbeSourcesTask


def _source(sid):
    return SimpleNamespace(source_id=sid)


def test_probe_task_deduplicates_limits_and_runs_serially(_qapp):
    active = 0
    calls = []
    lock = threading.Lock()

    class Content:
        def fetch_video_streams(self, source, url, **kwargs):
            nonlocal active
            with lock:
                active += 1
                assert active == 1
                calls.append(source.source_id)
            time.sleep(0.01)
            with lock:
                active -= 1
            return f"https://cdn/{source.source_id}.m3u8", ""

    sources = [
        {"source_id": "a", "source": _source("a"), "url": "a"},
        {"source_id": "a", "source": _source("a"), "url": "a2"},
        {"source_id": "b", "source": _source("b"), "url": "b"},
        {"source_id": "c", "source": _source("c"), "url": "c"},
    ]
    results = []
    task = _ProbeSourcesTask(Content(), sources, generation=7, max_sources=2, timeout_ms=1000)
    task.signals.finished.connect(lambda *args: results.append(args))

    task.run()

    assert calls == ["a", "b"]
    assert results == [({"a": "ok", "b": "ok"}, 7, task)]


def test_probe_task_marks_empty_and_failed(_qapp):
    class Content:
        def fetch_video_streams(self, source, url, **kwargs):
            if source.source_id == "empty":
                return "", ""
            raise RuntimeError("no stream")

    results = []
    task = _ProbeSourcesTask(
        Content(),
        [
            {"source_id": "empty", "source": _source("empty"), "url": "empty"},
            {"source_id": "failed", "source": _source("failed"), "url": "failed"},
        ],
        generation=3,
        timeout_ms=1000,
    )
    task.signals.finished.connect(lambda *args: results.append(args))

    task.run()

    assert results == [({"empty": "failed", "failed": "failed"}, 3, task)]


def test_probe_task_timeout_waits_for_worker_before_next_source(_qapp):
    started = threading.Event()
    release = threading.Event()
    next_started = threading.Event()
    calls = []

    class Content:
        def fetch_video_streams(self, source, url, **kwargs):
            calls.append(source.source_id)
            if source.source_id == "slow":
                started.set()
                release.wait(1)
                return "https://cdn/late.m3u8", ""
            next_started.set()
            return "https://cdn/ok.m3u8", ""

    results = []
    task = _ProbeSourcesTask(
        Content(),
        [
            {"source_id": "slow", "source": _source("slow"), "url": "slow"},
            {"source_id": "next", "source": _source("next"), "url": "next"},
        ],
        generation=11,
        timeout_ms=10,
    )
    task.signals.finished.connect(lambda *args: results.append(args))
    thread = threading.Thread(target=task.run)
    thread.start()
    assert started.wait(1)
    time.sleep(0.03)
    assert not next_started.is_set()
    release.set()
    thread.join(1)
    _qapp.processEvents()

    assert results == [({"slow": "timeout", "next": "ok"}, 11, task)]
    assert calls == ["slow", "next"]

    cancelled = _ProbeSourcesTask(
        Content(),
        [{"source_id": "slow", "source": _source("slow"), "url": "slow"}],
        generation=12,
        timeout_ms=1000,
    )
    cancelled.cancel()
    cancelled.run()
    assert cancelled.signals is not None


def test_stop_playback_clears_probe_marks_and_discards_late_results(_qapp, monkeypatch):
    view = VideoView(object())
    try:
        detail = SimpleNamespace(source_list=[{"sid": "a", "name": "线路 A"}], url="detail")
        view._detail = detail
        view._populate_source_combo(detail)
        task = object()
        view._probe_task = task
        view._on_sources_probed({"a": "ok"}, view._probe_generation, task)
        assert view.source_combo.itemText(0) == "线路 A（可播放）"
        assert view._source_actions[0].text() == "线路 A（可播放）"
        generation = view._probe_generation
        monkeypatch.setattr(view, "_cancel_stream_tasks", lambda: None)
        monkeypatch.setattr(view, "_invalidate_stream_cache", lambda: None)
        monkeypatch.setattr(view, "_stop_cast", lambda: None)
        monkeypatch.setattr(view, "_stop_player", lambda: None)
        view.stop_playback()
        assert view._probe_results == {}
        assert view.source_combo.itemText(0) == "线路 A"
        assert view._source_actions[0].text() == "线路 A"
        view._on_sources_probed({"late": "ok"}, generation, task)
        assert view._probe_generation != generation
        assert view._probe_results == {}
    finally:
        view.close()


def test_current_stale_probe_result_clears_task_and_restores_button(_qapp):
    view = VideoView(object())
    try:
        detail = SimpleNamespace(source_list=[{"sid": "a", "name": "线路 A"}], url="detail")
        view._detail = detail
        view._populate_source_combo(detail)
        view._probe_task = object()
        view._probe_results = {"a": "ok"}
        view._update_probe_button()
        assert not view.probe_btn.isEnabled()
        view._on_sources_probed({"a": "timeout"}, view._probe_generation - 1, view._probe_task)
        assert view._probe_task is None
        assert view._probe_results == {}
        assert view.probe_btn.isEnabled()
        assert view.source_combo.itemText(0) == "线路 A"
    finally:
        view.close()


def test_video_view_only_probes_after_manual_call(_qapp, monkeypatch):
    view = VideoView(object())
    try:
        view._detail = SimpleNamespace(source_list=[{"sid": "a"}])
        created = []
        monkeypatch.setattr(view, "_start_probe_task", lambda *args: created.append(True))
        assert created == []
        view._probe_sources()
        assert created == [True]
    finally:
        view.close()


def test_video_view_has_manual_probe_button_and_marks_source_results(_qapp):
    view = VideoView(object())
    try:
        detail = SimpleNamespace(source_list=[
            {"sid": "a", "name": "线路 A"},
            {"sid": "b", "name": "线路 B"},
            {"sid": "c", "name": "线路 C"},
        ], url="detail")
        view._detail = detail
        view._populate_source_combo(detail)
        assert not view.probe_btn.isHidden()
        assert view.source_combo.itemData(0) == "a"
        view._probe_task = object()
        view._on_sources_probed(
            {"a": "ok", "b": "timeout", "c": "failed"},
            view._probe_generation, view._probe_task,
        )
        assert view.source_combo.itemText(0) == "线路 A（可播放）"
        assert view.source_combo.itemText(1) == "线路 B（超时）"
        assert view.source_combo.itemText(2) == "线路 C"
        assert view.source_combo.itemData(0) == "a"
    finally:
        view.close()


def test_video_view_probe_button_controls_task_and_disabled_config(_qapp, monkeypatch):
    view = VideoView(object())
    try:
        detail = SimpleNamespace(source_list=[{"sid": "a", "name": "线路 A"}], url="detail")
        view._detail = detail
        view._populate_source_combo(detail)
        created = []
        monkeypatch.setattr(view, "_start_probe_task", lambda *args: created.append(True))
        view.probe_btn.click()
        assert created == [True]
        assert not view.probe_btn.isEnabled()
        view._on_sources_probed({}, view._probe_generation, view._probe_task)
        assert view.probe_btn.isEnabled()
        view._source_probe_settings = {"enabled": False}
        view._populate_source_combo(detail)
        assert not view.probe_btn.isEnabled()
        created.clear()
        view._probe_sources()
        assert created == []
    finally:
        view.close()


def test_source_menu_selection_changes_combo(_qapp):
    view = VideoView(object())
    try:
        detail = SimpleNamespace(source_list=[
            {"sid": "a", "name": "线路 A"},
            {"sid": "b", "name": "线路 B"},
        ])
        view._detail = detail
        view._populate_source_combo(detail)
        view._select_source(1)
        assert view.source_combo.currentIndex() == 1
    finally:
        view.close()


def _probe_view(monkeypatch):
    from gui.pages.reader import video_view

    tasks = []
    monkeypatch.setattr(
        video_view.QThreadPool, "globalInstance",
        lambda: SimpleNamespace(start=tasks.append),
    )
    view = VideoView(object())
    view._detail = SimpleNamespace(
        source_list=[{"sid": "a", "name": "线路 A"}], url="detail",
    )
    view._populate_source_combo(view._detail)
    return view, tasks


def _finish_probe(task, results):
    task.signals.finished.emit(results, task._generation, task)


@pytest.mark.parametrize("finish_before_play", [False, True])
def test_probe_results_survive_normal_play(_qapp, monkeypatch, finish_before_play):
    from framework import external_player

    view, tasks = _probe_view(monkeypatch)
    monkeypatch.setattr(external_player, "open_with_player", lambda *a, **kw: "")
    monkeypatch.setattr(view, "_build_series_playlist", lambda *a: None)
    monkeypatch.setattr(view, "_force_proxy_enabled", lambda: False)
    monkeypatch.setattr(view, "_source_network_caching_ms", lambda: 8000)
    try:
        view._probe_sources()
        task = tasks[-1]
        generation = view._probe_generation
        play_generation = view._play_gen
        if finish_before_play:
            _finish_probe(task, {"a": "ok"})
        view._play("https://cdn/test.mp4", "", "测试")
        assert view._play_gen > play_generation
        assert view._probe_generation == generation
        if not finish_before_play:
            _finish_probe(task, {"a": "ok"})
        assert view._probe_results == {"a": "ok"}
        assert view.source_combo.itemText(0) == "线路 A（可播放）"
        assert view._source_actions[0].text() == "线路 A（可播放）"
        assert view._probe_task is None
        assert view.probe_btn.isEnabled()
    finally:
        view.close()


def test_progress_options_falls_back_when_episode_has_no_title(_qapp, monkeypatch):
    from framework import external_player

    view = VideoView(object())
    detail = SimpleNamespace(url="detail", title="详情")
    view._detail = detail
    view._episodes = [SimpleNamespace(url="episode")]
    view._current_idx = 0
    handoffs = []
    progress = []
    view.handoff_writer = lambda *args: handoffs.append(args)
    view.progress_writer = progress.append
    monkeypatch.setattr(external_player, "flush_player_progress", lambda: None)
    try:
        options = view._progress_options()
        options["on_handoff"]()
        options["on_progress"](0, {"seconds": 5, "duration": 10})
        assert handoffs == [(detail, "detail", "第1集", "episode")]
        assert progress[0][1:3] == ("第1集", "episode")
    finally:
        view.close()


@pytest.mark.parametrize("new_finished", [False, True])
@pytest.mark.parametrize("same_generation", [False, True])
def test_old_probe_cannot_clear_new_task_or_marks(
    _qapp, monkeypatch, new_finished, same_generation,
):
    view, tasks = _probe_view(monkeypatch)
    try:
        view._probe_sources()
        old = tasks[-1]
        view._probe_sources()
        new = tasks[-1]
        assert old._cancelled.is_set()
        assert new._generation > old._generation
        if new_finished:
            _finish_probe(new, {"a": "ok"})
        generation = new._generation if same_generation else old._generation
        old.signals.finished.emit({"a": "timeout"}, generation, old)
        assert view._probe_task is (None if new_finished else new)
        assert view.probe_btn.isEnabled() == new_finished
        assert view._probe_results == ({"a": "ok"} if new_finished else {})
        text = "线路 A（可播放）" if new_finished else "线路 A"
        assert view.source_combo.itemText(0) == text
        assert view._source_actions[0].text() == text
        if not new_finished:
            _finish_probe(new, {"a": "ok"})
            assert view._probe_results == {"a": "ok"}
            assert view.probe_btn.isEnabled()
    finally:
        view.close()


@pytest.mark.parametrize("entry", ["load", "reload_detail", "switch"])
def test_detail_transition_clears_probe_state_and_rebuilds_names(_qapp, monkeypatch, entry):
    view, tasks = _probe_view(monkeypatch)
    try:
        view._probe_sources()
        _finish_probe(tasks[-1], {"a": "ok"})
        view._probe_sources()
        old = tasks[-1]
        assert view.source_combo.itemText(0) == "线路 A（可播放）"
        for method in (
            "_cancel_stream_tasks", "_invalidate_stream_cache", "_stop_cast",
            "_stop_player", "_populate_ep_cards", "_maybe_load_recommendations",
        ):
            monkeypatch.setattr(view, method, lambda *a: None)
        detail = SimpleNamespace(
            source_list=[{"sid": "a", "name": "新线路 A"}],
            chapters=[SimpleNamespace(title="第 1 集", url="episode")],
            url="new-detail", title="新作品",
        )
        if entry == "load":
            view.load(_source("new"), detail)
        elif entry == "reload_detail":
            view.reload_detail(detail)
        else:
            view._detail.source_list.append({"sid": "b", "name": "线路 B"})
            view._populate_source_combo(view._detail)
            view.source_combo.setCurrentIndex(1)
        expected = "线路 A" if entry == "switch" else "新线路 A"
        assert old._cancelled.is_set()
        assert view._probe_generation > old._generation
        assert view._probe_task is None
        assert view._probe_results == {}
        assert view.source_combo.itemText(0) == expected
        assert view._source_actions[0].text() == expected
        assert view._probe_names["a"] == expected
        assert view.probe_btn.isEnabled()
        _finish_probe(old, {"a": "timeout"})
        assert view._probe_results == {}
        assert view.source_combo.itemText(0) == expected
        assert view._source_actions[0].text() == expected
    finally:
        view.close()


def test_switch_source_clears_probe_results_and_cancels_task(_qapp, monkeypatch):
    view = VideoView(object())
    try:
        detail = SimpleNamespace(source_list=[
            {"sid": "a", "name": "线路 A"},
            {"sid": "b", "name": "线路 B"},
        ])
        view._detail = detail
        view._populate_source_combo(detail)
        view._probe_results = {"a": "ok"}
        cancelled = []
        monkeypatch.setattr(view, "_cancel_probe_task", lambda: cancelled.append(True))
        view.source_combo.setCurrentIndex(1)
        assert cancelled == [True]
        assert view._probe_results == {}
    finally:
        view.close()
