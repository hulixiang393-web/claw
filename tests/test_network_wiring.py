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


# ---------------------------------------------------------------------- #
# D8：check_links 并发数（源 constraints / 全局设置 / 自适应）与连接隔离
# ---------------------------------------------------------------------- #
def test_check_link_workers_source_constraint_and_setting():
    from framework.discovery import Discovery

    disc = Discovery(_Stub(), _Stub(), _Stub())
    # 源 constraints 显式 → 直接采用（钳制 1..8）
    src = SimpleNamespace(raw={"constraints": {"check_links_threads": 2}})
    assert disc._check_link_workers(src, ["u"] * 20, 3) == 2
    # max_concurrency 兜底
    src2 = SimpleNamespace(raw={"constraints": {"max_concurrency": 1}})
    assert disc._check_link_workers(src2, ["u"] * 5, 3) == 1
    # 无配置 → 自适应（上限 8，链接少时跟随链接数）
    src3 = SimpleNamespace(raw={})
    assert disc._check_link_workers(src3, ["u"] * 2, 3) == 2
    assert disc._check_link_workers(src3, ["u"] * 20, 3) == 8
    # 全局 network.check_links_threads（dict 运行时）
    disc._health_reporter = SimpleNamespace(_runtime={"network": {"check_links_threads": 4}})
    assert disc._check_link_workers(src3, ["u"] * 20, 3) == 4
    # 全局钳制到 8
    assert disc._check_link_workers(src3, ["u"] * 20, 3) >= 1


def test_check_link_workers_global_from_settings_manager(tmp_path):
    from framework.discovery import Discovery
    from framework.settings_manager import SettingsManager

    settings = SettingsManager(tmp_path / "settings.json")
    settings.set("network", "check_links_threads", 6)
    disc = Discovery(_Stub(), _Stub(), _Stub())
    disc._health_reporter = SimpleNamespace(_runtime=settings)
    assert disc._check_link_workers(SimpleNamespace(raw={}), ["u"] * 10, 3) == 6


def test_check_links_reuses_one_client_per_worker():
    """D8：同一 worker 线程内复用同一 HttpClient（不每链接新建），且配置一致。"""
    from framework.discovery import Discovery

    created = []

    class _Fake(_FakeCheckHttp):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            created.append(self)

    disc = Discovery(_Stub(), _Stub(), _Stub())
    disc._http = SimpleNamespace(defaults=NetworkDefaults(retries=0))
    # 固定 1 worker + 多条链接：验证链接间复用（实例数远小于链接数）。
    source = SimpleNamespace(
        base_url="http://example.com",
        raw={"constraints": {"check_links_threads": 1}},
    )
    urls = [f"http://example.com/{i}" for i in range(5)]
    with patch("framework.discovery.HttpClient", _Fake):
        results = disc.check_links(source, urls)
    assert len(results) == 5 and all(r["ok"] for r in results)
    assert len(created) == 1  # 1 worker 只建 1 个 client，跨链接复用
    assert created[0].defaults is disc._http.defaults
    assert len(created[0].calls) == 5


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))