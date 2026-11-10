# -*- coding: utf-8 -*-
"""外部播放器播放列表：m3u 生成、mpv 起始项、App 的下一集交给播放器。"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.desktop_player import Prefs, build_argv
from framework.player_session import PlayerSession

ENTRIES = [
    ("http://127.0.0.1:1/e/key/0", "第一集"),
    ("http://127.0.0.1:1/e/key/1", "第二集"),
]


def _prefs(key):
    return Prefs(player=key, player_path="")


def test_mpv_keeps_full_playlist_and_sets_start(monkeypatch):
    monkeypatch.setattr("framework.desktop_player.resolve",
                        lambda preferred, explicit="": ("mpv", "mpv.exe"))
    session = PlayerSession()
    try:
        path, start = session._prepare_playlist(ENTRIES, 1, _prefs("mpv"))
        assert path and start == 1
        assert session.has_playlist
        text = Path(path).read_text(encoding="utf-8")
        assert "#EXTM3U" in text
        assert "/e/key/0" in text and "/e/key/1" in text
        assert "#EXTINF:-1,第一集" in text
    finally:
        session.close()
    assert not session.has_playlist


def test_non_mpv_trims_playlist_to_current_episode(monkeypatch):
    monkeypatch.setattr("framework.desktop_player.resolve",
                        lambda preferred, explicit="": ("vlc", "vlc.exe"))
    session = PlayerSession()
    try:
        path, start = session._prepare_playlist(ENTRIES, 1, _prefs("vlc"))
        assert start == 0
        text = Path(path).read_text(encoding="utf-8")
        assert "/e/key/0" not in text
        assert "/e/key/1" in text
    finally:
        session.close()


def test_build_argv_playlist_start_only_for_mpv():
    mpv = build_argv("mpv", "mpv.exe", "episodes.m3u", playlist_start=2)
    assert "--playlist-start=2" in mpv
    vlc = build_argv("vlc", "vlc.exe", "episodes.m3u", playlist_start=2)
    assert not any(arg.startswith("--playlist-start") for arg in vlc)


# --------------------------------------------------------------------------- #
# VideoView：播放列表存在时「下一集」交给播放器，不现场取流
# --------------------------------------------------------------------------- #
def _view(n=3):
    from gui.pages.reader.video_view import VideoView
    from framework.content import Chapter, Detail

    detail = Detail(
        source_id="demo", content_type="video", url="http://example.com/v/1",
        title="work", cover="", chapters=[
            Chapter(f"ep{i + 1}", f"http://e/{i}", cover="") for i in range(n)
        ],
    )
    view = VideoView(None)
    view._detail = detail
    view._episodes = detail.chapters
    view._source = SimpleNamespace(source_id="demo", content_type="video")
    return view


class _PlaylistPlayer:
    has_playlist = True

    def __init__(self):
        self.steps = []

    def playlist_next(self):
        self.steps.append("next")
        return True

    def playlist_prev(self):
        self.steps.append("prev")
        return True


def test_next_episode_uses_player_playlist(_qapp, monkeypatch):
    view = _view()
    view._current_idx = 0
    view._player = _PlaylistPlayer()
    monkeypatch.setattr(view, "_select_episode",
                        lambda idx: pytest_fail("不应现场取流"))
    view._on_next_ep()
    assert view._player.steps == ["next"]
    view._current_idx = 1
    view._on_prev_ep()
    assert view._player.steps == ["next", "prev"]


def test_media_ended_defers_to_player_playlist(_qapp, monkeypatch):
    view = _view()
    view._current_idx = 0
    view._player = _PlaylistPlayer()
    monkeypatch.setattr(view, "_select_episode",
                        lambda idx: pytest_fail("播放器会自动前进"))
    view._on_media_ended()


def test_series_played_updates_current_episode(_qapp):
    view = _view()
    view._current_idx = 0
    emitted = []
    view.episode_changed.connect(lambda payload: emitted.append(payload))
    view._on_series_played(2)
    assert view._current_idx == 2
    assert emitted and emitted[0][2] == "http://e/2"


def pytest_fail(message):
    raise AssertionError(message)


def test_local_video_hands_playlist_and_follows_next(_qapp, tmp_path, monkeypatch):
    """本地多集：整份选集列表交给外播（可直接点下一集），切集后进度跟到新集。"""
    import framework.player_session as _ps
    from framework.reading_progress import ReadingProgress
    from gui.app import MainWindow

    class _Sig:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def emit(self, *args):
            for slot in self.slots:
                slot(*args)

    class _FakePlayer:
        instances = []

        def __init__(self, *a, **kw):
            self.time_changed = _Sig()
            self.length_changed = _Sig()
            self.state_changed = _Sig()
            self.playlist_pos_changed = _Sig()
            self.calls = []
            self.time = 0
            self.length = 900_000
            _FakePlayer.instances.append(self)

        def play(self, url, audio_url="", title="", kind="",
                 network_caching_ms=0, start_s=0, playlist=None,
                 playlist_index=0):
            self.calls.append({"url": url, "playlist": list(playlist or []),
                               "index": playlist_index, "start": start_s})
            self.state_changed.emit(3)

        def get_length(self):
            return self.length

        def get_time(self):
            return self.time

        def close(self):
            pass

    clock = [0.0]
    monkeypatch.setattr(_ps, "PlayerSession", _FakePlayer)
    monkeypatch.setattr(
        "gui.app.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    monkeypatch.setattr(MainWindow, "_pick_local_episode",
                        lambda self, names, cur: 0)

    folder = tmp_path / "本地视频"
    folder.mkdir()
    paths = [str(folder / f"第{i}集.mp4") for i in (1, 2, 3)]
    progress = ReadingProgress(tmp_path / "progress.json")
    window = SimpleNamespace(reading_progress=progress)
    window._on_local_progress = lambda payload: MainWindow._on_local_progress(window, payload)
    rec = {"key": str(folder), "episode_paths": paths, "title": "本地视频", "url": "book"}
    MainWindow._play_local_video(window, rec)

    player = _FakePlayer.instances[0]
    call = player.calls[0]
    assert len(call["playlist"]) == 3
    assert call["index"] == 0
    assert call["playlist"][1][0].startswith("file://")
    assert unquote(call["playlist"][1][0]).endswith("第2集.mp4")

    # 播放器里点「下一集」→ playlist-pos 1：当前集换成第 2 集并落盘
    player.playlist_pos_changed.emit(1)
    assert progress.resume(str(folder))["chapter_url"].endswith("第2集.mp4")

    # 切集冷却期内的旧位置回写被丢弃（不污染新集进度）
    player.time = 100_000
    player.time_changed.emit(100_000)
    assert progress.resume(str(folder))["location"] is None

    # 冷却期过后正常回写第 2 集位置
    clock[0] = 2.0
    player.time = 50_000
    player.time_changed.emit(50_000)
    resumed = progress.resume(str(folder))
    assert resumed["chapter_url"].endswith("第2集.mp4")
    assert resumed["location"]["seconds"] == 50


# --------------------------------------------------------------------------- #
# 续播点：mpv 的 --start 是「逐文件」选项
# --------------------------------------------------------------------------- #
class _FakeIPC:
    instances: list = []

    def __init__(self, server, on_event):
        self.server = server
        self.sent = []
        self.ok = True
        _FakeIPC.instances.append(self)

    def connect(self, timeout=0):
        return True

    def send(self, command):
        self.sent.append(list(command))
        return len(self.sent)

    def close(self):
        self.ok = False


class _FakeProc:
    pid = 4243

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        return None


def test_resume_point_is_seeked_instead_of_passed_as_start(
        _qapp, monkeypatch):
    """有播放列表时续播点必须走 IPC seek，不能交给 --start。

    2026-11-04 实测（mpv v0.41，`--start=4` + 两项列表）：第 1 项从 4s
    起播，`playlist-next` 之后第 2 项**也从 4s 起播** —— `start` 是逐文件
    选项。续到 20 分钟再看下一集，就等于每集都被切掉开头 20 分钟。
    """
    captured = {}
    _FakeIPC.instances = []
    monkeypatch.setattr("framework.player_session._MpvIPC", _FakeIPC)
    monkeypatch.setattr("framework.desktop_player.resolve",
                        lambda preferred, explicit="": ("mpv", "mpv.exe"))
    monkeypatch.setattr(
        "framework.player_session.desktop_player.launch",
        lambda url, **kw: (
            captured.update(kw),
            SimpleNamespace(key="mpv", name="mpv", exe="mpv.exe", argv=[],
                            process=_FakeProc(),
                            ipc_server=kw.get("ipc_server", "")),
        )[1])
    monkeypatch.setattr("framework.player_session.desktop_player.load_prefs",
                        lambda *a, **k: Prefs(player="mpv", player_path=""))
    monkeypatch.setattr(PlayerSession, "_hold_proxy_lease",
                        lambda self, proc: None)

    session = PlayerSession()
    session.play("http://127.0.0.1:1/e/key/0", start_s=1200,
                 playlist=ENTRIES, playlist_index=1)

    assert captured["start_s"] == 0, "列表模式不能把续播点塞进 --start"
    assert captured["playlist_start"] == 1
    assert session.has_playlist
    assert [c for c in _FakeIPC.instances[-1].sent if c[:1] == ["seek"]] == []

    session._on_ipc_event({"event": "file-loaded"})
    asserts = [c for c in _FakeIPC.instances[-1].sent if c[:1] == ["seek"]]
    assert asserts == [["seek", 1200, "absolute"]], "起播后对当前集补一次绝对 seek"

    # 下一集（新的 file-loaded）不能再把续播点应用一次
    session._on_ipc_event({"event": "start-file"})
    session._on_ipc_event({"event": "file-loaded"})
    asserts = [c for c in _FakeIPC.instances[-1].sent if c[:1] == ["seek"]]
    assert asserts == [["seek", 1200, "absolute"]]


def test_non_ipc_player_keeps_start_argument(_qapp, monkeypatch):
    """VLC/PotPlayer 没有回执通道：续播点仍交给它们的 --start-time / /seek。"""
    captured = {}
    monkeypatch.setattr("framework.player_session._MpvIPC", _FakeIPC)
    monkeypatch.setattr("framework.desktop_player.resolve",
                        lambda preferred, explicit="": ("vlc", "vlc.exe"))
    monkeypatch.setattr(
        "framework.player_session.desktop_player.launch",
        lambda url, **kw: (
            captured.update(kw),
            SimpleNamespace(key="vlc", name="VLC", exe="vlc.exe", argv=[],
                            process=_FakeProc(), ipc_server=""),
        )[1])
    monkeypatch.setattr("framework.player_session.desktop_player.load_prefs",
                        lambda *a, **k: Prefs(player="vlc", player_path=""))
    monkeypatch.setattr(PlayerSession, "_hold_proxy_lease",
                        lambda self, proc: None)

    session = PlayerSession()
    session.play("http://127.0.0.1:1/e/key/0", start_s=600,
                 playlist=ENTRIES, playlist_index=0)
    assert captured["start_s"] == 600, "没有 IPC 的播放器只能靠启动参数定位"
    assert session._pending_resume_s == 0
