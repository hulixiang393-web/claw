"""外部播放器层（framework/desktop_player.py）：探测/选择/命令行/拉起。"""
from __future__ import annotations

import json
import sys

import pytest

from framework import desktop_player as dp


# --------------------------------------------------------------------------- #
# 夹具：可控的「已安装播放器」
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_players(tmp_path, monkeypatch):
    """在 tmp 目录造两个假 exe，并把 PLAYERS 收窄成 mpv/vlc 两项。"""
    mpv_dir = tmp_path / "MPV Player"
    vlc_dir = tmp_path / "VLC"
    mpv_dir.mkdir()
    vlc_dir.mkdir()
    mpv = mpv_dir / "mpv.exe"
    vlc = vlc_dir / "vlc.exe"
    mpv.write_bytes(b"MZ")
    vlc.write_bytes(b"MZ")

    specs = (
        dp.PlayerSpec(key="mpv", name="mpv", exe_names=("mpv.exe",),
                      win_dirs=(str(mpv_dir),), exe_in_dir=("mpv.exe",)),
        dp.PlayerSpec(key="vlc", name="VLC", exe_names=("vlc.exe",),
                      win_dirs=(str(vlc_dir),), exe_in_dir=("vlc.exe",)),
    )
    monkeypatch.setattr(dp, "PLAYERS", specs)
    monkeypatch.setattr(dp.shutil, "which", lambda name: None)
    return {"mpv": str(mpv), "vlc": str(vlc)}


# --------------------------------------------------------------------------- #
# 探测与选择
# --------------------------------------------------------------------------- #
def test_detect_finds_both_players(fake_players):
    found = dp.detect()
    assert found == fake_players


def test_detect_skips_missing_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(dp.shutil, "which", lambda name: None)
    monkeypatch.setattr(dp, "PLAYERS", (
        dp.PlayerSpec(key="mpv", name="mpv", exe_names=("mpv.exe",),
                      win_dirs=(str(tmp_path / "nope"),), exe_in_dir=("mpv.exe",)),
    ))
    assert dp.detect() == {}


def test_auto_prefers_mpv_over_vlc(fake_players):
    """默认优先级：mpv 在前（VLC 正是用户觉得卡的元凶）。"""
    assert dp.resolve("auto", "") == ("mpv", fake_players["mpv"])


def test_explicit_preference_is_honoured(fake_players):
    assert dp.resolve("vlc", "") == ("vlc", fake_players["vlc"])


def test_unknown_preference_falls_back_to_detected(fake_players):
    assert dp.resolve("potplayer", "")[0] == "mpv"


def test_resolve_without_any_player_returns_none(monkeypatch):
    monkeypatch.setattr(dp.shutil, "which", lambda name: None)
    monkeypatch.setattr(dp, "PLAYERS", ())
    assert dp.resolve("auto", "") == (None, None)


def test_explicit_path_wins(fake_players, tmp_path):
    custom = tmp_path / "PotPlayerMini64.exe"
    custom.write_bytes(b"MZ")
    key, exe = dp.resolve("auto", str(custom))
    assert exe == str(custom)
    assert key == "potplayer"          # 按文件名猜类型，决定参数风格


def test_bad_explicit_path_degrades_to_auto(fake_players):
    assert dp.resolve("auto", "D:/nope/none.exe")[0] == "mpv"


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
HDRS = {"Referer": "https://example.com/", "User-Agent": "UA/1.0"}
URL = "http://127.0.0.1:9/s/tok/index.m3u8"


def test_mpv_argv_has_anti_stutter_and_headers():
    argv = dp.build_argv("mpv", "mpv.exe", URL, headers=HDRS, title="第1集",
                         start_s=125, extra_args=["--volume=80"],
                         audio_url="http://127.0.0.1:9/s/tok/a.m4a")
    assert argv[0] == "mpv.exe"
    assert argv[-1] == URL                     # 媒体地址必须在最后
    assert "--referrer=https://example.com/" in argv
    assert "--user-agent=UA/1.0" in argv
    assert "--start=125" in argv
    assert "--volume=80" in argv
    assert "--audio-file=http://127.0.0.1:9/s/tok/a.m4a" in argv
    # 抗抖动参数：readahead 与缓存上限（VLC 卡的正是这一块）
    assert any(a.startswith("--demuxer-readahead-secs=") for a in argv)
    assert any(a.startswith("--demuxer-max-bytes=") for a in argv)
    assert "--cache=yes" in argv


def test_vlc_argv_uses_vlc_option_names():
    argv = dp.build_argv("vlc", "vlc.exe", URL, headers=HDRS, start_s=60,
                         audio_url="http://127.0.0.1:9/s/tok/a.m4a")
    assert "--http-referrer=https://example.com/" in argv
    assert "--http-user-agent=UA/1.0" in argv
    assert "--start-time=60" in argv
    assert "--input-slave=http://127.0.0.1:9/s/tok/a.m4a" in argv
    assert argv[-1] == URL


def test_potplayer_argv_style():
    argv = dp.build_argv("potplayer", "PotPlayerMini64.exe", URL, headers=HDRS,
                         title="第1集", start_s=30)
    assert argv[1] == URL
    assert "/referer=https://example.com/" in argv
    assert "/seek=30" in argv


def test_argv_rejects_empty_url():
    with pytest.raises(ValueError):
        dp.build_argv("mpv", "mpv.exe", "")


def test_unknown_player_falls_back_to_mpv_style():
    argv = dp.build_argv("custom", "weird.exe", URL, headers=HDRS)
    assert "--referrer=https://example.com/" in argv
    assert argv[-1] == URL


def test_empty_start_and_headers_are_omitted():
    argv = dp.build_argv("mpv", "mpv.exe", URL)
    assert not any(a.startswith("--start=") for a in argv)
    assert not any(a.startswith("--referrer=") for a in argv)
    assert argv[-1] == URL


# --------------------------------------------------------------------------- #
# 拉起
# --------------------------------------------------------------------------- #
def test_launch_spawns_built_argv(fake_players, monkeypatch):
    seen = {}
    monkeypatch.setattr(dp, "_spawn", lambda argv: seen.setdefault("argv", argv))
    launched = dp.launch(URL, title="第1集", start_s=5)
    assert launched.key == "mpv"
    assert launched.name == "mpv"
    assert seen["argv"] == launched.argv
    assert seen["argv"][-1] == URL


def test_launch_without_player_raises(monkeypatch):
    monkeypatch.setattr(dp.shutil, "which", lambda name: None)
    monkeypatch.setattr(dp, "PLAYERS", ())
    with pytest.raises(dp.PlayerNotFound):
        dp.launch(URL)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_load_prefs_reads_video_section(tmp_path):
    cfg = tmp_path / "app_config.json"
    cfg.write_text(json.dumps({"video": {
        "player": "potplayer", "player_path": "D:/p/PotPlayerMini64.exe",
        "player_extra_args": ["--volume=70"], "prefer_external": True,
        "external_fullscreen": True,
    }}), encoding="utf-8")
    prefs = dp.load_prefs(tmp_path)
    assert prefs.player == "potplayer"
    assert prefs.player_path.endswith("PotPlayerMini64.exe")
    assert prefs.extra_args == ("--volume=70",)
    assert prefs.prefer_external is True
    assert prefs.fullscreen is True


def test_load_prefs_defaults_when_section_missing(tmp_path):
    (tmp_path / "app_config.json").write_text("{}", encoding="utf-8")
    prefs = dp.load_prefs(tmp_path)
    assert prefs.player == "auto"
    assert prefs.prefer_external is False


def test_load_prefs_survives_broken_json(tmp_path):
    (tmp_path / "app_config.json").write_text("{ not json", encoding="utf-8")
    prefs = dp.load_prefs(tmp_path)
    assert prefs.player == "auto"      # 配置坏了也必须能继续播


def test_extra_args_accepts_single_string(tmp_path):
    (tmp_path / "app_config.json").write_text(json.dumps({"video": {
        "player_extra_args": "--volume=50"}}), encoding="utf-8")
    assert dp.load_prefs(tmp_path).extra_args == ("--volume=50",)


def test_spawn_detaches_without_console(monkeypatch):
    """拉起必须是分离进程：不占控制台、不随 App 退出被杀。"""
    captured = {}

    class FakePopen:
        pid = 4242

        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs

        def poll(self):
            return None

    monkeypatch.setattr(dp.subprocess, "Popen", FakePopen)
    proc = dp._spawn(["mpv.exe", "x.m3u8"])
    assert proc.pid == 4242
    assert captured["argv"] == ["mpv.exe", "x.m3u8"]
    kwargs = captured["kwargs"]
    assert kwargs["stdin"] is dp.subprocess.DEVNULL
    if sys.platform == "win32":
        assert kwargs["creationflags"] & dp.subprocess.DETACHED_PROCESS
    else:
        assert kwargs["start_new_session"] is True
