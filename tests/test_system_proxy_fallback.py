"""系统代理健康探测与直连回退测试（test_system_proxy_fallback.py）。

背景：requests 默认 trust_env=True 会无条件走 Windows 注册表/环境变量的系统
代理（如加速器 127.0.0.1:65532）。VPN/加速器关闭后端口已死，但注册表代理项
仍在 → 所有请求连向不存在的代理 → 国内直连源也打不开（"不开 VPN 就打不开"）。

本测试覆盖：
- _parse_proxy_host_port 解析（http/https/socks/非法）
- _live_system_proxy：本地代理死 → None（直连）；本地代理活 → 采用；
  远程代理不探活直接采用；无法解析 → 按历史行为采用
- HttpClient._effective_proxy 优先级：显式 > 全局默认 > 存活系统代理 > None
- 端到端：系统代理视为"死"（探测返回 None）时，localhost 直连正常（国内站恢复）

运行：python -m pytest tests/test_system_proxy_fallback.py -q
"""

from __future__ import annotations

import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import framework.http as http_mod  # noqa: E402
from framework.http import HttpClient, NetworkDefaults  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_proxy_cache(monkeypatch):
    """每个用例前清空系统代理 TTL 缓存，避免用例间相互污染。"""
    http_mod._SYSTEM_PROXY_CACHE["t"] = 0.0
    http_mod._SYSTEM_PROXY_CACHE["url"] = None
    yield
    http_mod._SYSTEM_PROXY_CACHE["t"] = 0.0
    http_mod._SYSTEM_PROXY_CACHE["url"] = None


def _live_local_listener():
    """返回一个真实监听的 (sock, port)，用于模拟存活的本地加速器端口。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


# ---------------------------------------------------------------------- #
# _parse_proxy_host_port
# ---------------------------------------------------------------------- #
def test_parse_proxy_http():
    assert http_mod._parse_proxy_host_port("http://127.0.0.1:65532") == ("127.0.0.1", 65532)


def test_parse_proxy_no_scheme_default_port():
    assert http_mod._parse_proxy_host_port("http://127.0.0.1") == ("127.0.0.1", 80)


def test_parse_proxy_socks():
    assert http_mod._parse_proxy_host_port("socks5://127.0.0.1:7890") == ("127.0.0.1", 7890)


def test_parse_proxy_invalid_returns_none():
    assert http_mod._parse_proxy_host_port("://bad::::") == (None, None)


# ---------------------------------------------------------------------- #
# _live_system_proxy：本地代理死/活、远程代理、无法解析
# ---------------------------------------------------------------------- #
def test_live_proxy_dead_local_returns_none(monkeypatch):
    """本地代理端口已死 → 返回 None（上层直连，国内站恢复打开）。"""
    monkeypatch.setattr(
        http_mod, "_system_proxy_candidates", lambda: ["http://127.0.0.1:1"]
    )
    assert http_mod._live_system_proxy() is None


def test_live_proxy_alive_local_returns_url(monkeypatch):
    """本地代理端口存活（VPN 开着）→ 返回该代理 URL（海外站可用且快）。"""
    sock, port = _live_local_listener()
    try:
        monkeypatch.setattr(
            http_mod, "_system_proxy_candidates", lambda: [f"http://127.0.0.1:{port}"]
        )
        assert http_mod._live_system_proxy() == f"http://127.0.0.1:{port}"
    finally:
        sock.close()


def test_live_proxy_remote_adopted_without_probe(monkeypatch):
    """远程代理（非回环）不探活直接采用：公司内网代理等不会被误杀。"""
    monkeypatch.setattr(
        http_mod, "_system_proxy_candidates", lambda: ["http://proxy.corp:8080"]
    )
    assert http_mod._live_system_proxy() == "http://proxy.corp:8080"


def test_live_proxy_unparseable_adopted(monkeypatch):
    """无法解析 host:port 的候选（非标准 socks 写法）按历史行为采用。"""
    monkeypatch.setattr(
        http_mod, "_system_proxy_candidates", lambda: ["socks5://unresolvable"]
    )
    assert http_mod._live_system_proxy() == "socks5://unresolvable"


def test_live_proxy_dead_local_then_alive_picks_live(monkeypatch):
    """多个候选时跳过死代理、选到存活的那个。"""
    sock, port = _live_local_listener()
    try:
        monkeypatch.setattr(
            http_mod,
            "_system_proxy_candidates",
            lambda: [
                "http://127.0.0.1:1",  # 死代理
                f"http://127.0.0.1:{port}",  # 活代理
            ],
        )
        assert http_mod._live_system_proxy() == f"http://127.0.0.1:{port}"
    finally:
        sock.close()


# ---------------------------------------------------------------------- #
# HttpClient._effective_proxy 优先级
# ---------------------------------------------------------------------- #
def _http(monkeypatch, *, defaults=None, system_url=None, enabled=True):
    """构造 HttpClient 并注入可控的系统代理返回值。"""
    monkeypatch.setattr(
        http_mod, "_live_system_proxy", lambda: system_url
    )
    http = HttpClient(sleeper=lambda s: None, defaults=defaults)
    http._system_proxy_enabled = enabled
    return http


def test_effective_explicit_wins(monkeypatch):
    http = _http(monkeypatch, system_url="http://127.0.0.1:65532")
    assert http._effective_proxy("http://p1:1111") == "http://p1:1111"


def test_effective_global_default_wins(monkeypatch):
    http = _http(
        monkeypatch,
        defaults=NetworkDefaults(proxy="http://global:3128"),
        system_url="http://127.0.0.1:65532",
    )
    assert http._effective_proxy(None) == "http://global:3128"


def test_effective_system_when_enabled(monkeypatch):
    http = _http(monkeypatch, system_url="http://127.0.0.1:65532")
    assert http._effective_proxy(None) == "http://127.0.0.1:65532"


def test_effective_direct_when_system_dead(monkeypatch):
    """系统代理死/无 → None（直连）。"""
    http = _http(monkeypatch, system_url=None)
    assert http._effective_proxy(None) is None


def test_effective_direct_when_disabled(monkeypatch):
    """测试沙箱关闭系统代理后恒直连。"""
    http = _http(monkeypatch, system_url="http://127.0.0.1:65532", enabled=False)
    assert http._effective_proxy(None) is None


# ---------------------------------------------------------------------- #
# 端到端：系统代理死 → localhost 直连正常（国内站恢复）
# ---------------------------------------------------------------------- #
class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"OK-DIRECT"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: A002
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_direct_works_when_system_proxy_dead(monkeypatch, server):
    """系统代理（视为死）时走直连：localhost 端到端成功，等价于国内站不靠 VPN。"""
    http = _http(monkeypatch, system_url=None)  # 探测返回 None → 直连
    url = f"http://127.0.0.1:{server.server_port}/ok"
    assert http.get_text(url, retries=2, interval_ms=0) == "OK-DIRECT"


def test_no_implicit_registry_proxy_in_session(server):
    """生产 session 恒 trust_env=False：不隐式读注册表代理，直连不受本机系统代理干扰。"""
    http = HttpClient(sleeper=lambda s: None)
    http._system_proxy_enabled = False
    assert http._session is not None and http._session.trust_env is False
    url = f"http://127.0.0.1:{server.server_port}/ok"
    assert http.get_text(url, retries=2, interval_ms=0) == "OK-DIRECT"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))