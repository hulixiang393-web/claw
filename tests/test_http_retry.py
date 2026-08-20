"""HTTP 重试策略测试（test_http_retry.py）。

覆盖：
- 4xx（404）为确定性失败：不重试，立即抛 RequestError（末页探测提速，避免每页 +3.5s）
- 空响应（200 空 body）：瞬态，走重试，恢复后成功
- 5xx：保持原有重试语义（重试 retries+1 次后失败）

端到端用 localhost 真实 HTTP 服务器。
运行：python -m pytest tests/test_http_retry.py -q
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from framework.http import HttpClient, RequestError  # noqa: E402


def _make_http():
    """测试 HttpClient：禁用系统代理继承，跳过退避 sleep。"""
    http = HttpClient(sleeper=lambda s: None)
    if http._session is not None:
        http._session.trust_env = False
    return http


class _CountHandler(BaseHTTPRequestHandler):
    counter = None

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        self.counter[path] = self.counter.get(path, 0) + 1
        if path == "/ok":
            body, code = b"HELLO", 200
        elif path == "/empty":
            if self.counter[path] == 1:
                body, code = b"", 200  # 首次空响应 → 触发重试
            else:
                body, code = b"RECOVERED", 200
        elif path == "/notfound":
            body, code = b"NOT-FOUND", 404
        else:
            body, code = b"ERR", 500
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: A002
        pass


def _serve(counter):
    _CountHandler.counter = counter
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CountHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


@pytest.fixture
def server():
    srv = _serve({})
    yield srv
    srv.shutdown()


def _url(server, path):
    return f"http://127.0.0.1:{server.server_port}{path}"


def test_4xx_not_retried(server):
    """404 是确定性失败：retries=3 也只请求 1 次，立即抛 RequestError。"""
    http = _make_http()
    with pytest.raises(RequestError):
        http.get_text(_url(server, "/notfound"), retries=3, interval_ms=0)
    assert server.RequestHandlerClass.counter["/notfound"] == 1


def test_empty_body_retried(server):
    """200 空 body 是瞬态：重试后恢复则成功。"""
    http = _make_http()
    text = http.get_text(_url(server, "/empty"), retries=3, interval_ms=0)
    assert text == "RECOVERED"
    assert server.RequestHandlerClass.counter["/empty"] == 2


def test_5xx_still_retried(server):
    """5xx 保持原有重试语义：retries=3 → 共请求 4 次后抛 RequestError。"""
    http = _make_http()
    with pytest.raises(RequestError):
        http.get_text(_url(server, "/err500"), retries=3, interval_ms=0)
    assert server.RequestHandlerClass.counter["/err500"] == 4
