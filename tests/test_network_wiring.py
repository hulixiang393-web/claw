# -*- coding: utf-8 -*-
"""settings → NetworkDefaults / check_links 接线测试（test_network_wiring.py）。

覆盖：app.py 的 network_defaults_from_settings 从 app_config network.* 读取
impersonate（TLS 伪装档位，默认关闭）与 user_agents（UA 轮换列表，默认空）；
discovery.check_links 每个 worker 新建 HttpClient 时复用 App 层已接线的
NetworkDefaults（含新键），保证并发检查与主请求网络能力一致。全部 mock 不联网。
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.http import NetworkDefaults


class _FakeSettings:
    """SettingsManager 同接口：get(section, key, default=None)。"""

    def __init__(self, data):
        self.data = data

    def get(self, section, key, default=None):
        return self.data.get(section, {}).get(key, default)


def test_network_defaults_off_by_default():
    """缺省配置（无 impersonate/user_agents 键）→ 新键关闭，行为不变。"""
    from gui.app import network_defaults_from_settings

    settings = _FakeSettings({
        "network": {
            "default_timeout": 10,
            "default_retries": 3,
            "default_request_interval": 0,
            "proxy": None,
            "default_user_agent": "UA-DEFAULT",
        },
    })
    nd = network_defaults_from_settings(settings)
    assert nd.impersonate is None
    assert nd.user_agents is None
    assert nd.timeout == 10 and nd.retries == 3
    assert nd.user_agent == "UA-DEFAULT"


def test_network_defaults_wired_from_settings():
    """配置 impersonate/user_agents → 接线进 NetworkDefaults。"""
    from gui.app import network_defaults_from_settings

    settings = _FakeSettings({
        "network": {
            "default_timeout": 20,
            "default_retries": 5,
            "default_request_interval": 100,
            "proxy": "http://127.0.0.1:7890",
            "default_user_agent": "UA-A",
            "impersonate": "chrome",
            "user_agents": ["UA-1", "UA-2"],
        },
    })
    nd = network_defaults_from_settings(settings)
    assert nd.impersonate == "chrome"
    assert nd.user_agents == ["UA-1", "UA-2"]
    assert nd.timeout == 20 and nd.retries == 5 and nd.interval_ms == 100
    assert nd.proxy == "http://127.0.0.1:7890"


def test_network_defaults_empty_list_off():
    """user_agents=[] 显式空列表等价关闭（不改变行为）。"""
    from gui.app import network_defaults_from_settings

    nd = network_defaults_from_settings(_FakeSettings({
        "network": {"user_agents": [], "impersonate": None},
    }))
    assert nd.user_agents is None
    assert nd.impersonate is None


class _FakeCheckHttp:
    """记录构造 defaults；get_text 返回假 HTML（不联网）。"""

    def __init__(self, defaults=None, *a, **k):
        self.defaults = defaults
        self.calls = []

    def get_text(self, url, headers=None, **k):
        self.calls.append(url)
        return "<html></html>"


class _Stub:
    pass


def test_check_links_reuses_wired_defaults():
    """check_links 每 worker 的 HttpClient 复用 App 层已接线的 NetworkDefaults。"""
    from framework.discovery import Discovery

    defaults = NetworkDefaults(impersonate="chrome", user_agents=["UA-1"])
    disc = Discovery(_Stub(), _Stub(), _Stub())
    disc._http = SimpleNamespace(defaults=defaults)
    source = SimpleNamespace(base_url="http://example.com")

    seen = []
    orig_init = _FakeCheckHttp.__init__

    def _spy(self, defaults=None, *a, **k):
        seen.append(defaults)
        orig_init(self, defaults=defaults, *a, **k)

    with patch("framework.discovery.HttpClient", _FakeCheckHttp), patch.object(
        _FakeCheckHttp, "__init__", _spy
    ):
        results = disc.check_links(source, ["http://example.com/a"])
    assert results and results[0]["ok"] is True
    assert seen, "应创建 worker HttpClient"
    assert all(s is defaults for s in seen)


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))