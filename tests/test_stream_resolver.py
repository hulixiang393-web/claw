import threading
import time

import pytest

from framework.stream_resolver import StreamCancelledError, StreamResolver


StreamBudget = getattr(__import__("framework.stream_resolver", fromlist=["StreamBudget"]), "StreamBudget", None)


class _Source:
    source_id = "demo"


class _Content:
    def __init__(self, candidates=None, delay=0):
        self.candidates = candidates or []
        self.delay = delay
        self.calls = 0

    def resolve_video_stream_candidates(self, source, episode_url, quality, merged):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return list(self.candidates)


@pytest.mark.parametrize("source_id,budget,expected", [
    ("agedm", "pipeline", ("https://cdn/pipeline.m3u8", "")),
    ("agedm", None, ("", "")),
    ("demo", "pipeline", ("", "")),
    ("demo", None, ("", "")),
])
def test_content_pipeline_budget_is_age_opt_in(monkeypatch, source_id, budget, expected):
    from framework.content import Content

    content = Content(None, None, None)
    source = type("Source", (), {"source_id": source_id, "raw": {"endpoints": {"content": {"episode": {}}}}})()
    if budget:
        source.raw["endpoints"]["content"]["episode"]["stream_budget"] = budget
    calls = []

    def pipeline(*args, context=None, source_lock=False, **kwargs):
        calls.append((context, source_lock))
        time.sleep(0.08)
        context.check()
        return "https://cdn/pipeline.m3u8", ""

    monkeypatch.setattr(content, "_fetch_video_streams_legacy", pipeline)
    resolver = StreamResolver(content, total_budget=1, candidate_timeout=0.02)
    assert resolver.resolve(source, "https://site/episode", source_lock=True) == expected
    assert len(calls) == 1
    assert calls[0][1] is True
    until = time.monotonic() + 1
    while resolver._inflight and time.monotonic() < until:
        time.sleep(0.005)
    assert resolver._inflight == {}
    assert resolver.get_cached(source, "https://site/episode") == (expected if expected[0] else None)


def test_pipeline_cancel_returns_promptly_and_keeps_worker_inflight(monkeypatch):
    from framework.content import Content

    content = Content(None, None, None)
    source = _Source()
    source.source_id = "agedm"
    source.raw = {"endpoints": {"content": {"episode": {"stream_budget": "pipeline"}}}}
    started = threading.Event()
    release = threading.Event()
    errors = []

    def pipeline(*args, **kwargs):
        started.set()
        release.wait(2)
        return "https://cdn/late.m3u8", ""

    monkeypatch.setattr(content, "_fetch_video_streams_legacy", pipeline)
    resolver = StreamResolver(content, total_budget=5, candidate_timeout=0.02)

    def run():
        try:
            resolver.resolve(source, "https://site/episode")
        except StreamCancelledError as exc:
            errors.append(exc)

    owner = threading.Thread(target=run)
    owner.start()
    try:
        assert started.wait(1)
        resolver.invalidate(source, "https://site/episode")
        owner.join(0.5)
        assert not owner.is_alive()
        assert errors
        assert resolver._inflight
        assert resolver._cache == {}
    finally:
        release.set()
        owner.join(2)
    until = time.monotonic() + 1
    while resolver._inflight and time.monotonic() < until:
        time.sleep(0.005)
    assert resolver._inflight == {}
    assert resolver._cache == {}


def test_pipeline_descriptor_is_source_agnostic():
    def slow():
        time.sleep(0.08)
        return "https://cdn/pipeline.m3u8", ""

    resolver = StreamResolver(
        _Content([{"resolver": slow, "budget": "pipeline"}]),
        total_budget=1, candidate_timeout=0.02,
    )
    assert resolver.resolve(_Source(), "https://site/episode") == ("https://cdn/pipeline.m3u8", "")


def test_legacy_loader_keeps_short_budget_and_discards_late_result():
    from types import SimpleNamespace

    release = threading.Event()

    def legacy(*args, **kwargs):
        release.wait(1)
        return "https://cdn/late.m3u8", ""

    resolver = StreamResolver(
        SimpleNamespace(_fetch_video_streams_legacy=legacy),
        total_budget=1, candidate_timeout=0.02,
    )
    try:
        assert resolver.resolve(_Source(), "https://site/episode") == ("", "")
        assert resolver._inflight
        assert resolver._cache == {}
    finally:
        release.set()
    until = time.monotonic() + 1
    while resolver._inflight and time.monotonic() < until:
        time.sleep(0.005)
    assert resolver._inflight == {}
    assert resolver._cache == {}


def test_short_candidate_still_times_out_and_tries_next():
    release = threading.Event()

    def slow():
        release.wait(1)
        return "https://cdn/late.m3u8", ""

    resolver = StreamResolver(
        _Content([slow, ("https://cdn/next.m3u8", "")]),
        total_budget=1, candidate_timeout=0.02,
    )
    try:
        assert resolver.resolve(_Source(), "https://site/episode") == (
            "https://cdn/next.m3u8", ""
        )
    finally:
        release.set()


def test_direct_media_returns_before_loading_legacy_candidates():
    content = _Content([("https://cdn/legacy.m3u8", "")], delay=0.2)
    resolver = StreamResolver(content, total_budget=0.05)

    assert resolver.resolve(_Source(), "https://cdn/direct.m3u8", merged=True) == ("https://cdn/direct.m3u8", "")
    assert content.calls == 0


def test_candidates_are_deduplicated_before_maximum_is_applied():
    content = _Content([
        ("https://cdn/one.m3u8", ""),
        ("https://cdn/one.m3u8", ""),
        ("https://cdn/two.m3u8", ""),
    ])
    resolver = StreamResolver(content, max_candidates=2)

    assert resolver.resolve(_Source(), "https://site/episode") == ("https://cdn/one.m3u8", "")
    assert content.calls == 1


def test_success_is_cached_but_failure_is_not_cached():
    content = _Content([("https://cdn/video.m3u8", "")])
    resolver = StreamResolver(content)

    assert resolver.resolve(_Source(), "https://site/episode") == ("https://cdn/video.m3u8", "")
    assert resolver.resolve(_Source(), "https://site/episode") == ("https://cdn/video.m3u8", "")
    assert content.calls == 1

    content.candidates = []
    assert resolver.resolve(_Source(), "https://site/failure") == ("", "")
    assert resolver.resolve(_Source(), "https://site/failure") == ("", "")
    assert content.calls == 3


def test_total_budget_stops_later_candidates(monkeypatch):
    content = _Content([("https://cdn/one.m3u8", ""), ("https://cdn/two.m3u8", "")])
    now = [0.0]
    monkeypatch.setattr("framework.stream_resolver.time.monotonic", lambda: now[0])

    def load(*args):
        content.calls += 1
        now[0] += 31.0
        return list(content.candidates)

    content.resolve_video_stream_candidates = load
    resolver = StreamResolver(content, total_budget=30)

    with pytest.raises(StreamCancelledError):
        resolver.resolve(_Source(), "https://site/episode")
    assert content.calls == 1


def test_same_key_concurrent_requests_share_one_resolution():
    content = _Content([("https://cdn/video.m3u8", "")], delay=0.05)
    resolver = StreamResolver(content)
    results = []

    def run():
        results.append(resolver.resolve(_Source(), "https://site/episode", "1080p", True))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [("https://cdn/video.m3u8", "")] * 4
    assert content.calls == 1


def test_timed_out_resolution_remains_inflight_until_loader_finishes():
    started = threading.Event()
    release = threading.Event()
    calls = []

    class _BlockingContent:
        def resolve_video_stream_candidates(self, *args):
            calls.append("started")
            started.set()
            release.wait()
            return [("https://cdn/late.m3u8", "")]

    resolver = StreamResolver(_BlockingContent(), total_budget=0.05)
    errors = []

    def run():
        try:
            errors.append(("result", resolver.resolve(_Source(), "https://site/episode")))
        except (TimeoutError, RuntimeError) as exc:
            errors.append(("error", str(exc)))



    owner = threading.Thread(target=run)
    owner.start()
    assert started.wait(1)
    owner.join(0.5)
    assert not owner.is_alive()

    waiter = threading.Thread(target=run)
    waiter.start()
    waiter.join(0.2)

    assert not waiter.is_alive()
    assert calls == ["started"]
    release.set()
    assert len(errors) == 2
    deadline = time.monotonic() + 1
    while resolver._inflight and time.monotonic() < deadline:
        time.sleep(0.005)
    assert resolver.resolve(_Source(), "https://site/episode") == ("https://cdn/late.m3u8", "")
    assert calls == ["started", "started"]


def test_waiter_uses_remaining_time_from_its_resolution_deadline():
    loader_started = threading.Event()
    release_loader = threading.Event()

    class _BlockingContent:
        def resolve_video_stream_candidates(self, *args):
            loader_started.set()
            release_loader.wait()
            return []

    resolver = StreamResolver(_BlockingContent(), total_budget=0.15)
    owner = threading.Thread(target=lambda: _resolve_ignoring_error(resolver))
    owner.start()
    assert loader_started.wait(1)

    started = threading.Event()
    elapsed = []

    def wait():
        started.set()
        began = time.monotonic()
        try:
            resolver.resolve(_Source(), "https://site/episode")
        except (TimeoutError, RuntimeError):
            elapsed.append(time.monotonic() - began)

    with resolver._lock:
        waiter = threading.Thread(target=wait)
        waiter.start()
        assert started.wait(1)
        time.sleep(0.1)
    waiter.join(0.5)
    release_loader.set()
    owner.join(1)

    assert not waiter.is_alive()
    assert elapsed and elapsed[0] < 0.2


def _resolve_ignoring_error(resolver):
    try:
        resolver.resolve(_Source(), "https://site/episode")
    except TimeoutError:
        pass


def test_candidate_callbacks_try_next_candidate_after_failure():
    attempts = []

    def broken():
        attempts.append("broken")
        raise RuntimeError("signed-url-secret-cookie")

    def working():
        attempts.append("working")
        return ("https://cdn/working.m3u8", "")

    content = _Content([broken, working])
    resolver = StreamResolver(content, max_candidates=2)

    assert resolver.resolve(_Source(), "https://site/episode") == ("https://cdn/working.m3u8", "")
    assert attempts == ["broken", "working"]


def test_candidate_callback_cancellation_is_raised_without_trying_next_candidate():
    attempts = []

    def cancelled(context=None):
        attempts.append("cancelled")
        context.cancel()
        raise StreamCancelledError("cancelled")

    def working():
        attempts.append("working")
        return ("https://cdn/working.m3u8", "")

    content = _Content([cancelled, working])
    resolver = StreamResolver(content, max_candidates=2)

    with pytest.raises(StreamCancelledError):
        resolver.resolve(_Source(), "https://site/episode")
    assert attempts == ["cancelled"]


def test_resolver_does_not_expose_exception_chain_or_traceback_details():
    class _BrokenContent:
        def resolve_video_stream_candidates(self, *args):
            raise RuntimeError("signed-url-secret-cookie")

    resolver = StreamResolver(_BrokenContent())

    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_Source(), "https://site/episode")

    error = exc_info.value
    assert "signed-url-secret-cookie" not in str(error)
    assert "candidate" in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "signed-url-secret-cookie" not in "".join(__import__("traceback").format_exception(error))


def test_expired_resolution_raises_dedicated_cancellation_error_and_is_not_cached():
    from framework.stream_resolver import StreamCancelledError

    content = _Content([("https://cdn/video.m3u8", "")], delay=0.05)
    resolver = StreamResolver(content, total_budget=0.001)

    with pytest.raises(StreamCancelledError):
        resolver.resolve(_Source(), "https://site/episode", deadline=time.monotonic() - 1)

    assert resolver._cache == {}


def test_same_key_waiter_has_deadline_without_starting_duplicate_resolution():
    content = _Content([("https://cdn/video.m3u8", "")], delay=0.2)
    resolver = StreamResolver(content, total_budget=0.05)
    results = []

    def run():
        try:
            results.append(("ok", resolver.resolve(_Source(), "https://site/episode")))
        except (TimeoutError, RuntimeError):
            results.append(("error", None))

    owner = threading.Thread(target=run)
    owner.start()
    time.sleep(0.01)
    waiter = threading.Thread(target=run)
    waiter.start()
    waiter.join(0.15)

    assert not waiter.is_alive()
    assert content.calls == 1
    owner.join()
    assert len(results) == 2
    assert all(result in (("error", None), ("ok", ("", ""))) for result in results)


def test_cancelled_budget_discards_late_result_without_caching():
    started = threading.Event()
    release = threading.Event()
    calls = []

    class _BlockingContent:
        def resolve_video_stream_candidates(self, *args, context=None):
            calls.append(context)
            started.set()
            release.wait()
            return [("https://cdn/late.m3u8", "")]

    resolver = StreamResolver(_BlockingContent(), total_budget=0.05)
    first = threading.Thread(
        target=lambda: _resolve_ignoring_error(resolver)
    )
    first.start()
    assert started.wait(1)
    first.join(0.5)
    assert not first.is_alive()

    release.set()
    deadline = time.monotonic() + 1
    while resolver._inflight and time.monotonic() < deadline:
        time.sleep(0.005)

    assert resolver._cache == {}
    assert resolver.resolve(_Source(), "https://site/episode") == (
        "https://cdn/late.m3u8",
        "",
    )
    assert len(calls) == 2
    assert StreamBudget is not None
    assert all(isinstance(context, StreamBudget) for context in calls)


def test_same_key_cancelled_request_is_reused_while_worker_is_alive():
    started = threading.Event()
    release = threading.Event()
    calls = []

    class _BlockingContent:
        def resolve_video_stream_candidates(self, *args, context=None):
            calls.append(context)
            started.set()
            release.wait()
            return [("https://cdn/video.m3u8", "")]

    resolver = StreamResolver(_BlockingContent(), total_budget=0.05)
    owner = threading.Thread(
        target=lambda: resolver.resolve(_Source(), "https://site/episode")
    )
    owner.start()
    assert started.wait(1)

    waiter = threading.Thread(
        target=lambda: resolver.resolve(
            _Source(), "https://site/episode", deadline=time.monotonic() + 0.2
        )
    )
    waiter.start()
    time.sleep(0.02)
    assert len(calls) == 1

    release.set()
    owner.join(1)
    waiter.join(1)
    assert len(calls) == 1


def test_success_cache_expires_after_short_ttl(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("framework.stream_resolver.time.monotonic", lambda: now[0])
    content = _Content([("https://cdn/video.m3u8", "")])
    resolver = StreamResolver(content, cache_ttl=5)

    assert resolver.resolve(_Source(), "https://site/episode") == ("https://cdn/video.m3u8", "")
    now[0] = 104.9
    assert resolver.resolve(_Source(), "https://site/episode") == ("https://cdn/video.m3u8", "")
    now[0] = 105.1
    assert resolver.resolve(_Source(), "https://site/episode") == ("https://cdn/video.m3u8", "")
    assert content.calls == 2


def test_signed_url_is_never_cached(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("framework.stream_resolver.time.monotonic", lambda: now[0])
    monkeypatch.setattr("framework.stream_resolver.time.time", lambda: 1700000000 + now[0])
    content = _Content([("https://cdn/video.m3u8?expire=1700000103&sig=secret", "")])
    resolver = StreamResolver(content, cache_ttl=60)

    assert resolver.resolve(_Source(), "https://site/episode") == (
        "https://cdn/video.m3u8?expire=1700000103&sig=secret",
        "",
    )
    now[0] = 102.0
    assert resolver.resolve(_Source(), "https://site/episode") == (
        "https://cdn/video.m3u8?expire=1700000103&sig=secret",
        "",
    )
    assert content.calls == 2


@pytest.mark.parametrize("change", ["source", "url", "quality", "merged"])
def test_cache_isolates_each_request_dimension(change):
    from types import SimpleNamespace

    content = _Content([("https://cdn/first.mp4", "")])
    resolver = StreamResolver(content)
    args = [SimpleNamespace(source_id="one"), "https://site/episode", "best", True]
    resolver.resolve(*args)
    content.candidates = [("https://cdn/second.mp4", "")]
    replacements = {
        "source": (0, SimpleNamespace(source_id="two")),
        "url": (1, "https://site/other"),
        "quality": (2, "720p"),
        "merged": (3, False),
    }
    index, value = replacements[change]
    args[index] = value
    assert resolver.resolve(*args) == ("https://cdn/second.mp4", "")
    assert content.calls == 2


@pytest.mark.parametrize("expire", ["1700000000", "1699999999"])
def test_expired_video_or_audio_is_not_cached(monkeypatch, expire):
    monkeypatch.setattr("framework.stream_resolver.time.time", lambda: 1700000000)
    content = _Content([("https://cdn/video.mp4", f"https://cdn/audio.m4a?expire={expire}")])
    resolver = StreamResolver(content)
    resolver.resolve(_Source(), "https://site/episode")
    resolver.resolve(_Source(), "https://site/episode")
    assert content.calls == 2


def test_invalidation_discards_late_result_and_allows_fresh_resolution():
    started = threading.Event()
    release = threading.Event()
    errors = []

    def slow(context=None):
        started.set()
        assert release.wait(2)
        return "https://cdn/old.mp4", ""

    content = _Content([slow])
    resolver = StreamResolver(content)

    def resolve():
        try:
            resolver.resolve(_Source(), "https://site/episode")
        except StreamCancelledError as error:
            errors.append(error)

    owner = threading.Thread(target=resolve)
    owner.start()
    try:
        assert started.wait(1)
        resolver.invalidate(_Source(), "https://site/episode")
    finally:
        release.set()
        owner.join(2)
    assert not owner.is_alive()
    assert errors
    assert resolver._cache == {}
    deadline = time.monotonic() + 1
    while resolver._inflight and time.monotonic() < deadline:
        time.sleep(0.005)
    assert resolver._inflight == {}
    content.candidates = [("https://cdn/fresh.mp4", "")]
    assert resolver.resolve(_Source(), "https://site/episode") == ("https://cdn/fresh.mp4", "")


def test_invalidation_is_scoped_to_source_and_url():
    from types import SimpleNamespace

    content = _Content([("https://cdn/old.mp4", "")])
    resolver = StreamResolver(content)
    source = SimpleNamespace(source_id="other")
    resolver.resolve(_Source(), "https://site/episode")
    resolver.resolve(source, "https://site/episode")
    resolver.resolve(_Source(), "https://site/other")
    resolver.invalidate(_Source(), "https://site/episode")
    content.candidates = [("https://cdn/fresh.mp4", "")]
    assert resolver.resolve(_Source(), "https://site/episode") == ("https://cdn/fresh.mp4", "")
    assert resolver.resolve(source, "https://site/episode") == ("https://cdn/old.mp4", "")
    assert resolver.resolve(_Source(), "https://site/other") == ("https://cdn/old.mp4", "")


# ---------------------------------------------------------------------- #
# D4：源级流解析选项（signed_query_names / cache_ttl）
# ---------------------------------------------------------------------- #
def test_instance_signed_query_names_merge_with_builtin():
    resolver = StreamResolver(_Content(), cache_ttl=30, signed_query_names=["x-sign", "X-TOKEN"])
    assert "x-sign" in resolver._signed_query_names
    assert "x-token" in resolver._signed_query_names  # 大写下沉
    assert "token" in resolver._signed_query_names  # 内置集保留（含相邻键）
    assert "expire" in resolver._signed_query_names


def test_resolve_per_call_cache_ttl_is_used_for_cache_write(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("framework.stream_resolver.time.monotonic", lambda: now[0])
    content = _Content([("https://cdn/video.m3u8", "")])
    resolver = StreamResolver(content, cache_ttl=60)
    assert resolver.resolve(_Source(), "https://site/episode", cache_ttl=2) == (
        "https://cdn/video.m3u8",
        "",
    )
    key = ("demo", "https://site/episode", "", True)
    expires_at = resolver._cache[key][0]
    assert expires_at == pytest.approx(102.0)
    now[0] = 102.1
    assert resolver.resolve(_Source(), "https://site/episode") == ("https://cdn/video.m3u8", "")
    assert content.calls == 2  # 短路 TTL 已过 → 重新取流


def test_resolve_per_call_signed_names_override_defeats_cache(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("framework.stream_resolver.time.monotonic", lambda: now[0])
    content = _Content([("https://cdn/video.m3u8?x-sign=abc", "")])
    resolver = StreamResolver(content, cache_ttl=60)
    assert resolver.resolve(_Source(), "https://site/episode", signed_query_names=["x-sign"]) == (
        "https://cdn/video.m3u8?x-sign=abc",
        "",
    )
    now[0] = 102.0
    assert resolver.resolve(_Source(), "https://site/episode", signed_query_names=["x-sign"]) == (
        "https://cdn/video.m3u8?x-sign=abc",
        "",
    )
    assert content.calls == 2  # 命中签名参数 → 永不可缓存


def test_stream_resolver_options_reads_source_raw():
    from framework.stream_resolver import stream_resolver_options

    source = type("Source", (), {"raw": {"stream_resolver": {
        "signed_query_names": ["x-sign"],
        "cache_ttl": "12.5",
    }}})()
    assert stream_resolver_options(source) == {
        "signed_query_names": ["x-sign"],
        "cache_ttl": 12.5,
    }
    # 显式入参优先
    assert stream_resolver_options(source, signed_query_names=["other"])["signed_query_names"] == ["other"]
    assert stream_resolver_options(source, cache_ttl=3)["cache_ttl"] == 3.0
    # 非法 cache_ttl 省略（走 resolver 默认）
    assert "cache_ttl" not in stream_resolver_options(source, cache_ttl="abc")
    # 未配置 → 空 dict
    assert stream_resolver_options(type("S", (), {"raw": {}})()) == {}


# ---------------------------------------------------------------------- #
# D4：实例级默认从 settings 读取 + 非法输入安全回退
# ---------------------------------------------------------------------- #
def test_stream_resolver_defaults_from_settings_manager(tmp_path):
    from framework.settings_manager import SettingsManager

    settings = SettingsManager(tmp_path / "settings.json")
    settings.set("sources_runtime", "stream_cache_ttl_seconds", 12.5)
    settings.set("sources_runtime", "stream_signed_query_names", ["X-Sign", "appsig"])
    from framework.stream_resolver import stream_resolver_defaults

    opts = stream_resolver_defaults(settings)
    assert opts["cache_ttl"] == 12.5
    assert list(opts["signed_query_names"]) == ["X-Sign", "appsig"]


def test_stream_resolver_defaults_from_dict_and_missing_falls_back():
    from framework.stream_resolver import stream_resolver_defaults

    # 未配置键 → 不返回任何覆盖（走 StreamResolver 内置硬编码默认）
    assert stream_resolver_defaults({"sources_runtime": {}}) == {}
    assert stream_resolver_defaults(None) == {}
    assert stream_resolver_defaults("bogus") == {}
    # 非法类型 → 回退默认（不返回键）
    assert stream_resolver_defaults(
        {"sources_runtime": {"stream_cache_ttl_seconds": "abc"}}
    ) == {}


def test_resolver_init_bad_ttl_and_bad_names_fall_back():
    # 非法 TTL / 不可迭代 names：回退内置默认，不抛异常（安全默认）
    r = StreamResolver(_Content(), cache_ttl="not-a-number", signed_query_names=12345)
    assert r._cache_ttl == StreamResolver.DEFAULT_CACHE_TTL
    assert "signature" in r._signed_query_names
    # None names → 仍保留内置签名集
    r2 = StreamResolver(_Content(), cache_ttl=None)
    assert r2._cache_ttl == StreamResolver.DEFAULT_CACHE_TTL
    assert "token" in r2._signed_query_names
