# -*- coding: utf-8 -*-
"""external_player 的 VLC HTTP 控制面：命令转发 + 播放列表项 id 定位。

VLC 以 --extraintf=http 启动后，App 内键盘事件经 player_command 转发成
HTTP 控制命令；全集播放列表场景还要按**播放列表项 id** 跳集（pl_play 的
id 不是下标），并读 playlist.json 建 {集下标: vlc_id} 映射。本文件 mock
requests 与 _control_state，只断言：
- 无控制会话 → False
- requests 缺失（ImportError 兜底）→ False
- 有会话 → 请求 /requests/status.xml，参数含 command/val，basic auth
  用户名为空、密码为 _control_state 里的密码；2xx 返回 True
- 网络异常 / HTTP 4xx/5xx → False
- item_id 走 id= 参数（不占用 val）；上一集命令名是 pl_previous（无 pl_prev）
- playlist.json 的解析 / 缓存 / refresh / 失败不缓存空结果
- 进程存活判断；换集重开（_terminate_previous）时清空控制态与列表缓存
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


# ---- 播放列表项 id 控制面（全集跳集） ----


class _Resp:
    """requests 替身响应：固定 status_code，可选 json 载荷。"""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _state(monkeypatch):
    """摆好控制会话 + 清空列表缓存（控制态复用本文件既有的 _set_state）。"""
    _set_state(monkeypatch)
    monkeypatch.setattr(ep, "_playlist_items", [])


def test_pl_play_sends_id_not_val(monkeypatch):
    """pl_play 的取值是播放列表**项 id**：必须发 id= 而不是 val=。"""
    _state(monkeypatch)
    seen = {}

    def _get(url, params=None, auth=None, timeout=None):
        seen["url"] = url
        seen["params"] = dict(params or {})
        return _Resp(200)

    monkeypatch.setattr(ep.requests, "get", _get)
    assert ep.player_command("pl_play", item_id=7) is True
    assert seen["params"] == {"command": "pl_play", "id": 7}
    assert "val" not in seen["params"]
    assert "status.xml" in seen["url"]


def test_val_still_supported(monkeypatch):
    """既有 seek/volume 走 val，签名扩展不得破坏。"""
    _state(monkeypatch)
    seen = {}
    monkeypatch.setattr(ep.requests, "get",
                        lambda url, params=None, **k: (seen.update(
                            params=dict(params or {})) or _Resp(200)))
    assert ep.player_command("seek", "+10") is True
    assert seen["params"] == {"command": "seek", "val": "+10"}


def test_previous_uses_valid_command_name(monkeypatch):
    """VLC 没有 pl_prev；上一集必须发 pl_previous。"""
    _state(monkeypatch)
    seen = []
    monkeypatch.setattr(ep.requests, "get",
                        lambda url, params=None, **k: (
                            seen.append(params["command"]) or _Resp(200)))
    assert ep.player_previous() is True
    assert seen == ["pl_previous"]


def test_player_goto_sends_item_id(monkeypatch):
    """player_goto 把集号作为 id= 发（pl_play 取项 id，不是下标）。"""
    _state(monkeypatch)
    seen = {}

    def _get(url, params=None, auth=None, timeout=None):
        seen["params"] = dict(params or {})
        return _Resp(200)

    monkeypatch.setattr(ep.requests, "get", _get)
    assert ep.player_goto(9) is True
    assert seen["params"] == {"command": "pl_play", "id": 9}


def test_player_next_sends_pl_next(monkeypatch):
    """player_next 发的是 pl_next（与上一集 pl_previous 成对）。"""
    _state(monkeypatch)
    seen = []

    def _get(url, params=None, auth=None, timeout=None):
        seen.append(dict(params or {}))
        return _Resp(200)

    monkeypatch.setattr(ep.requests, "get", _get)
    assert ep.player_next() is True
    assert seen == [{"command": "pl_next"}]


def test_playlist_items_parsed_and_cached(monkeypatch):
    _state(monkeypatch)
    payload = [{"id": 1, "name": "第01集 a", "uri": "http://x/e/k/0"},
               {"id": 2, "name": "第02集 b", "uri": "http://x/e/k/1"}]
    calls = []

    def _get(url, params=None, auth=None, timeout=None):
        calls.append((url, timeout))
        return _Resp(200, payload)

    monkeypatch.setattr(ep.requests, "get", _get)
    assert ep.player_playlist_items() == payload
    assert ep.player_playlist_items() == payload      # 命中缓存
    assert len(calls) == 1                            # 只请求了一次
    assert "playlist.json" in calls[0][0]
    assert calls[0][1] == 2.0                         # 默认超时透传，不无限期挂住
    ep.player_playlist_items(refresh=True)
    assert len(calls) == 2


def test_playlist_items_normalizes_string_ids(monkeypatch):
    """VLC 发的是**字符串** id（httprequests.lua: result.id=tostring(item.id)）。

    必须在 player_playlist_items 这一个地方归一化：握手按 id 发 pl_play、App
    侧按 id 切集，都假定 int。不归一化的话「非 int 就跳过」会把每一项都丢掉，
    映射恒空。
    """
    _state(monkeypatch)
    monkeypatch.setattr(ep.requests, "get", lambda *a, **k: _Resp(200, [
        {"id": "1", "name": "第01集", "uri": "http://x/e/k/0"},
        {"id": "2", "name": "第02集", "uri": "http://x/e/k/1"}]))
    items = ep.player_playlist_items()
    assert [it["id"] for it in items] == [1, 2]        # "1" -> 1
    assert all(isinstance(it["id"], int) for it in items)
    # 其余字段原样透传
    assert items[0]["uri"] == "http://x/e/k/0"
    # 转不成 int 的保留原值（不丢项：项数是有效信息），由消费方自行守卫
    monkeypatch.setattr(ep.requests, "get", lambda *a, **k: _Resp(200, [
        {"id": "abc", "uri": "http://x/e/k/0"}]))
    assert ep.player_playlist_items(refresh=True) == [
        {"id": "abc", "uri": "http://x/e/k/0"}]


def test_playlist_items_failure_returns_empty(monkeypatch):
    _state(monkeypatch)
    monkeypatch.setattr(ep.requests, "get",
                        lambda *a, **k: _Resp(500, []))
    assert ep.player_playlist_items() == []
    monkeypatch.setattr(ep.requests, "get",
                        lambda *a, **k: _Resp(200, [{"id": 3}]))
    assert ep.player_playlist_items() == [{"id": 3}]   # 空结果不缓存 → 重试


def test_playlist_items_non_list_payload(monkeypatch):
    """playlist.json 返回非 list（VLC 未就绪/异常响应体）→ 静默 []。"""
    _state(monkeypatch)
    monkeypatch.setattr(ep.requests, "get",
                        lambda *a, **k: _Resp(200, {"state": "not-a-list"}))
    assert ep.player_playlist_items() == []


def test_command_without_control_state_returns_false(monkeypatch):
    _state(monkeypatch)
    monkeypatch.setattr(ep, "_control_state", None)
    assert ep.player_goto(1) is False
    assert ep.player_next() is False
    assert ep.player_playlist_items() == []


def test_player_running(monkeypatch):
    class _Proc:
        def __init__(self, code):
            self._code = code

        def poll(self):
            return self._code

    monkeypatch.setattr(ep, "_last_proc", None)
    assert ep.player_running() is False
    monkeypatch.setattr(ep, "_last_proc", _Proc(None))
    assert ep.player_running() is True
    monkeypatch.setattr(ep, "_last_proc", _Proc(0))
    assert ep.player_running() is False


def test_terminate_previous_clears_playlist_cache(monkeypatch):
    """换集重开：旧会话的 playlist 缓存必须清空（否则 id 映射串台）。"""
    monkeypatch.setattr(ep, "_playlist_items", [{"id": 1}])
    monkeypatch.setattr(ep, "_control_state", {"port": 1, "password": "x"})
    monkeypatch.setattr(ep, "_last_proc", None)
    ep._terminate_previous()
    assert ep._playlist_items == []
    assert ep._control_state is None


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))