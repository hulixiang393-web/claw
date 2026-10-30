# -*- coding: utf-8 -*-
"""媒体分片链路的 TLS 降级与快速失败。

现场（omofuna 实测，2026-10）：
 - 正片分片经站点中继 302 到 `s3.uvdag.top`，那台证书与主机名不匹配 →
   requests 抛 SSLError，媒体代理**完全没有降级逻辑**（早上只修了抓页面的
   `framework/http.py`），分片全废 → 播放器反复重试 → 用户看到「源站很快
   但 mpv 卡」。
 - 清单里还有大量死分片：直连先吃满 5.5s 连接超时、再走代理 1.3s 拿 404，
   一次拖动白等十几秒。分片必须短超时、快速失败。
"""
from __future__ import annotations

import pytest
import requests

import framework.media_proxy as mp


class _Resp:
    status_code = 200
    headers: dict = {}

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _clean():
    mp._reset_media_insecure_hosts()
    yield
    mp._reset_media_insecure_hosts()


class _Session:
    """记录每次调用的假会话：前 n 次抛 SSLError，之后返回响应。"""

    def __init__(self, fail_times: int = 1, exc=None):
        self.fail_times = fail_times
        self.calls: list[dict] = []
        self.exc = exc or requests.exceptions.SSLError(
            "certificate verify failed: host='bad.test'"
        )

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.exc
        return _Resp()


def test_ssl_error_records_host_and_retries_without_verify():
    """SSLError → 记账该主机 → 就地 verify=False 重试成功。"""
    session = _Session()
    resp = mp._media_get(session, "https://bad.test/a.ts", {}, (3.0, 10.0))
    assert resp.status_code == 200
    assert mp._media_host_insecure("bad.test") is True
    assert session.calls[0].get("verify") is not False
    assert session.calls[1]["verify"] is False


def test_redirect_target_host_from_error_text_is_also_recorded():
    """302 目标主机的证书错误要一起记账（入口 host 不同）。"""
    session = _Session()
    mp._media_get(session, "https://relay.test:2087/https://c.test/a.ts", {},
                  (3.0, 10.0))
    assert mp._media_host_insecure("bad.test") is True


def test_direct_route_does_not_downgrade_in_place():
    """直连遇 SSLError 只记账并抛错，交给上层回退代理（不在同一条路纠缠）。"""
    session = _Session(fail_times=1)
    with pytest.raises(requests.exceptions.SSLError):
        mp._media_get(session, "https://bad.test/a.ts", {}, (3.0, 10.0),
                      downgrade_on_error=False)
    assert len(session.calls) == 1
    assert mp._media_host_insecure("bad.test") is True


def test_marked_host_skips_verify_on_later_requests():
    """已记账的主机后续请求直接不校验，不再多打一次失败请求。"""
    mp._note_media_host_insecure("bad.test")
    session = _Session(fail_times=0)
    mp._media_get(session, "https://bad.test/a.ts", {}, (3.0, 10.0))
    assert len(session.calls) == 1
    assert session.calls[0]["verify"] is False


def test_reset_clears_insecure_hosts():
    mp._note_media_host_insecure("bad.test")
    assert mp._media_host_insecure("bad.test") is True
    mp._reset_media_insecure_hosts()
    assert mp._media_host_insecure("bad.test") is False


def test_insecure_host_memory_is_capped():
    """记账表有上限，不能无界增长（长时间播放会遇到大量 CDN 域）。"""
    for i in range(400):
        mp._note_media_host_insecure(f"h{i}.test")
    assert len(mp._INSECURE_MEDIA_HOSTS) <= 256


def test_non_ssl_errors_are_not_marked_insecure():
    """普通连接错误不该被当成证书问题降级。"""
    session = _Session(fail_times=1, exc=requests.ConnectionError("boom"))
    with pytest.raises(requests.ConnectionError):
        mp._media_get(session, "https://ok.test/a.ts", {}, (3.0, 10.0))
    assert mp._media_host_insecure("ok.test") is False


def test_hosts_in_ssl_error_extracts_redirect_target():
    exc = requests.exceptions.SSLError(
        "HTTPSConnectionPool(host='s3.uvdag.top', port=443): Max retries exceeded"
    )
    assert mp._hosts_in_ssl_error(exc) == ["s3.uvdag.top"]


def test_segment_timeout_is_short():
    """分片请求必须用短超时：默认 60s 读会让一个死分片拖住整次拖动。"""
    import inspect
    import re

    src = inspect.getsource(mp.MediaProxy._serve_cache)
    # 宽松断言：分片路径存在 (3.0, 10.0) 这类短超时元组
    assert re.search(r"seg_timeout\s*=\s*\(3\.0,\s*10\.0\)\s*if\s*is_segment", src)
