# -*- coding: utf-8 -*-
"""fetch_rendered_video_sync：extra_delay 死等 → 轮询提前返回（只快不慢）。"""
import threading
import time

import pytest

import framework.playwright_helper as pw
from framework.stream_resolver import StreamBudget, StreamCancelledError


class _FakeSource:
    def __init__(self, src):
        self._src = src

    def get_attribute(self, name):
        return self._src if name == "src" else None


class _FakeVideo:
    def __init__(self, src=""):
        self._src = src
        self._sources = []

    def get_attribute(self, name):
        return self._src if name == "src" else None

    def query_selector_all(self, sel):
        return self._sources if sel == "source" else []


class _FakeFrame:
    def __init__(self, videos):
        self._videos = videos

    def query_selector_all(self, sel):
        return self._videos if sel == "video" else []


class _FakeRequest:
    def __init__(self, url):
        self.url = url

    def is_navigation_request(self):
        return False

    @property
    def frame(self):
        return None


class _FakeResponse:
    def __init__(self, url, content_type="video/mp4", status=200):
        self.request = _FakeRequest(url)
        self.headers = {"content-type": content_type}
        self.status = status


class _FakePage:
    """wait_for_timeout 推进「时间」；on_after_wait 钩子模拟 JS 延迟注入。"""

    def __init__(self, videos=None, hits_after=None):
        self.frames = [_FakeFrame(videos or [])]
        self.waited = 0
        self._n_wait = 0
        self._req_cb = None
        self._events = {}
        self.main_frame = self.frames[0]
        self.main_frame.url = "about:blank"
        # hits_after: [(第几次 wait 之后, 触发的媒体请求 URL), ...]
        self._hits_after = hits_after or []
        # src_after: [(第几次 wait 之后, 给 video 换上的 src), ...]
        self._src_after = []

    def on(self, event, cb):
        self._events[event] = cb
        if event == "request":
            self._req_cb = cb

    def goto(self, url, timeout=None, wait_until=None):
        pass

    def wait_for_timeout(self, ms):
        self._n_wait += 1
        self.waited += ms
        for n, url in list(self._hits_after):
            if self._n_wait == n and self._req_cb:
                self._hits_after.remove((n, url))
                self._req_cb(_FakeRequest(url))
        for n, src in list(self._src_after):
            if self._n_wait == n:
                self._src_after.remove((n, src))
                self.frames[0]._videos[0]._src = src


def _run(page, extra_delay_ms=2500, slow_fallback_rounds=3, context=None,
         capture_media_responses=False, **options):
    import contextlib

    @contextlib.contextmanager
    def _browser(proxy=None):
        yield None, None

    @contextlib.contextmanager
    def _pg(browser):
        yield page

    pw_orig = pw._sync_browser
    pg_orig = pw._sync_page
    pw._sync_browser = _browser
    pw._sync_page = _pg
    try:
        return pw.fetch_rendered_video_sync(
            "http://x/play", wait_until="domcontentloaded",
            timeout_ms=5000, extra_delay_ms=extra_delay_ms,
            slow_fallback_rounds=slow_fallback_rounds, context=context,
            capture_media_responses=capture_media_responses, **options)
    finally:
        pw._sync_browser = pw_orig
        pw._sync_page = pg_orig


def _navigation_timeout(page, committed=True, status=200, hit=False, context=None):
    from types import SimpleNamespace
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    def goto(url, **kwargs):
        if committed:
            page._events.get("response", lambda response: None)(SimpleNamespace(
                status=status,
                request=SimpleNamespace(is_navigation_request=lambda: True, frame=page.main_frame),
            ))
            page.main_frame.url = url
            page._events.get("framenavigated", lambda frame: None)(page.main_frame)
        if hit:
            page._req_cb(_FakeRequest("https://cdn/ready.m3u8"))
        if context is not None:
            context.cancel()
        raise PlaywrightTimeoutError("navigation timeout")

    page.goto = goto


@pytest.mark.parametrize("options", [{}, {"recover_navigation_timeout": False}, {"recover_navigation_timeout": True}])
@pytest.mark.parametrize("delayed", [False, True])
def test_committed_navigation_timeout_requires_opt_in(delayed, options):
    page = _FakePage(hits_after=[(2, "https://cdn/ready.m3u8")] if delayed else [])
    _navigation_timeout(page, hit=not delayed)
    recovery = options.get("recover_navigation_timeout", False)
    assert _run(page, slow_fallback_rounds=0, **options) == (
        "https://cdn/ready.m3u8" if recovery else ""
    )
    assert page.waited == (500 if delayed and recovery else 0)


@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("committed,status", [(False, 200), (True, None), (True, 404), (True, 503)])
def test_navigation_timeout_without_successful_commit_does_not_scan(committed, status, recovery):
    page = _FakePage(videos=[_FakeVideo("https://cdn/stale.m3u8")])
    _navigation_timeout(page, committed=committed, status=status, hit=True)
    assert _run(page, recover_navigation_timeout=recovery) == ""
    assert page.waited == 0


@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("status", [400, 403, 404, 503])
def test_http_error_response_never_returns_stale_media(recovery, status):
    from types import SimpleNamespace

    page = _FakePage(videos=[_FakeVideo("https://cdn/stale.m3u8")])
    page.goto = lambda *a, **k: SimpleNamespace(status=status)
    assert _run(page, recover_navigation_timeout=recovery) == ""
    assert page.waited == 0


@pytest.mark.parametrize("recovery", [False, True])
def test_real_navigation_error_does_not_recover(recovery):
    from playwright.sync_api import Error

    page = _FakePage(videos=[_FakeVideo("https://cdn/stale.m3u8")])

    def goto(*args, **kwargs):
        raise Error("net::ERR_NAME_NOT_RESOLVED")

    page.goto = goto
    assert _run(page, recover_navigation_timeout=recovery) == ""
    assert page.waited == 0


@pytest.mark.parametrize("recovery", [False, True])
def test_cancelled_committed_navigation_timeout_never_returns_media(recovery):
    page = _FakePage()
    context = StreamBudget(time.monotonic() + 30)
    _navigation_timeout(page, hit=True, context=context)
    with pytest.raises(StreamCancelledError):
        _run(page, context=context, recover_navigation_timeout=recovery)
    assert page.waited == 0


def test_committed_timeout_scan_stays_inside_pipeline_budget(monkeypatch):
    now, page = _clocked_page(monkeypatch)
    _navigation_timeout(page)
    page._hits_after = [(8, "https://cdn/late.m3u8")]
    with pytest.raises(StreamCancelledError):
        _run(page, context=StreamBudget(now[0] + 0.5), recover_navigation_timeout=True)
    assert page.waited <= 500


def test_returns_immediately_when_src_ready():
    """页面加载完 src 已就绪（dm84 实测 delay=0 即可）→ 首扫命中直接返回，
    一次 wait 都不发生——省掉整个 extra_delay 死等（原 2.5s）。"""
    page = _FakePage(videos=[_FakeVideo("https://cdn/v.m3u8")])
    assert _run(page) == "https://cdn/v.m3u8"
    assert page.waited == 0


def test_returns_as_soon_as_src_appears():
    """JS 延迟注入：第 2 次轮询（约 500ms）后出现 → 约 500ms 返回，
    而不是死等满 2500ms。"""
    page = _FakePage(videos=[_FakeVideo("")])
    page._src_after = [(2, "https://cdn/late.m3u8")]
    assert _run(page) == "https://cdn/late.m3u8"
    assert page.waited == 500        # 2 次轮询间隔，远小于 2500 死等


def test_waits_full_extra_delay_then_retry_rounds_when_slow():
    """慢注入站（agedm 类）：轮询等满 extra_delay 仍无 → 维持原 3 轮
    重试语义（轮间 4000ms），第 1 轮重试前注入 → 行为与旧实现一致。"""
    page = _FakePage(videos=[_FakeVideo("")])
    page._src_after = [(12, "https://cdn/agedm.m3u8")]   # 10 次轮询 + 2 次重试 wait 后
    assert _run(page, extra_delay_ms=2500) == "https://cdn/agedm.m3u8"
    assert page.waited >= 2500


def test_media_hit_request_returned_when_video_src_stays_blob():
    """blob 播放器：video src 拿不到，媒体请求出现即返回（轮询段即可命中，
    不必等满 extra_delay 再进重试段）。"""
    page = _FakePage(videos=[_FakeVideo("")],
                     hits_after=[(3, "https://cdn/master.m3u8")])
    assert _run(page) == "https://cdn/master.m3u8"
    assert page.waited == 750        # 第 3 次 wait 后命中


def test_extensionless_video_response_is_returned_when_src_is_blob():
    """AGE CDN 的 MP4 地址没有扩展名，必须按响应类型捕获而不是猜后缀。"""
    page = _FakePage(videos=[_FakeVideo("blob:http://x/player")])

    def goto(url, **kwargs):
        page._events["response"](_FakeResponse("https://cdn.example/video-token"))

    page.goto = goto
    assert _run(page, capture_media_responses=True) == "https://cdn.example/video-token"
    assert page.waited == 0


def test_extensionless_video_response_is_ignored_by_default():
    """其他源保持旧行为：默认不从响应 Content-Type 猜取媒体地址。"""
    page = _FakePage(videos=[_FakeVideo("blob:http://x/player")])

    def goto(url, **kwargs):
        page._events["response"](_FakeResponse("https://cdn.example/video-token"))

    page.goto = goto
    assert _run(page, extra_delay_ms=0, slow_fallback_rounds=0) == ""


def test_can_disable_slow_fallback_for_fast_sources():
    page = _FakePage(videos=[_FakeVideo("")])
    assert _run(page, extra_delay_ms=0, slow_fallback_rounds=0) == ""
    assert page.waited == 0


def test_returns_empty_when_nothing_found():
    """全程无源 → ""（与旧实现一致；总等待 = extra_delay + 3 轮重试）。"""
    page = _FakePage(videos=[_FakeVideo("")])
    assert _run(page, extra_delay_ms=500) == ""
    assert page.waited >= 500 + 12000


def _clocked_page(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(pw.time, "monotonic", lambda: now[0])
    page = _FakePage(videos=[_FakeVideo("")])
    wait = page.wait_for_timeout

    def advance(ms):
        now[0] += ms / 1000
        wait(ms)

    page.wait_for_timeout = advance
    return now, page


@pytest.mark.parametrize("media", [False, True])
def test_slow_player_returns_during_fallback_within_budget(monkeypatch, media):
    now, page = _clocked_page(monkeypatch)
    if media:
        page._hits_after = [(8, "https://cdn/slow.m3u8")]
    else:
        page._src_after = [(8, "https://cdn/slow.m3u8")]
    assert _run(page, extra_delay_ms=500, context=StreamBudget(now[0] + 5)) == "https://cdn/slow.m3u8"
    assert page.waited == 2000


def test_navigation_consumes_actual_budget_before_slow_wait(monkeypatch):
    now, page = _clocked_page(monkeypatch)
    navigation = []

    def goto(url, timeout=None, wait_until=None):
        navigation.append(timeout)
        now[0] += 0.2

    page.goto = goto
    with pytest.raises(StreamCancelledError):
        _run(page, extra_delay_ms=0, context=StreamBudget(now[0] + 0.6))
    assert navigation == [pytest.approx(600)]
    assert page.waited == pytest.approx(400)


def test_cancel_during_wait_does_not_enter_another_fallback(monkeypatch):
    now, page = _clocked_page(monkeypatch)
    context = StreamBudget(now[0] + 30)
    wait = page.wait_for_timeout

    def cancel(ms):
        wait(ms)
        context.cancel()

    page.wait_for_timeout = cancel
    with pytest.raises(StreamCancelledError):
        _run(page, extra_delay_ms=0, context=context)
    assert page._n_wait == 1
    assert page.waited <= 250


def test_expired_queued_job_never_runs():
    started = threading.Event()
    release = threading.Event()
    calls = []
    owner = threading.Thread(target=lambda: pw.run_on_render_thread(
        lambda: started.set() or release.wait(2)
    ))
    owner.start()
    try:
        assert started.wait(1)
        with pytest.raises(StreamCancelledError):
            pw.run_on_render_thread(lambda: calls.append("expired"), context=StreamBudget(time.monotonic() + 0.03))
    finally:
        release.set()
        owner.join(2)
    pw.run_on_render_thread(lambda: None)
    assert calls == []


def test_expired_video_never_opens_browser(monkeypatch):
    calls = []
    monkeypatch.setattr(pw, "_sync_browser", lambda *args: calls.append("browser"))
    with pytest.raises(StreamCancelledError):
        pw.fetch_rendered_video_sync.__wrapped__("http://x/play", context=StreamBudget(time.monotonic() - 1))
    assert calls == []


def test_failure_log_uses_stage_and_exception_type_only(monkeypatch, caplog):
    import contextlib

    @contextlib.contextmanager
    def broken_browser(proxy=None):
        raise RuntimeError("signed-url-secret")
        yield None, None

    monkeypatch.setattr(pw, "_sync_browser", broken_browser)
    with caplog.at_level("WARNING", logger="framework.playwright_helper"):
        assert pw.fetch_rendered_video_sync(
            "https://video.example/watch?token=secret", extra_delay_ms=0,
            slow_fallback_rounds=0
        ) == ""

    message = " ".join(record.getMessage() for record in caplog.records)
    assert "阶段=视频渲染" in message
    assert "异常类型=RuntimeError" in message
    assert "video.example" not in message
    assert "signed-url-secret" not in message
