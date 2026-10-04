from types import SimpleNamespace

import pytest

from framework.cast_session import CastSession


class FakeProxy:
    def __init__(self, events=None):
        self.calls = []
        self.events = events if events is not None else self.calls
        self.next_url = "http://192.168.1.10:4321/s/token"
        self.lan_tokens = {"token"}
        self.release_calls = []
        self._lan_error = None

    def start_lan_endpoint(self, local_ip):
        self.calls.append(("start_lan_endpoint", local_ip))
        if self.events is not self.calls:
            self.events.append(("start_lan_endpoint", local_ip))
        return "http://192.168.1.10:4321"

    def build_url(self, media_url, headers, ad_block, force_proxy, public_base):
        self.calls.append(("build_url", media_url, headers, ad_block, force_proxy, public_base))
        if self.events is not self.calls:
            self.events.append(("build_url", media_url))
        token = self.next_url.rsplit("/", 1)[-1]
        self.lan_tokens.add(token)
        return self.next_url

    def release_token(self, url):
        token = url.rsplit("/", 1)[-1]
        self.release_calls.append(token)
        self.lan_tokens.discard(token)

    def stop_lan_endpoint(self):
        self.calls.append(("stop_lan_endpoint",))
        if self.events is not self.calls:
            self.events.append(("stop_lan_endpoint",))


class FakeClient:
    def __init__(self, calls, states=None, positions=None, events=None):
        self.calls = calls
        self.events = events if events is not None else calls
        self.states = iter(states or [{"state": "PLAYING"}])
        self.positions = iter(positions or [{"position": 12, "duration": 90, "track": 1, "uri": "u"}])

    def set_av_transport_uri(self, uri, title, mime):
        self.calls.append(("set_av_transport_uri", uri, title, mime))
        if self.events is not self.calls:
            self.events.append(("set_av_transport_uri", uri, title, mime))

    def play(self):
        self.calls.append(("play",))
        if self.events is not self.calls:
            self.events.append(("play",))

    def pause(self):
        self.calls.append(("pause",))
        if self.events is not self.calls:
            self.events.append(("pause",))

    def seek(self, seconds):
        self.calls.append(("seek", seconds))
        if self.events is not self.calls:
            self.events.append(("seek", seconds))

    def stop(self):
        self.calls.append(("stop",))
        if self.events is not self.calls:
            self.events.append(("stop",))

    def get_transport_info(self):
        self.calls.append(("get_transport_info",))
        if self.events is not self.calls:
            self.events.append(("get_transport_info",))
        return next(self.states)

    def get_position_info(self):
        self.calls.append(("get_position_info",))
        if self.events is not self.calls:
            self.events.append(("get_position_info",))
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


def test_start_rejects_active_session_with_runtime_error(monkeypatch, renderer):
    proxy = FakeProxy()
    calls = []
    client = FakeClient(calls)
    monkeypatch.setattr("framework.cast_session._local_ip_for_renderer", lambda _: "192.168.1.5")
    session = CastSession(proxy, lambda _: client)

    session.start(renderer, 0, "https://cdn.example/video.mp4", "第一集", {}, None, False)

    with pytest.raises(RuntimeError, match="already active"):
        session.start(renderer, 1, "https://cdn.example/video2.mp4", "第二集", {}, None, False)


def test_set_episode_recovers_old_uri_on_play_failure(monkeypatch, renderer):
    events = []
    proxy = FakeProxy(events=events)
    calls = []

    class FailingPlayClient(FakeClient):
        def __init__(self, calls, events, fail_on_play_count):
            super().__init__(calls, events=events)
            self.play_count = 0
            self.fail_on_play_count = fail_on_play_count

        def play(self):
            self.play_count += 1
            self.calls.append(("play",))
            if self.events is not self.calls:
                self.events.append(("play", self.play_count))
            if self.play_count == self.fail_on_play_count:
                raise RuntimeError("play failed")

    client = FailingPlayClient(calls, events, fail_on_play_count=2)
    monkeypatch.setattr("framework.cast_session._local_ip_for_renderer", lambda _: "192.168.1.5")
    session = CastSession(proxy, lambda _: client)

    session.start(renderer, 0, "https://cdn.example/first.mp4", "第一集", {}, None, False)
    old_url = session._current_url
    proxy.next_url = "http://192.168.1.10:4321/s/new_token"

    with pytest.raises(RuntimeError, match="play failed"):
        session.set_episode(1, "https://cdn.example/second.mp4", "第二集", {}, None, False)

    assert session.current_episode_index == 0
    assert session._current_url == old_url
    assert ("set_av_transport_uri", old_url, "第一集", "video/mp4") in calls
    assert "new_token" in proxy.release_calls


def test_set_episode_releases_old_token_on_success(monkeypatch, renderer):
    proxy = FakeProxy()
    calls = []
    client = FakeClient(calls)
    monkeypatch.setattr("framework.cast_session._local_ip_for_renderer", lambda _: "192.168.1.5")
    session = CastSession(proxy, lambda _: client)

    session.start(renderer, 0, "https://cdn.example/first.mp4", "第一集", {}, None, False)
    old_token = session._current_url.rsplit("/", 1)[-1]
    proxy.next_url = "http://192.168.1.10:4321/s/new_token"

    session.set_episode(1, "https://cdn.example/second.mp4", "第二集", {}, None, False)

    assert old_token in proxy.release_calls


def test_stop_propagates_lan_endpoint_error(monkeypatch, renderer):
    proxy = FakeProxy()
    stop_attempts = [0]

    def failing_stop_lan_endpoint():
        stop_attempts[0] += 1
        proxy.calls.append(("stop_lan_endpoint",))
        if stop_attempts[0] == 1:
            proxy._lan_error = RuntimeError("endpoint cleanup failed")

    proxy.stop_lan_endpoint = failing_stop_lan_endpoint
    calls = []
    client = FakeClient(calls)
    monkeypatch.setattr("framework.cast_session._local_ip_for_renderer", lambda _: "192.168.1.5")
    session = CastSession(proxy, lambda _: client)

    session.start(renderer, 0, "https://cdn.example/video.mp4", "第一集", {}, None, False)

    with pytest.raises(RuntimeError, match="endpoint cleanup failed"):
        session.stop()

    session.stop()
    assert stop_attempts[0] == 2


def test_complete_startup_order_with_unified_events(monkeypatch, renderer):
    events = []
    proxy = FakeProxy(events=events)
    calls = []
    client = FakeClient(calls, events=events)
    monkeypatch.setattr("framework.cast_session._local_ip_for_renderer", lambda _: "192.168.1.5")
    session = CastSession(proxy, lambda _: client)

    session.start(renderer, 0, "https://cdn.example/video.mp4", "第一集", {}, None, False)

    expected_order = [
        ("start_lan_endpoint", "192.168.1.5"),
        ("build_url", "https://cdn.example/video.mp4"),
        ("set_av_transport_uri", "http://192.168.1.10:4321/s/token", "第一集", "video/mp4"),
        ("play",),
    ]

    for i, expected in enumerate(expected_order):
        assert events[i] == expected, f"Event {i}: expected {expected}, got {events[i]}"


def test_set_episode_build_url_failure_does_not_affect_state(monkeypatch, renderer):
    proxy = FakeProxy()
    calls = []
    client = FakeClient(calls)
    monkeypatch.setattr("framework.cast_session._local_ip_for_renderer", lambda _: "192.168.1.5")
    session = CastSession(proxy, lambda _: client)

    session.start(renderer, 0, "https://cdn.example/first.mp4", "第一集", {}, None, False)
    old_index = session.current_episode_index
    old_url = session._current_url

    original_build_url = proxy.build_url
    def failing_build_url(*args, **kwargs):
        raise RuntimeError("build url failed")

    proxy.build_url = failing_build_url

    with pytest.raises(RuntimeError, match="build url failed"):
        session.set_episode(1, "https://cdn.example/second.mp4", "第二集", {}, None, False)

    assert session.current_episode_index == old_index
    assert session._current_url == old_url


def test_set_episode_uri_success_play_failure_full_recovery(monkeypatch, renderer):
    events = []
    proxy = FakeProxy(events=events)
    calls = []

    class PartialFailClient(FakeClient):
        def __init__(self, calls, events):
            super().__init__(calls, events=events)
            self.set_episode_started = False

        def set_av_transport_uri(self, uri, title, mime):
            super().set_av_transport_uri(uri, title, mime)
            if self.set_episode_started:
                pass

        def play(self):
            if self.set_episode_started:
                self.calls.append(("play",))
                if self.events is not self.calls:
                    self.events.append(("play", "after_uri_set"))
                raise RuntimeError("play after new uri failed")
            super().play()

    client = PartialFailClient(calls, events=events)
    monkeypatch.setattr("framework.cast_session._local_ip_for_renderer", lambda _: "192.168.1.5")
    session = CastSession(proxy, lambda _: client)

    session.start(renderer, 0, "https://cdn.example/first.mp4", "第一集", {}, None, False)
    old_url = session._current_url
    old_index = session.current_episode_index
    proxy.next_url = "http://192.168.1.10:4321/s/new_token"

    client.set_episode_started = True

    with pytest.raises(RuntimeError, match="play after new uri failed"):
        session.set_episode(1, "https://cdn.example/second.mp4", "第二集", {}, None, False)

    assert session.current_episode_index == old_index
    assert session._current_url == old_url
    assert "new_token" in proxy.release_calls
    assert ("set_av_transport_uri", old_url, "第一集", "video/mp4") in calls
