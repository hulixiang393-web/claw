"""证书链不完整站点的降级测试（test_http_cert_fallback.py）。

背景：requests+certifi 不会像浏览器那样去 AIA 补拉中间证书，站点少发中间证书时
直接 `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`，
界面里等同"打不开"（agedm.io 实测如此；curl -k 与浏览器都能正常取到内容）。

策略：**只在证书链验证失败时**对该主机改用 verify=False，并记住该主机；
超时、连接被拒、反爬状态码一律照旧，其他主机校验强度不变。

运行：python -m pytest tests/test_http_cert_fallback.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import framework.http as http_mod  # noqa: E402
from framework.http import HttpClient, NetworkDefaults  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    http_mod._reset_insecure_hosts()
    http_mod._reset_proxy_failures()
    yield
    http_mod._reset_insecure_hosts()
    http_mod._reset_proxy_failures()


class _CertError(Exception):
    """模拟 requests 的 SSLCertVerificationError 文本。"""


def _cert_failure():
    return _CertError(
        "HTTPSConnectionPool(host='www.agedm.io', port=443): Max retries exceeded "
        "(Caused by SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] "
        "certificate verify failed: unable to get local issuer certificate (_ssl.c:997)')))"
    )


class _Resp:
    status_code = 200
    text = "<html>ok</html>"
    content = b"IMG"
    headers = {"Content-Type": "text/html; charset=utf-8"}

    def raise_for_status(self):
        return None


def _direct_client():
    """关掉系统代理，专注证书分支。"""
    client = HttpClient(sleeper=lambda s: None, defaults=NetworkDefaults())
    client._system_proxy_enabled = False
    return client


# ---------------------------------------------------------------------- #
# _is_cert_error
# ---------------------------------------------------------------------- #
def test_is_cert_error_recognizes_chain_failure():
    assert http_mod._is_cert_error(_cert_failure()) is True


def test_is_cert_error_ignores_timeout_and_connection_errors():
    assert http_mod._is_cert_error(OSError("read timed out")) is False
    assert http_mod._is_cert_error(
        OSError("[WinError 10054] 远程主机强迫关闭了一个现有的连接")) is False


# ---------------------------------------------------------------------- #
# GET 文本
# ---------------------------------------------------------------------- #
def test_cert_failure_retries_without_verify(monkeypatch):
    verifies = []
    client = _direct_client()

    def fake_get(url, **kwargs):
        verifies.append(kwargs.get("verify"))
        if kwargs.get("verify"):
            raise _cert_failure()
        return _Resp()

    monkeypatch.setattr(client._session, "get", fake_get)
    assert "ok" in client.get_text("https://www.agedm.io/catalog/x", retries=0)
    assert verifies == [True, False]
    assert http_mod._insecure_host("www.agedm.io") is True


def test_remembered_host_skips_verify_entirely(monkeypatch):
    verifies = []
    client = _direct_client()

    def fake_get(url, **kwargs):
        verifies.append(kwargs.get("verify"))
        return _Resp()

    monkeypatch.setattr(client._session, "get", fake_get)
    client.get_text("https://www.agedm.io/a", retries=0)
    http_mod._note_insecure_host("www.agedm.io")
    verifies.clear()
    client.get_text("https://www.agedm.io/b", retries=0)
    assert verifies == [False], "已记录的主机不该再白跑一次严格校验"


def test_timeout_is_not_downgraded(monkeypatch):
    verifies = []
    client = _direct_client()

    def fake_get(url, **kwargs):
        verifies.append(kwargs.get("verify"))
        raise OSError("read timed out")

    monkeypatch.setattr(client._session, "get", fake_get)
    with pytest.raises(http_mod.RequestError):
        client.get_text("https://www.agedm.io/a", retries=0)
    assert verifies == [True], "超时与证书无关，不放宽"


def test_other_hosts_keep_strict_verification(monkeypatch):
    """放宽只针对出问题的主机：别的站仍然严格校验、也不被记进名单。"""
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("verify")))
        if kwargs.get("verify") and "agedm" in url:
            raise _cert_failure()
        return _Resp()

    client = _direct_client()
    monkeypatch.setattr(client._session, "get", fake_get)
    client.get_text("https://www.agedm.io/a", retries=0)
    client.get_text("https://www.dm5.com/b", retries=0)
    assert calls == [
        ("https://www.agedm.io/a", True),    # 首次严格
        ("https://www.agedm.io/a", False),   # 证书链问题 → 放宽重试
        ("https://www.dm5.com/b", True),     # 别的站照旧严格，一次就好
    ]
    assert http_mod._insecure_host("www.agedm.io") is True
    assert http_mod._insecure_host("www.dm5.com") is False


# ---------------------------------------------------------------------- #
# bytes / POST
# ---------------------------------------------------------------------- #
def test_get_bytes_also_falls_back(monkeypatch):
    verifies = []

    def fake_get(url, **kwargs):
        verifies.append(kwargs.get("verify"))
        if kwargs.get("verify"):
            raise _cert_failure()
        return _Resp()

    client = _direct_client()
    monkeypatch.setattr(client._session, "get", fake_get)
    assert client.get_bytes("https://www.agedm.io/p.jpg", retries=0) == b"IMG"
    assert verifies == [True, False]


def test_post_text_also_falls_back(monkeypatch):
    verifies = []

    def fake_post(url, **kwargs):
        verifies.append(kwargs.get("verify"))
        if kwargs.get("verify"):
            raise _cert_failure()
        return _Resp()

    client = _direct_client()
    monkeypatch.setattr(client._session, "post", fake_post)
    assert "ok" in client.post_text("https://www.agedm.io/api", json_body={}, retries=0)
    assert verifies == [True, False]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
