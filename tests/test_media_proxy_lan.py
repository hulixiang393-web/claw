# -*- coding: utf-8 -*-
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
import requests

import framework.media_proxy as mp
from framework.media_cache import MediaCache
from framework.media_proxy import MediaProxy


class _FakeUpstream(BaseHTTPRequestHandler):
    hits = {}

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        type(self).hits[path] = type(self).hits.get(path, 0) + 1
        if path == "/hls/index.m3u8":
            body = (
                '#EXTM3U\n'
                '#EXT-X-KEY:METHOD=AES-128,URI="/hls/key.bin"\n'
                '#EXTINF:1,\n/hls/seg.ts\n'
                '#EXTINF:1,\nnested.m3u8\n'
                '#EXT-X-ENDLIST\n'
            ).encode()
            self._send(body, "application/vnd.apple.mpegurl")
            return
        if path == "/hls/nested.m3u8":
            body = b"#EXTM3U\n#EXTINF:1,\n/hls/nested-seg.ts\n#EXT-X-ENDLIST\n"
            self._send(body, "application/vnd.apple.mpegurl")
            return
        if path in ("/hls/seg.ts", "/hls/nested-seg.ts"):
            self._send(path.encode(), "video/mp2t")
            return
        if path == "/hls/key.bin":
            self._send(b"key", "application/octet-stream")
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def upstream():
    _FakeUpstream.hits = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _upstream_url(server, path):
    return f"http://127.0.0.1:{server.server_port}{path}"


def _http_urls(text):
    return re.findall(r'http://[^"\n]+', text)


def _urls_with_path(text, path):
    return [url for url in _http_urls(text) if path in url]


class _OffCache:
    enabled = False


@pytest.fixture
def proxy_ctx():
    proxy = MediaProxy(cache=_OffCache())
    yield proxy
    proxy.stop_lan_endpoint()
    proxy.stop()
    proxy._leases.clear()


def _token(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def test_start_lan_endpoint_rejects_wildcard_hosts(proxy_ctx):
    for bind_host in ("", "0.0.0.0", "::", "0:0:0:0:0:0:0:0"):
        with pytest.raises(ValueError, match="bind_host"):
            proxy_ctx.start_lan_endpoint(bind_host)


def test_public_base_must_be_active_lan_base(proxy_ctx):
    with pytest.raises(ValueError, match="public_base"):
        proxy_ctx.build_url("https://cdn.example/video.m3u8", public_base="http://127.0.0.1:1")
    lan_base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    proxy_ctx.stop_lan_endpoint()
    with pytest.raises(ValueError, match="public_base"):
        proxy_ctx.build_url("https://cdn.example/video.m3u8", public_base=lan_base)


def test_local_m3u8_tokens_survive_lan_stop(upstream, proxy_ctx):
    target = _upstream_url(upstream, "/hls/index.m3u8")
    local_manifest_url = proxy_ctx.build_url(target, {"Referer": "r"})
    proxy_ctx.start_lan_endpoint("127.0.0.1")
    manifest = requests.get(local_manifest_url, timeout=10)
    local_tokens = [url for url in _http_urls(manifest.text) if "/s/" in url]
    assert local_tokens
    proxy_ctx.stop_lan_endpoint()
    assert all(requests.get(url, timeout=10).status_code == 200 for url in local_tokens)
    assert all(_token(url) not in proxy_ctx._lan_tokens for url in local_tokens)


def test_rewrite_rejects_foreign_base_without_token_leak(proxy_ctx):
    proxy_ctx.start_lan_endpoint("127.0.0.1")
    before_tokens = set(proxy_ctx._tokens)
    before_lan_tokens = set(proxy_ctx._lan_tokens)
    with pytest.raises(ValueError, match="public_base"):
        proxy_ctx._rewrite_m3u8(
            "#EXTM3U\nsegment.ts",
            "https://cdn.example/index.m3u8",
            {},
            public_base="http://127.0.0.1:1",
        )
    assert set(proxy_ctx._tokens) == before_tokens
    assert set(proxy_ctx._lan_tokens) == before_lan_tokens


def test_rewrite_rejects_stale_base_without_token_leak(proxy_ctx):
    stale_base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    proxy_ctx.stop_lan_endpoint()
    before_tokens = set(proxy_ctx._tokens)
    before_lan_tokens = set(proxy_ctx._lan_tokens)
    with pytest.raises(ValueError, match="public_base"):
        proxy_ctx._rewrite_m3u8(
            "#EXTM3U\nsegment.ts",
            "https://cdn.example/index.m3u8",
            {},
            public_base=stale_base,
        )
    assert set(proxy_ctx._tokens) == before_tokens
    assert set(proxy_ctx._lan_tokens) == before_lan_tokens


def test_start_lan_endpoint_does_not_reset_prefetch_circuit_breaker(proxy_ctx):
    proxy_ctx._pf_disabled = True
    proxy_ctx._pf_failures = 3
    proxy_ctx._pf_cancel.set()
    proxy_ctx.start_lan_endpoint("127.0.0.1")
    assert proxy_ctx._pf_disabled is True
    assert proxy_ctx._pf_failures == 3
    assert proxy_ctx._pf_cancel.is_set()


def test_lan_http_s_and_c_and_nested_manifest_use_requesting_endpoint(upstream, tmp_path):
    cache = MediaCache(root=tmp_path / "cache", max_videos=3, max_bytes=32 * 1024 ** 2)
    proxy = MediaProxy(cache=cache)
    try:
        target = _upstream_url(upstream, "/hls/index.m3u8")
        local_manifest_url = proxy.build_url(target, {"Referer": "r"})
        lan_base = proxy.start_lan_endpoint("127.0.0.1")
        lan_manifest_url = proxy.build_url(
            target, {"Referer": "r"}, public_base=lan_base)
        local_base = local_manifest_url.rsplit("/s/", 1)[0]

        local_manifest = requests.get(local_manifest_url, timeout=10)
        lan_manifest = requests.get(lan_manifest_url, timeout=10)
        assert local_manifest.status_code == 200
        assert lan_manifest.status_code == 200

        local_segments = _urls_with_path(local_manifest.text, "/c/")
        lan_segments = _urls_with_path(lan_manifest.text, "/c/")
        assert local_segments and lan_segments
        assert all(url.startswith(local_base + "/c/") for url in local_segments)
        assert all(url.startswith(lan_base + "/c/") for url in lan_segments)
        assert all(urlsplit(url).port == urlsplit(local_base).port for url in local_segments)
        assert all(urlsplit(url).port == urlsplit(lan_base).port for url in lan_segments)

        local_nested = _urls_with_path(local_manifest.text, "/nested.m3u8")
        lan_nested = _urls_with_path(lan_manifest.text, "/nested.m3u8")
        assert local_nested and lan_nested
        assert requests.get(local_segments[0], timeout=10).status_code == 200
        assert requests.get(lan_segments[0], timeout=10).status_code == 200
        local_nested_manifest = requests.get(local_nested[0], timeout=10)
        lan_nested_manifest = requests.get(lan_nested[0], timeout=10)
        assert local_nested_manifest.status_code == 200
        assert lan_nested_manifest.status_code == 200
        assert all(url.startswith(local_base + "/c/") for url in _http_urls(local_nested_manifest.text))
        assert all(url.startswith(lan_base + "/c/") for url in _http_urls(lan_nested_manifest.text))

        proxy.stop_lan_endpoint()
        local_nested_after_stop = requests.get(local_nested[0], timeout=10)
        assert local_nested_after_stop.status_code == 200
        assert all(url.startswith(local_base + "/c/") for url in _http_urls(local_nested_after_stop.text))
    finally:
        proxy.stop_lan_endpoint()
        proxy.stop()


def test_lan_http_series_memo_isolated_by_endpoint(proxy_ctx):
    calls = []

    def resolve(idx):
        calls.append(idx)
        return "https://cdn.example/video.m3u8", "", {"Referer": "r"}, None

    proxy_ctx._ensure_server()
    lan_base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    key = proxy_ctx.register_series(resolve, count=1)
    local_url = proxy_ctx.series_episode_url(key, 0)
    lan_url = proxy_ctx.series_episode_url(key, 0, public_base=lan_base)
    local_response = requests.get(local_url, allow_redirects=False, timeout=10)
    lan_response = requests.get(lan_url, allow_redirects=False, timeout=10)
    assert local_response.status_code == 302
    assert lan_response.status_code == 302
    assert local_response.headers["Location"].startswith(local_url.rsplit("/e/", 1)[0] + "/s/")
    assert lan_response.headers["Location"].startswith(lan_base + "/s/")
    assert local_response.headers["Location"] != lan_response.headers["Location"]
    assert calls == [0, 0]


def test_build_url_defaults_to_localhost(proxy_ctx):
    url = proxy_ctx.build_url("https://cdn.example/video.m3u8", {"Referer": "r"})
    parts = urlsplit(url)
    assert parts.scheme == "http"
    assert parts.hostname == "127.0.0.1"
    assert parts.port
    assert parts.path.startswith("/s/")


def test_lan_thread_start_failure_rolls_back(monkeypatch, proxy_ctx):
    real_server = mp.ThreadingHTTPServer
    closed = []

    class _TrackingServer(real_server):
        def server_close(self):
            closed.append(True)
            return super().server_close()

    class _FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(mp, "ThreadingHTTPServer", _TrackingServer)
    monkeypatch.setattr(mp.threading, "Thread", _FailingThread)
    with pytest.raises(RuntimeError, match="thread start failed"):
        proxy_ctx.start_lan_endpoint("127.0.0.1")
    assert closed == [True]
    assert proxy_ctx._lan_server is None
    assert proxy_ctx._lan_thread is None
    assert proxy_ctx._lan_base is None


def _assert_failed_lan_base_rejected(proxy, base):
    before_tokens = set(proxy._tokens)
    before_lan_tokens = set(proxy._lan_tokens)
    with pytest.raises((ValueError, OSError)):
        proxy.build_url("https://cdn.example/video.m3u8", public_base=base)
    with pytest.raises((ValueError, OSError)):
        proxy._rewrite_m3u8(
            "#EXTM3U\nsegment.ts",
            "https://cdn.example/index.m3u8",
            {},
            public_base=base,
        )
    assert set(proxy._tokens) == before_tokens
    assert set(proxy._lan_tokens) == before_lan_tokens


def test_lan_shutdown_failure_rejects_old_base_without_token_leak(monkeypatch, proxy_ctx):
    base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    server = proxy_ctx._lan_server
    original_shutdown = server.shutdown

    def fail_shutdown():
        raise OSError("shutdown failed")

    monkeypatch.setattr(server, "shutdown", fail_shutdown)
    proxy_ctx.stop_lan_endpoint()
    assert proxy_ctx._lan_server is server
    assert proxy_ctx._lan_base == base
    _assert_failed_lan_base_rejected(proxy_ctx, base)
    with pytest.raises(OSError, match="failed"):
        proxy_ctx.start_lan_endpoint("127.0.0.1")
    monkeypatch.setattr(server, "shutdown", original_shutdown)


def test_lan_server_close_failure_rejects_old_base_without_token_leak(monkeypatch, proxy_ctx):
    base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    server = proxy_ctx._lan_server
    original_close = server.server_close

    def fail_close():
        raise OSError("close failed")

    monkeypatch.setattr(server, "server_close", fail_close)
    proxy_ctx.stop_lan_endpoint()
    assert proxy_ctx._lan_error is not None
    _assert_failed_lan_base_rejected(proxy_ctx, base)
    monkeypatch.setattr(server, "server_close", original_close)
    proxy_ctx.stop_lan_endpoint()


def test_lan_join_failure_rejects_old_base_without_token_leak(proxy_ctx):
    base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    server = proxy_ctx._lan_server
    thread = proxy_ctx._lan_thread
    original_join = thread.join

    def fail_join(*args, **kwargs):
        raise RuntimeError("join failed")

    thread.join = fail_join
    proxy_ctx.stop_lan_endpoint()
    assert proxy_ctx._lan_error is not None
    _assert_failed_lan_base_rejected(proxy_ctx, base)
    thread.join = original_join
    thread.join(timeout=2)
    server.server_close()
    proxy_ctx._lan_server = None
    proxy_ctx._lan_thread = None
    proxy_ctx._lan_base = None
    proxy_ctx._lan_error = None


def test_lan_stale_cleanup_catches_join_exception(monkeypatch, proxy_ctx):
    base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    server = proxy_ctx._lan_server
    thread = proxy_ctx._lan_thread
    original_is_alive = thread.is_alive
    original_join = thread.join
    original_shutdown = server.shutdown
    thread.is_alive = lambda: False
    thread.join = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("join failed"))
    try:
        with pytest.raises(OSError, match="restart"):
            proxy_ctx.start_lan_endpoint("127.0.0.1")
        assert proxy_ctx._lan_error is not None
        _assert_failed_lan_base_rejected(proxy_ctx, base)
    finally:
        thread.is_alive = original_is_alive
        thread.join = original_join
        original_shutdown()
        original_join(timeout=2)
        server.server_close()
        proxy_ctx._lan_server = None
        proxy_ctx._lan_thread = None
        proxy_ctx._lan_base = None
        proxy_ctx._lan_error = None


def test_lan_stale_cleanup_catches_non_oserror(monkeypatch, proxy_ctx):
    base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    server = proxy_ctx._lan_server
    thread = proxy_ctx._lan_thread
    original_is_alive = thread.is_alive
    original_shutdown = server.shutdown
    original_join = thread.join
    thread.is_alive = lambda: False

    def fail_shutdown():
        raise RuntimeError("shutdown failed")

    monkeypatch.setattr(server, "shutdown", fail_shutdown)
    try:
        with pytest.raises(OSError, match="restart"):
            proxy_ctx.start_lan_endpoint("127.0.0.1")
        assert proxy_ctx._lan_error is not None
        _assert_failed_lan_base_rejected(proxy_ctx, base)
    finally:
        thread.is_alive = original_is_alive
        monkeypatch.setattr(server, "shutdown", original_shutdown)
        original_shutdown()
        original_join(timeout=2)
        server.server_close()
        proxy_ctx._lan_server = None
        proxy_ctx._lan_thread = None
        proxy_ctx._lan_base = None
        proxy_ctx._lan_error = None


def test_lan_start_stop_is_serialized(monkeypatch, proxy_ctx):
    real_server = mp.ThreadingHTTPServer
    created = []

    def slow_server(*args, **kwargs):
        time.sleep(0.05)
        server = real_server(*args, **kwargs)
        created.append(server)
        return server

    monkeypatch.setattr(mp, "ThreadingHTTPServer", slow_server)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(proxy_ctx.start_lan_endpoint("127.0.0.1")))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    try:
        assert len(results) == 8
        assert len(set(results)) == 1
        thread = proxy_ctx._lan_thread
        proxy_ctx.stop_lan_endpoint()
        assert proxy_ctx._lan_server is None
        assert thread is not None
        assert not thread.is_alive()
    finally:
        for server in created:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass


def test_start_lan_endpoint_binds_requested_host_and_reuses(proxy_ctx):
    first = proxy_ctx.start_lan_endpoint("127.0.0.1")
    second = proxy_ctx.start_lan_endpoint("127.0.0.1")
    parts = urlsplit(first)
    assert parts.hostname == "127.0.0.1"
    assert parts.port
    assert proxy_ctx._lan_server.server_address[0] == "127.0.0.1"
    assert second == first


def test_lan_build_url_uses_lan_base_and_owns_token(proxy_ctx):
    lan_base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    url = proxy_ctx.build_url(
        "https://cdn.example/video.m3u8",
        {"Referer": "r"},
        public_base=lan_base,
    )
    assert url.startswith(lan_base + "/s/")
    assert _token(url) in proxy_ctx._lan_tokens


def test_stop_lan_endpoint_preserves_local_tokens(proxy_ctx):
    local_url = proxy_ctx.build_url("https://cdn.example/local.m3u8", {"Referer": "r"})
    lan_base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    lan_url = proxy_ctx.build_url(
        "https://cdn.example/lan.m3u8",
        {"Referer": "r"},
        public_base=lan_base,
    )
    local_token = _token(local_url)
    lan_token = _token(lan_url)
    proxy_ctx.stop_lan_endpoint()
    proxy_ctx.stop_lan_endpoint()
    assert local_token in proxy_ctx._tokens
    assert lan_token not in proxy_ctx._tokens
    assert proxy_ctx._lan_tokens == set()
    assert proxy_ctx._server is not None
    assert proxy_ctx._lan_server is None


def test_release_lan_tokens_preserves_local_tokens(proxy_ctx):
    local_url = proxy_ctx.build_url("https://cdn.example/local.m3u8", {"Referer": "r"})
    lan_base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    lan_url = proxy_ctx.build_url(
        "https://cdn.example/lan.m3u8",
        {"Referer": "r"},
        public_base=lan_base,
    )
    proxy_ctx.release_lan_tokens()
    assert _token(local_url) in proxy_ctx._tokens
    assert _token(lan_url) not in proxy_ctx._tokens
    assert proxy_ctx._lan_tokens == set()


def test_lan_m3u8_rewrite_uses_lan_base_for_segments_keys_and_nested_manifests(proxy_ctx):
    lan_base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    local_base = proxy_ctx.build_url("https://cdn.example/local.m3u8", {"Referer": "r"})
    rewritten = proxy_ctx._rewrite_m3u8(
        '\n'.join([
            "#EXTM3U",
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"',
            "segment-1.ts",
            "variant.m3u8",
        ]),
        "https://cdn.example/hls/index.m3u8",
        {"Referer": "r"},
        public_base=lan_base,
    )
    urls = re.findall(r'http://[^"\n]+', rewritten)
    assert len(urls) == 3
    assert all(url.startswith(lan_base + "/s/") for url in urls)
    assert local_base not in rewritten
    assert all(_token(url) in proxy_ctx._lan_tokens for url in urls)


def test_stop_lan_endpoint_is_idempotent_and_local_endpoint_survives(proxy_ctx):
    lan_base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    proxy_ctx.stop_lan_endpoint()
    proxy_ctx.stop_lan_endpoint()
    local_url = proxy_ctx.build_url("https://cdn.example/local.m3u8", {"Referer": "r"})
    assert local_url.startswith("http://127.0.0.1:")
    assert lan_base != local_url.rsplit("/s/", 1)[0]


def test_release_token_removes_target_token_only(proxy_ctx):
    local_url = proxy_ctx.build_url("https://cdn.example/local.m3u8", {"Referer": "r"})
    lan_base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    lan_url = proxy_ctx.build_url(
        "https://cdn.example/lan.m3u8", {"Referer": "r"}, public_base=lan_base
    )

    proxy_ctx.release_token(lan_url)
    proxy_ctx.release_token(lan_url)  # 幂等

    assert _token(lan_url) not in proxy_ctx._tokens
    assert _token(lan_url) not in proxy_ctx._lan_tokens
    assert _token(local_url) in proxy_ctx._tokens

    proxy_ctx.release_token(local_url)

    assert _token(local_url) not in proxy_ctx._tokens


def test_cast_session_stop_failure_keeps_lan_guard(monkeypatch, proxy_ctx):
    # N-1：stop() 只读代理公开错误通道，不得清空失败生命周期标记。
    # LAN 停止失败 → stop() 抛错 → 该状态下 build_url/start_lan_endpoint 仍被拒绝。
    from types import SimpleNamespace

    from framework.cast_session import CastSession

    class _CastClient:
        def set_av_transport_uri(self, uri, title, mime):
            pass

        def play(self):
            pass

        def stop(self):
            pass

    base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    server = proxy_ctx._lan_server
    original_close = server.server_close

    def fail_close():
        raise OSError("close failed")

    monkeypatch.setattr(server, "server_close", fail_close)
    monkeypatch.setattr(
        "framework.cast_session._local_ip_for_renderer", lambda _: "127.0.0.1"
    )
    session = CastSession(proxy_ctx, lambda _: _CastClient())
    renderer = SimpleNamespace(location="http://192.168.1.20:80/root.xml")
    try:
        session.start(
            renderer, 0, "https://cdn.example/video.m3u8", "第一集", {}, None, False
        )
        with pytest.raises(OSError, match="close failed"):
            session.stop()
        with pytest.raises(OSError, match="failed lifecycle"):
            proxy_ctx.build_url(
                "https://cdn.example/video.m3u8", public_base=base
            )
        with pytest.raises(OSError, match="failed lifecycle"):
            proxy_ctx.start_lan_endpoint("127.0.0.1")
    finally:
        monkeypatch.setattr(server, "server_close", original_close)
