# -*- coding: utf-8 -*-
"""HTTP 405 反爬分类测试（test_http_anti405.py）。

背景：17k 等站用 HTTP 405（而非 403/429）作为限流/封禁信号；短时间并发请求
后返回 405，之后所有请求失败 → 下载不完全。框架应把 405 归为反爬状态，使
请求抛 AntiScrapeError → 触发代理池换 IP 重试（而非当作确定性 4xx 直接失败）。

端到端用 localhost 真实 HTTP 服务器。
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from framework.http import (  # noqa: E402
    HttpClient,
    AntiScrapeError,
    _is_anti_scrape_status,
)


def test_405_is_anti_scrape_status():
    assert _is_anti_scrape_status(405) is True
    assert _is_anti_scrape_status(403) is True
    assert _is_anti_scrape_status(429) is True


class _H405(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"Method Not Allowed"
        self.send_response(405)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: A002
        pass


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _H405)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def test_get_text_405_raises_anti_scrape():
    server = _serve()
    try:
        http = HttpClient(sleeper=lambda s: None)
        http._system_proxy_enabled = False
        with pytest.raises(AntiScrapeError):
            http.get_text(f"http://127.0.0.1:{server.server_port}/x", retries=0)
    finally:
        server.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
