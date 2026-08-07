"""代理池自动启用逻辑测试（test_proxy_pool.py）。

覆盖：
- ProxyPool auto 语义（未启用直连 / engage 后走代理）
- HttpClient 触发反爬（403）→ 自动启用代理池并换 IP 重试成功（端到端）
- 普通源（200 直连）不启用代理；普通错误（404）不启用
- 每源独立 auto 池：只有触发反爬的源启用，其他源不受影响

端到端用 localhost 真实 HTTP 服务器 + 简易 HTTP 正向代理模拟：
  target 服务器：直连（无 X-Via-Proxy 头）→ 403（模拟被反爬拦）；
                  走代理（带 X-Via-Proxy 头）→ 200。
  两个转发代理：一个带标记（→200）、一个不带（→403），验证换 IP。

运行：python tests/test_proxy_pool.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.proxy_pool import (  # noqa: E402
    ProxyPool,
    global_proxy_config,
    reset_global_proxy_config,
)
from framework.http import HttpClient, RequestError  # noqa: E402


def _make_http():
    """测试 HttpClient：禁用系统代理继承。

    生产 HttpClient 默认走系统代理（用户加速器），但测试连 localhost
    端到端服务器，不能被系统代理（如 127.0.0.1:7890）拦截。
    """
    http = HttpClient(sleeper=lambda s: None)
    if http._session is not None:
        http._session.trust_env = False
    return http


# ---------------------------------------------------------------------- #
# localhost 服务器
# ---------------------------------------------------------------------- #
class _TargetHandler(BaseHTTPRequestHandler):
    """模拟站点：直连被反爬拦（403），走代理则放行（200）。

    /ok        直连 200（普通源）
    /blocked   直连 403 / 带 X-Via-Proxy 头 200（反爬源）
    /notfound  恒 404（普通错误）
    """

    def do_GET(self):  # noqa: N802
        via = self.headers.get("X-Via-Proxy")
        path = self.path.split("?")[0]
        if path == "/ok":
            body, code = b"OK-DIRECT", 200
        elif path == "/notfound":
            body, code = b"NOT-FOUND", 404
        elif via:
            body, code = b"PROXIED-OK", 200
        else:
            body, code = b"BLOCKED", 403
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: A002
        pass


class _ForwardProxy(BaseHTTPRequestHandler):
    """简易 HTTP 正向代理：转发绝对 URL，可选带 X-Via-Proxy 标记。"""

    via = False

    def do_GET(self):  # noqa: N802
        u = urlsplit(self.path)  # requests 向代理发绝对 URL
        req = urllib.request.Request(f"{u.scheme}://{u.netloc}{u.path or '/'}")
        if self.via:
            req.add_header("X-Via-Proxy", "1")
        try:
            # 关闭环境代理：只连 127.0.0.1 目标，不被开发机 HTTP(S)_PROXY 干扰
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=5) as r:
                body = r.read()
                self.send_response(r.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except Exception:  # noqa: BLE001
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):  # noqa: A002
        pass


_ForwardVia = type("_ForwardVia", (_ForwardProxy,), {"via": True})
_ForwardPlain = type("_ForwardPlain", (_ForwardProxy,), {"via": False})


def _serve(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _shutdown(*servers):
    for s in servers:
        try:
            s.shutdown()
            s.server_close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------- #
# 单元：ProxyPool auto 语义
# ---------------------------------------------------------------------- #
def test_explicit_pool_always_on():
    pool = ProxyPool(["http://p1:1111"])
    assert not pool.is_auto
    assert pool.engaged          # 显式池始终启用
    assert pool.next() == "http://p1:1111"
    print("  [PASS] 显式配置池始终启用")


def test_auto_pool_gating():
    pool = ProxyPool(["http://p1:1111", "http://p2:2222"], auto=True)
    assert pool.is_auto and not pool.engaged
    assert pool.next() is None   # 未启用 → 直连
    pool.engage()
    assert pool.engaged
    assert pool.next() == "http://p1:1111"
    pool.mark_bad()
    assert pool.next() == "http://p2:2222"
    print("  [PASS] auto 池：未启用直连，engage 后走代理并轮换")


# ---------------------------------------------------------------------- #
# 端到端：反爬触发自动启用
# ---------------------------------------------------------------------- #
def test_auto_engage_on_antiscrape():
    target = _serve(_TargetHandler)
    proxy_plain = _serve(_ForwardPlain)   # 转发不带标记 → target 403
    proxy_via = _serve(_ForwardVia)       # 转发带标记 → target 200
    try:
        base = f"http://127.0.0.1:{target.server_port}"
        # 池：先一个"失效"代理（转发后仍 403），再一个可用代理（→200）
        pool = ProxyPool(
            [f"http://127.0.0.1:{proxy_plain.server_port}",
             f"http://127.0.0.1:{proxy_via.server_port}"],
            max_switches=3, auto=True,
        )
        http = _make_http()
        text = http.get_text(base + "/blocked", proxy_pool=pool,
                             retries=0, interval_ms=0)
        assert "PROXIED" in text, text
        assert pool.engaged
        # 再次请求：已启用 → 直接走代理（不再直连）
        text2 = http.get_text(base + "/blocked", proxy_pool=pool,
                              retries=0, interval_ms=0)
        assert "PROXIED" in text2
        print("  [PASS] 触发反爬(403)自动启用代理池并换 IP 重试成功")
    finally:
        _shutdown(target, proxy_plain, proxy_via)


def test_no_engage_on_normal_source():
    target = _serve(_TargetHandler)
    try:
        base = f"http://127.0.0.1:{target.server_port}"
        pool = ProxyPool(["http://127.0.0.1:1"], auto=True)  # 死代理，不应被使用
        http = _make_http()
        text = http.get_text(base + "/ok", proxy_pool=pool,
                             retries=0, interval_ms=0)
        assert "OK-DIRECT" in text
        assert not pool.engaged   # 没触发反爬 → 直连，不启用代理
        print("  [PASS] 正常源直连成功，不启用代理")
    finally:
        _shutdown(target)


def test_no_engage_on_regular_error():
    target = _serve(_TargetHandler)
    try:
        base = f"http://127.0.0.1:{target.server_port}"
        pool = ProxyPool(["http://127.0.0.1:1"], auto=True)
        http = _make_http()
        try:
            http.get_text(base + "/notfound", proxy_pool=pool,
                          retries=0, interval_ms=0)
            raise AssertionError("应抛 RequestError")
        except RequestError:
            pass
        assert not pool.engaged   # 404 非反爬 → 不启用代理
        print("  [PASS] 普通错误(404)不启用代理")
    finally:
        _shutdown(target)


# ---------------------------------------------------------------------- #
# 每源独立 auto 池：仅触发反爬的源启用
# ---------------------------------------------------------------------- #
def test_per_source_auto_pool():
    from framework.config import SourceConfig

    target = _serve(_TargetHandler)
    proxy_plain = _serve(_ForwardPlain)
    proxy_via = _serve(_ForwardVia)
    try:
        # 全局代理配置 → 测试临时 proxies 文件
        tmp = tempfile.mkdtemp()
        proxies_file = Path(tmp) / "proxies.json"
        proxies_file.write_text(json.dumps({
            "proxies": [
                f"http://127.0.0.1:{proxy_plain.server_port}",
                f"http://127.0.0.1:{proxy_via.server_port}",
            ]
        }), encoding="utf-8")
        reset_global_proxy_config()
        try:
            global_proxy_config({"file": str(proxies_file), "max_switches": 3})

            def _mk_source(sid):
                return SourceConfig.from_dict({
                    "$id": sid, "$type": "novel", "$name": sid,
                    "$enabled": True,
                    "transports": {"base_url": f"http://127.0.0.1:{target.server_port}"},
                })

            src_a = _mk_source("src_a")   # 触发反爬的源
            src_b = _mk_source("src_b")   # 正常的源
            pool_a, pool_b = src_a.proxy_pool(), src_b.proxy_pool()
            assert pool_a is not None and pool_a.is_auto and not pool_a.engaged
            assert pool_a is not pool_b   # 每源独立池实例（互不影响轮换状态）
            assert not pool_b.engaged

            http = _make_http()
            # src_a 正常抓取（直连 200），不启用
            ok = http.get_text(
                f"http://127.0.0.1:{target.server_port}/ok",
                headers=src_a.request_headers(),
                proxy_pool=pool_a, retries=0, interval_ms=0,
            )
            assert "OK-DIRECT" in ok and not pool_a.engaged
            # src_a 触发反爬 → 自动启用代理并成功
            text = http.get_text(
                f"http://127.0.0.1:{target.server_port}/blocked",
                headers=src_a.request_headers(),
                proxy_pool=pool_a, retries=0, interval_ms=0,
            )
            assert "PROXIED" in text
            assert pool_a.engaged
            assert not pool_b.engaged     # 只针对触发反爬的源
            print("  [PASS] 每源独立 auto 池，仅触发反爬的源启用代理")
        finally:
            reset_global_proxy_config()
    finally:
        _shutdown(target, proxy_plain, proxy_via)


def test_global_proxy_default_off():
    """默认全局配置指向空 data/proxies.json → 源无池（直连）。"""
    from framework.config import SourceConfig

    reset_global_proxy_config()
    try:
        src = SourceConfig.from_dict({
            "$id": "t", "$type": "novel", "$name": "t", "$enabled": True,
            "transports": {"base_url": "http://127.0.0.1:1"},
        })
        assert src.proxy_pool() is None   # 全局池为空 → 无代理
        print("  [PASS] 默认全局代理池为空 → 源走直连")
    finally:
        reset_global_proxy_config()


def main():
    tests = [
        test_explicit_pool_always_on,
        test_auto_pool_gating,
        test_auto_engage_on_antiscrape,
        test_no_engage_on_normal_source,
        test_no_engage_on_regular_error,
        test_per_source_auto_pool,
        test_global_proxy_default_off,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"  [FAIL] {t.__name__} 失败：{exc}")
            traceback.print_exc()
    print(f"\n结果：{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
