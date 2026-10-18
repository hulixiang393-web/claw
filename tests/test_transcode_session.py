import subprocess
import threading
import time
from pathlib import Path

import pytest

from framework.transcode_session import TranscodeError, TranscodeSession


class FakeProcess:
    def __init__(self, command, create_output=False):
        self.command = command
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self._done = threading.Event()
        if create_output:
            output_dir = Path(command[command.index("-hls_segment_filename") + 1]).parent

            def write_output():
                time.sleep(0.02)
                (output_dir / "index.m3u8").write_text("#EXTM3U\n#EXTINF:2,\nsegment00000.ts\n", encoding="utf-8")
                (output_dir / "segment00000.ts").write_bytes(b"segment")

            threading.Thread(target=write_output, daemon=True).start()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15
        self._done.set()

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        self._done.set()

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode


def test_start_builds_h264_aac_hls_command_and_waits_for_first_segment(monkeypatch, tmp_path):
    processes = []

    def fake_popen(command, **kwargs):
        process = FakeProcess(command, create_output=True)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    session = TranscodeSession(ffmpeg_path="ffmpeg-test", temp_root=tmp_path, startup_timeout=1)

    playlist_url = session.start(
        "https://example.test/video.m3u8?token=secret-token",
        {"Referer": "https://example.test/page?token=header-token", "Cookie": "sid=cookie-secret"},
        "http://192.168.1.5:4321/transcode/abc",
    )

    command = processes[0].command
    assert playlist_url == "http://192.168.1.5:4321/transcode/abc/index.m3u8"
    assert ["-c:v", "libx264"] == command[command.index("-c:v") : command.index("-c:v") + 2]
    assert ["-c:a", "aac"] == command[command.index("-c:a") : command.index("-c:a") + 2]
    assert ["-hls_time", "2"] == command[command.index("-hls_time") : command.index("-hls_time") + 2]
    assert "-hls_flags" in command and "independent_segments" in command
    assert "-headers" in command
    header_value = command[command.index("-headers") + 1]
    assert "Referer: https://example.test/page?token=header-token" in header_value
    assert "Cookie: sid=cookie-secret" in header_value
    assert session.playlist_path is not None
    session.stop()


def test_start_timeout_terminates_process_and_cleans_directory(monkeypatch, tmp_path):
    processes = []

    def fake_popen(command, **kwargs):
        process = FakeProcess(command)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    session = TranscodeSession(ffmpeg_path="ffmpeg-test", temp_root=tmp_path, startup_timeout=0.03)

    with pytest.raises(TranscodeError, match="timed out"):
        session.start("https://example.test/live.m3u8?token=secret", {}, "http://127.0.0.1:1/transcode/x")

    assert processes[0].terminate_calls == 1
    assert list(tmp_path.iterdir()) == []
    assert session.playlist_path is None


def test_stop_is_idempotent_and_cleans_directory(monkeypatch, tmp_path):
    process = None

    def fake_popen(command, **kwargs):
        nonlocal process
        process = FakeProcess(command, create_output=True)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    session = TranscodeSession(ffmpeg_path="ffmpeg-test", temp_root=tmp_path, startup_timeout=1)
    session.start("https://example.test/live.m3u8", {}, "http://127.0.0.1:1/transcode/x")

    session.stop()
    session.stop()

    assert process.terminate_calls == 1
    assert list(tmp_path.iterdir()) == []
    assert session.diagnostics["state"] == "stopped"


def test_diagnostics_redact_urls_tokens_and_headers(monkeypatch, tmp_path, capsys):
    def fake_popen(command, **kwargs):
        return FakeProcess(command)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    session = TranscodeSession(ffmpeg_path="ffmpeg-test", temp_root=tmp_path, startup_timeout=0.02)

    with pytest.raises(TranscodeError):
        session.start(
            "https://example.test/live.m3u8?token=source-secret",
            {"Authorization": "Bearer auth-secret", "Cookie": "sid=cookie-secret"},
            "http://127.0.0.1:1/transcode/secret-path",
        )

    output = repr(session.diagnostics) + capsys.readouterr().out
    for secret in ("source-secret", "auth-secret", "cookie-secret", "secret-path"):
        assert secret not in output
    assert "example.test" not in output
    assert "Authorization" not in output
