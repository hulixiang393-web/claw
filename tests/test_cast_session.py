from types import SimpleNamespace

import pytest

from framework.cast_session import CastSession


class FakeProxy:
    def __init__(self):
        self.calls = []
        self.next_url = "http://192.168.1.10:4321/s/token"

    def start_lan_endpoint(self, local_ip):
        self.calls.append(("start_lan_endpoint", local_ip))
        return "http://192.168.1.10:4321"

    def build_url(self, media_url, headers, ad_block, force_proxy, public_base):
        self.calls.append(("build_url", media_url, headers, ad_block, force_proxy, public_base))
        return self.next_url

    def stop_lan_endpoint(self):
        self.calls.append(("stop_lan_endpoint",))


class FakeClient:
    def __init__(self, calls, states=None, positions=None):
        self.calls = calls
        self.states = iter(states or [{"state": "PLAYING"}])
        self.positions = iter(positions or [{"position": 12, "duration": 90, "track": 1, "uri": "u"}])

    def set_av_transport_uri(self, uri, title, mime):
        self.calls.append(("set_av_transport_uri", uri, title, mime))

    def play(self):
        self.calls.append(("play",))

    def pause(self):
        self.calls.append(("pause",))

    def seek(self, seconds):
        self.calls.append(("seek", seconds))

    def stop(self):
        self.calls.append(("stop",))

    def get_transport_info(self):
        self.calls.append(("get_transport_info",))
        return next(self.states)

    def get_position_info(self):
        self.calls.append(("get_position_info",))
        return next(self.positions)


@pytest.fixture
def renderer():
    return SimpleNamespace(location="http://192.168.1.20:80/root.xml")


def test_start_uses_lan_url_sets_uri_then_plays_without_stopping_local_player(monkeypatch, renderer):
    proxy = FakeProxy()
    calls = []
    client = FakeClient(calls)
    stopped = []
    monkeypatch.setattr("framework.cast_session._local_ip_for_renderer", lambda _: "192.168.1.5")
    session = CastSession(proxy, lambda _: client, stopped.append)

    session.start(renderer, 2, "https://cdn.example/video.m3u8", "第二集", {"Referer": "r"}, {"x": 1}, True)

    assert proxy.calls == [
        ("start_lan_endpoint", "192.168.1.5"),
        ("build_url", "https://cdn.example/video.m3u8", {"Referer": "r"}, {"x": 1}, True, "http://192.168.1.10:4321"),
    ]
    assert calls == [
        ("set_av_transport_uri", "http://192.168.1.10:4321/s/token", "第二集", "application/vnd.apple.mpegurl"),
        ("play",),
    ]
    assert stopped == []


def test_poll_transitional_then_playing_stops_local_player_once(monkeypatch, renderer):
    proxy = FakeProxy()
    calls = []
    client = FakeClient(
        calls,
        states=[{"state": "TRANSITIONING"}, {"state": "PLAYING"}, {"state": "PLAYING"}],
        positions=[{"position": 0, "duration": 90, "track": 1, "uri": "u"}] * 3,
    )
    stopped = []
    monkeypatch.setattr("framework.cast_session._local_ip_for_renderer", lambda _: "192.168.1.5")
    session = CastSession(proxy, lambda _: client, lambda: stopped.append("stopped"))
    session.start(renderer, 0, "https://cdn.example/video.mp4", "第一集", {}, None, False)

    assert session.poll_state()["state"] == "TRANSITIONING"
    assert session.poll_state()["state"] == "PLAYING"
    assert session.poll_state()["state"] == "PLAYING"
    assert stopped == ["stopped"]


def test_pause_and_seek_delegate_to_client(monkeypatch, renderer):
    proxy = FakeProxy()
    calls = []
    client = FakeClient(calls)
    monkeypatch.setattr("framework.cast_session._local_ip_for_renderer", lambda _: "192.168.1.5")
    session = CastSession(proxy, lambda _: client)
    session.start(renderer, 0, "https://cdn.example/video.mp4", "第一集", {}, None, False)

    session.pause()
    session.seek(37)

    assert calls[-2:] == [("pause",), ("seek", 37)]


def test_set_episode_updates_index_and_sets_uri_then_plays(monkeypatch, renderer):
    proxy = FakeProxy()
    calls = []
    client = FakeClient(calls)
    monkeypatch.setattr("framework.cast_session._local_ip_for_renderer", lambda _: "192.168.1.5")
    session = CastSession(proxy, lambda _: client)
    session.start(renderer, 0, "https://cdn.example/first.mp4", "第一集", {}, None, False)
    proxy.next_url = "http://192.168.1.10:4321/s/next"

    session.set_episode(1, "https://cdn.example/second.mp4", "第二集", {}, None, False)

    assert session.current_episode_index == 1
    assert calls[-2:] == [
        ("set_av_transport_uri", "http://192.168.1.10:4321/s/next", "第二集", "video/mp4"),
        ("play",),
    ]


def test_start_failure_cleans_lan_endpoint_without_stopping_local_player(monkeypatch, renderer):
    proxy = FakeProxy()
    stopped = []

    def fail_factory(_):
        raise RuntimeError("client failed")

    monkeypatch.setattr("framework.cast_session._local_ip_for_renderer", lambda _: "192.168.1.5")
    session = CastSession(proxy, fail_factory, stopped.append)

    with pytest.raises(RuntimeError, match="client failed"):
        session.start(renderer, 0, "https://cdn.example/video.mp4", "第一集", {}, None, False)

    assert proxy.calls[-1] == ("stop_lan_endpoint",)
    assert stopped == []


def test_stop_is_idempotent_and_cleans_endpoint_when_client_stop_fails(monkeypatch, renderer):
    proxy = FakeProxy()
    calls = []

    class FailingStopClient(FakeClient):
        def stop(self):
            calls.append(("stop",))
            raise OSError("stop failed")

    client = FailingStopClient(calls)
    monkeypatch.setattr("framework.cast_session._local_ip_for_renderer", lambda _: "192.168.1.5")
    session = CastSession(proxy, lambda _: client)
    session.start(renderer, 0, "https://cdn.example/video.mp4", "第一集", {}, None, False)

    with pytest.raises(OSError, match="stop failed"):
        session.stop()
    session.stop()

    assert calls.count(("stop",)) == 1
    assert proxy.calls.count(("stop_lan_endpoint",)) == 2
    with pytest.raises(RuntimeError):
        session.pause()


def test_poll_returns_normalized_transport_and_position_fields(monkeypatch, renderer):
    proxy = FakeProxy()
    client = FakeClient(
        [],
        states=[{"state": "PLAYING", "status": "OK"}],
        positions=[{"position": 12, "duration": 90, "track": 3, "uri": "http://tv/video.mp4"}],
    )
    monkeypatch.setattr("framework.cast_session._local_ip_for_renderer", lambda _: "192.168.1.5")
    session = CastSession(proxy, lambda _: client)
    session.start(renderer, 2, "https://cdn.example/video.mp4", "第二集", {}, None, False)

    assert session.poll_state() == {
        "state": "PLAYING",
        "position": 12,
        "duration": 90,
        "track": 3,
        "uri": "http://tv/video.mp4",
    }


def test_controls_require_active_session():
    session = CastSession(FakeProxy(), lambda _: None)

    with pytest.raises(RuntimeError):
        session.play()
    with pytest.raises(RuntimeError):
        session.pause()
    with pytest.raises(RuntimeError):
        session.seek(1)
