# -*- coding: utf-8 -*-
"""external_player.player_command：VLC HTTP 控制命令转发。

VLC 以 --extraintf=http 启动后，App 内键盘事件经 player_command 转发成
HTTP 控制命令。本文件 mock requests 与 _control_state，只断言：
- 无控制会话 → False
- requests 缺失（ImportError 兜底）→ False
- 有会话 → 请求 /requests/status.xml，参数含 command/val，basic auth
  用户名为空、密码为 _control_state 里的密码；2xx 返回 True
- 网络异常 / HTTP 4xx/5xx → False
"""
import framework.external_player as ep


def _set_state(monkeypatch, **kw):
    state = {"host": "127.0.0.1", "port": 8090, "password": "secret"}
    state.update(kw)
    monkeypatch.setattr(ep, "_control_state", state)


class _FakeRequests:
    """可注入的 requests 替身：记录 get 调用，固定 status_code 或抛异常。"""

    def __init__(self, status=200, exc=None):
        self.status = status
        self.exc = exc
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc is not None:
            raise self.exc
        return type("R", (), {"status_code": self.status})()


def test_no_control_session_returns_false(monkeypatch):
    """_control_state 为空（浏览器 fallback / 未成功拉起）→ 静默 False。"""
    monkeypatch.setattr(ep, "_control_state", None)
    fake = _FakeRequests()
    monkeypatch.setattr(ep, "requests", fake)
    assert ep.player_command("pl_pause") is False
    assert fake.calls == []


def test_requests_missing_returns_false(monkeypatch):
    """requests 不可用（ImportError 兜底为空）→ 静默 False。"""
    _set_state(monkeypatch)
    monkeypatch.setattr(ep, "requests", None)
    assert ep.player_command("pl_pause") is False


def test_command_sent_with_command_and_val(monkeypatch):
    _set_state(monkeypatch)
    fake = _FakeRequests(status=200)
    monkeypatch.setattr(ep, "requests", fake)
    assert ep.player_command("seek", "30") is True

    url, kw = fake.calls[0]
    assert url == "http://127.0.0.1:8090/requests/status.xml"
    assert kw["params"] == {"command": "seek", "val": "30"}
    auth = kw["auth"]
    assert auth[0] == ""               # VLC HTTP 接口用户名空
    assert auth[1] == "secret"         # 密码为 --http-password 值
    assert kw["timeout"] == 2


def test_command_no_val_omits_val_param(monkeypatch):
    _set_state(monkeypatch)
    fake = _FakeRequests(status=200)
    monkeypatch.setattr(ep, "requests", fake)
    assert ep.player_command("pl_pause") is True

    url, kw = fake.calls[0]
    assert kw["params"] == {"command": "pl_pause"}  # 无 val 不追加空值
    assert url == "http://127.0.0.1:8090/requests/status.xml"


def test_http_error_returns_false(monkeypatch):
    _set_state(monkeypatch)
    fake = _FakeRequests(status=500)
    monkeypatch.setattr(ep, "requests", fake)
    assert ep.player_command("pl_next") is False


def test_network_exception_returns_false(monkeypatch):
    _set_state(monkeypatch)
    fake = _FakeRequests(exc=OSError("port closed"))
    monkeypatch.setattr(ep, "requests", fake)
    assert ep.player_command("pl_next", "1") is False
    assert len(fake.calls) == 1  # 已尝试发请求（非静默短路）


def test_control_args_in_open_with_player(monkeypatch):
    """open_with_player 失败的 Popen 除外；成功时控制接口参数随启动命令带上。"""
    import framework.external_player as ep_mod  # noqa: PLC0415

    monkeypatch.setattr(ep_mod, "_locate_vlc",
                        lambda: r"C:\Program Files\VideoLAN\VLC\vlc.exe")
    captured = {}

    def _popen(args, **kw):
        captured["args"] = list(args)
        return object()

    monkeypatch.setattr(ep_mod, "proxy_url_for",
                        lambda u, *a, **k: "http://127.0.0.1:9/s/x")
    monkeypatch.setattr(ep_mod.subprocess, "Popen", _popen)
    monkeypatch.setattr(ep_mod, "_pick_http_port", lambda: 8088)
    monkeypatch.setattr(ep_mod.secrets, "token_hex", lambda n: "CTRLPWD")
    monkeypatch.setattr(ep_mod, "_last_proc", None)
    monkeypatch.setattr(ep_mod, "_control_state", None)

    ep_mod.open_with_player("https://cdn.example.com/movie.mp4",
                            headers={"Referer": "https://fake.example/"})
    args = captured["args"]
    assert "--extraintf=http" in args
    assert "--http-host=127.0.0.1" in args
    assert "--http-port=8088" in args
    assert "--http-password=CTRLPWD" in args
    assert args.index("--extraintf=http") < args.index("http://127.0.0.1:9/s/x")
    # 控制状态已写入，player_command 可直接转发
    assert ep_mod._control_state == {
        "host": "127.0.0.1", "port": 8088, "password": "CTRLPWD",
    }


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))