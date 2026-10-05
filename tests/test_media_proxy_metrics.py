from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

import framework.media_proxy as mp
from framework.media_proxy import MediaProxy


_MANIFEST = b"#EXTM3U\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n"
_SEGMENT = b"segment-payload"


class _FakeHlsUpstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.endswith("index.m3u8"):
            body = _MANIFEST
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.endswith("segment.ts"):
            body = _SEGMENT
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(503)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _OffCache:
    enabled = False


def _direct_session():
    session = requests.Session()
    session.trust_env = False
    return session


@pytest.fixture
def hls_proxy(monkeypatch):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHlsUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    monkeypatch.setattr(mp, "_get_direct_session", _direct_session)
    monkeypatch.setattr(mp, "_get_session", _direct_session)
    proxy = MediaProxy(cache=_OffCache())
    proxy._ensure_server()
    base = f"http://127.0.0.1:{upstream.server_address[1]}"
    proxy.clear_transport_metrics()
    try:
        yield proxy, base
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()


def test_transport_metrics_redact_hls_manifest_and_segment(hls_proxy):
    proxy, base = hls_proxy
    manifest_url = proxy.build_url(
        f"{base}/index.m3u8?token=secret-token",
        {
            "Cookie": "session=secret-cookie",
            "Authorization": "Bearer secret-header",
        },
    )
    response = requests.get(manifest_url, timeout=5)
    assert response.status_code == 200
    segment_url = [line for line in response.text.splitlines() if line and not line.startswith("#")][0]
    assert requests.get(segment_url, timeout=5).content == _SEGMENT

    metrics = proxy.transport_metrics()
    assert {metric["kind"] for metric in metrics} >= {"manifest", "segment"}
    for metric in metrics:
        assert set(metric) >= {"kind", "status", "first_byte_ms", "bytes", "duration_ms"}
        assert "http://" not in str(metric)
        assert "secret-token" not in str(metric)
        assert "secret-cookie" not in str(metric)
        assert "secret-header" not in str(metric)
        assert metric["bytes"] >= 0
        assert metric["duration_ms"] >= 0
        if metric["status"] == 200:
            assert metric["first_byte_ms"] is not None


def test_transport_metrics_classify_http_failure_without_sensitive_data(hls_proxy):
    proxy, base = hls_proxy
    response = requests.get(proxy.build_url(f"{base}/missing.ts?token=secret-token"), timeout=5)
    assert response.status_code == 503

    metrics = proxy.transport_metrics()
    assert metrics
    metric = metrics[-1]
    assert metric["kind"] == "segment"
    assert metric["status"] == 503
    assert metric["failure_category"] in {"direct_http", "proxy_http"}
    assert "secret-token" not in str(metric)


def test_transport_metrics_classify_transport_exception(monkeypatch):
    class _FailingSession:
        def get(self, *args, **kwargs):
            raise requests.Timeout("upstream timeout")

    monkeypatch.setattr(mp, "_get_direct_session", lambda: _FailingSession())
    monkeypatch.setattr(mp, "_get_session", lambda: _FailingSession())
    proxy = MediaProxy(cache=_OffCache())
    proxy._ensure_server()
    try:
        response = requests.get(
            proxy.build_url("http://upstream.invalid/segment.ts?token=secret-token"),
            timeout=5,
        )
        assert response.status_code == 502
        metrics = proxy.transport_metrics()
        assert metrics
        assert metrics[-1]["failure_category"] in {"direct_timeout", "proxy_timeout"}
        assert "secret-token" not in str(metrics)
    finally:
        proxy.stop()


def test_transport_metrics_are_bounded_and_clearable(monkeypatch):
    proxy = MediaProxy(cache=_OffCache())
    try:
        monkeypatch.setattr(mp, "_UPSTREAM_DIAGNOSTICS_MAX", 2)
        for index in range(3):
            mp._record_upstream_diagnostic(
                f"http://127.0.0.1/{index}.ts?token=secret-token",
                {"Cookie": "secret-cookie"},
                "direct",
                mp.time.perf_counter(),
            )
        metrics = proxy.transport_metrics()
        assert len(metrics) == 2
        assert all(set(metric) >= {"kind", "status", "first_byte_ms", "bytes", "duration_ms"} for metric in metrics)
        proxy.clear_transport_metrics()
        assert proxy.transport_metrics() == []
    finally:
        proxy.stop()
