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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import framework.http as http_mod  # noqa: E402
from framework.http import HttpClient, NetworkDefaults  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_proxy_cache(monkeypatch):
    """每个用例前清空系统代理 TTL 缓存与僵尸代理记忆，避免用例间相互污染。"""
    http_mod._SYSTEM_PROXY_CACHE["t"] = 0.0
    http_mod._SYSTEM_PROXY_CACHE["url"] = None
    http_mod._reset_proxy_failures()
    yield
    http_mod._SYSTEM_PROXY_CACHE["t"] = 0.0
    http_mod._SYSTEM_PROXY_CACHE["url"] = None
    http_mod._reset_proxy_failures()


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


# ---------------------------------------------------------------------- #
# 僵尸代理：端口活着但转发不出去（加速器开着但上游断了）
# 现象：探活通过 → 全部请求交给它 → 每个都读超时（实测 44s）→ 界面空白、
# 播放器拿不到流。对策：走它失败立刻改直连，并记住这台主机。
# ---------------------------------------------------------------------- #
_SYSTEM_PROXY = "http://127.0.0.1:65532"


def _zombie_client(monkeypatch):
    """构造"系统代理=僵尸代理"的客户端（探测恒返回该系统代理）。"""
    monkeypatch.setattr(http_mod, "_live_system_proxy", lambda: _SYSTEM_PROXY)
    return HttpClient(sleeper=lambda s: None)


def test_system_proxy_failure_falls_back_to_direct(monkeypatch):
    """僵尸代理：一次短超时探测后立刻改直连，并记得这台主机。"""
    attempts = []
    http = _zombie_client(monkeypatch)

    def fake_get_once(url, headers, proxy, timeout, encoding=None):
        attempts.append((proxy, timeout))
        if proxy:
            raise OSError("read timed out")
        return "DIRECT-OK"

    monkeypatch.setattr(http, "_get_once", fake_get_once)
    assert http.get_text("http://site.test/a", retries=3, interval_ms=0) == "DIRECT-OK"

    assert attempts[0][0] == _SYSTEM_PROXY
    assert attempts[0][1] <= http_mod._PROXY_PROBE_TIMEOUT, "僵尸代理只能用短超时探一次"
    assert attempts[-1][0] is None, "失败后必须直连"
    assert http_mod._proxy_failed_for("site.test") is True


def test_proxy_failed_host_skips_proxy_entirely(monkeypatch):
    """已知走代理不通的主机：第二次请求连探测都不再做，直接直连。"""
    attempts = []
    http = _zombie_client(monkeypatch)

    def fake_get_once(url, headers, proxy, timeout, encoding=None):
        attempts.append(proxy)
        if proxy:
            raise OSError("read timed out")
        return "DIRECT-OK"

    monkeypatch.setattr(http, "_get_once", fake_get_once)
    assert http.get_text("http://site.test/a", retries=0, interval_ms=0) == "DIRECT-OK"
    attempts.clear()
    assert http.get_text("http://site.test/b", retries=0, interval_ms=0) == "DIRECT-OK"
    assert attempts == [None], "同主机第二次请求不该再碰系统代理"


def test_two_host_failures_suspend_system_proxy(monkeypatch):
    """不同主机连续失败到阈值 → 系统代理整体停用（到期自动恢复）。"""
    monkeypatch.setattr(
        http_mod, "_system_proxy_candidates", lambda: [_SYSTEM_PROXY]
    )
    assert http_mod._live_system_proxy() == _SYSTEM_PROXY
    http_mod._note_system_proxy_failure("a.test")
    assert http_mod._live_system_proxy() == _SYSTEM_PROXY, "单主机失败还不该整体停用"
    http_mod._note_system_proxy_failure("b.test")
    assert http_mod._system_proxy_suspended() is True
    assert http_mod._live_system_proxy() is None, "停用期内必须直连"


def test_suspension_expires(monkeypatch):
    """停用窗口到期 → 清空记忆，系统代理重新可用（VPN 修好后自动回来）。"""
    monkeypatch.setattr(
        http_mod, "_system_proxy_candidates", lambda: [_SYSTEM_PROXY]
    )
    http_mod._note_system_proxy_failure("a.test")
    http_mod._note_system_proxy_failure("b.test")
    assert http_mod._system_proxy_suspended() is True
    http_mod._PROXY_FAIL["until"] = time.monotonic() - 1
    assert http_mod._system_proxy_suspended() is False
    assert http_mod._PROXY_FAIL["hosts"] == set()
    assert http_mod._live_system_proxy() == _SYSTEM_PROXY


def test_explicit_proxy_is_never_bypassed(monkeypatch):
    """显式代理是调用方意图：失败就失败，不偷偷改直连。"""
    attempts = []
    http = _zombie_client(monkeypatch)

    def fake_get_once(url, headers, proxy, timeout, encoding=None):
        attempts.append(proxy)
        raise OSError("dead")

    monkeypatch.setattr(http, "_get_once", fake_get_once)
    with pytest.raises(http_mod.RequestError):
        http.get_text("http://site.test/a", proxy="http://p1:1111", retries=0,
                      interval_ms=0)
    assert attempts == ["http://p1:1111"]


def test_global_default_proxy_is_never_bypassed(monkeypatch):
    """全局 network.proxy 同理：配置了就按配置走，不自动直连。"""
    attempts = []
    monkeypatch.setattr(http_mod, "_live_system_proxy", lambda: _SYSTEM_PROXY)
    http = HttpClient(
        sleeper=lambda s: None, defaults=NetworkDefaults(proxy="http://global:3128")
    )

    def fake_get_once(url, headers, proxy, timeout, encoding=None):
        attempts.append(proxy)
        raise OSError("dead")

    monkeypatch.setattr(http, "_get_once", fake_get_once)
    with pytest.raises(http_mod.RequestError):
        http.get_text("http://site.test/a", retries=0, interval_ms=0)
    assert attempts == ["http://global:3128"]


def test_direct_fallback_retries_proxy_when_direct_also_fails(monkeypatch):
    """直连也不通：可能确实需要代理（只是慢），用完整预算再走一次代理。"""
    attempts = []
    http = _zombie_client(monkeypatch)

    def fake_get_once(url, headers, proxy, timeout, encoding=None):
        attempts.append((proxy, timeout))
        if proxy is None:
            raise OSError("direct blocked")
        if len([a for a in attempts if a[0] == _SYSTEM_PROXY]) == 1:  # 首次短超时探测
            raise OSError("slow proxy")
        return "VIA-PROXY"

    monkeypatch.setattr(http, "_get_once", fake_get_once)
    assert http.get_text("http://vpn-only.test/a", retries=0, interval_ms=0) == "VIA-PROXY"
    assert [flag for flag, _t in attempts] == [_SYSTEM_PROXY, None, _SYSTEM_PROXY]
    assert attempts[0][1] == http_mod._PROXY_PROBE_TIMEOUT
    assert attempts[2][1] == http.defaults.timeout, "兜底那次要用完整超时"
    # 直连也不通 → 不是僵尸代理的证据，不能记仇（否则真需要 VPN 的站会被长期判死）
    assert http_mod._proxy_failed_for("vpn-only.test") is False
    # 反而要记住「这个站只有代理能通」，下次别再白试直连
    assert http_mod._proxy_preferred("vpn-only.test") is True


def test_proxy_preferred_host_skips_probe_and_direct(monkeypatch):
    """已知只有代理能通的站：一次（完整预算的）代理请求就拿到结果。

    实测背景：favcomic / xvideos 直连 18s 连接被重置，走代理 1.6~2.3s 正常。
    没有偏好记忆时每次都要先 5s 探测 + 一次必然失败的直连。
    """
    attempts = []
    http = _zombie_client(monkeypatch)

    def fake_get_once(url, headers, proxy, timeout, encoding=None):
        attempts.append((proxy, timeout))
        return "VIA-PROXY"

    monkeypatch.setattr(http, "_get_once", fake_get_once)
    http_mod._note_proxy_success("only-proxy.test")
    assert http.get_text("http://only-proxy.test/a", retries=0, interval_ms=0) == "VIA-PROXY"
    assert len(attempts) == 1, attempts
    assert attempts[0][0] == _SYSTEM_PROXY
    assert attempts[0][1] == http.defaults.timeout, "偏好路径要用完整预算，不再短超时"


def test_proxy_preference_expires(monkeypatch):
    """偏好会过期：到期后回到「短探测 → 直连」的常规流程。"""
    attempts = []
    http = _zombie_client(monkeypatch)

    def fake_get_once(url, headers, proxy, timeout, encoding=None):
        attempts.append((proxy, timeout))
        if proxy and timeout == http_mod._PROXY_PROBE_TIMEOUT:
            raise OSError("read timed out")
        return "OK"

    monkeypatch.setattr(http, "_get_once", fake_get_once)
    http_mod._note_proxy_success("only-proxy.test")
    with http_mod._SYSTEM_PROXY_LOCK:
        http_mod._PROXY_OK["only-proxy.test"] = time.monotonic() - 1.0
    assert http.get_text("http://only-proxy.test/a", retries=0, interval_ms=0) == "OK"
    assert attempts[0][1] == http_mod._PROXY_PROBE_TIMEOUT, "过期后先短探测"


def test_antiscrape_still_propagates_from_proxy_attempt(monkeypatch):
    """反爬语义不能被"改直连"抹掉：代理那次的 AntiScrapeError 原样上抛。"""
    http = _zombie_client(monkeypatch)

    def fake_get_once(url, headers, proxy, timeout, encoding=None):
        raise http_mod.AntiScrapeError("反爬响应 HTTP 403")

    monkeypatch.setattr(http, "_get_once", fake_get_once)
    with pytest.raises(http_mod.AntiScrapeError):
        http.get_text("http://site.test/a", retries=0, interval_ms=0)


def test_post_text_also_falls_back_to_direct(monkeypatch):
    """POST 走同一套兜底（搜索/解密请求也常在僵尸代理下卡死）。"""
    calls = []
    http = _zombie_client(monkeypatch)

    class _Resp:
        status_code = 200
        text = "{}"
        headers = {"Content-Type": "application/json"}

        def raise_for_status(self):
            return None

    def fake_post(url, **kwargs):
        calls.append(kwargs.get("proxies"))
        if kwargs.get("proxies"):
            raise OSError("read timed out")
        return _Resp()

    monkeypatch.setattr(http._session, "post", fake_post)
    assert http.post_text("http://site.test/api", json_body={}, retries=0) == "{}"
    assert calls[0] is not None and calls[-1] is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
