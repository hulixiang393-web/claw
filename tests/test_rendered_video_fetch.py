# -*- coding: utf-8 -*-
"""fetch_rendered_video_sync：extra_delay 死等 → 轮询提前返回（只快不慢）。"""
import framework.playwright_helper as pw


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


class _FakePage:
    """wait_for_timeout 推进「时间」；on_after_wait 钩子模拟 JS 延迟注入。"""

    def __init__(self, videos=None, hits_after=None):
        self.frames = [_FakeFrame(videos or [])]
        self.waited = 0
        self._n_wait = 0
        self._req_cb = None
        # hits_after: [(第几次 wait 之后, 触发的媒体请求 URL), ...]
        self._hits_after = hits_after or []
        # src_after: [(第几次 wait 之后, 给 video 换上的 src), ...]
        self._src_after = []

    def on(self, event, cb):
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


def _run(page, extra_delay_ms=2500):
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
            timeout_ms=5000, extra_delay_ms=extra_delay_ms)
    finally:
        pw._sync_browser = pw_orig
        pw._sync_page = pg_orig


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


def test_returns_empty_when_nothing_found():
    """全程无源 → ""（与旧实现一致；总等待 = extra_delay + 3 轮重试）。"""
    page = _FakePage(videos=[_FakeVideo("")])
    assert _run(page, extra_delay_ms=500) == ""
    assert page.waited >= 500 + 12000
