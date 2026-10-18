from pathlib import Path

import pytest

from framework.dlna import DlnaClient, DlnaError
from tools.generate_cast_samples import generate_sample_set
from tools.virtual_dlna_renderer import VirtualMediaFetcher, VirtualRenderer, find_ffmpeg, start_media_server


PYTHON = r"C:\Users\alonely\AppData\Local\Programs\Python\Python310\python.exe"


def _ffmpeg_or_skip():
    try:
        return find_ffmpeg()
    except FileNotFoundError as exc:
        pytest.skip(str(exc))


def test_find_ffmpeg_rejects_missing_explicit_path():
    with pytest.raises(FileNotFoundError):
        find_ffmpeg(r"C:\missing\ffmpeg.exe")


def test_fetch_records_http_metadata(tmp_path):
    sample = tmp_path / "sample.mp4"
    sample.write_bytes(b"not a real media file")
    server, base = start_media_server(tmp_path)
    try:
        result = VirtualMediaFetcher(None).fetch(f"{base}/sample.mp4")
    finally:
        server.shutdown()
        server.server_close()

    assert result["http_status"] == 200
    assert result["content_type"].startswith("video/mp4")
    assert result["first_byte_ms"] >= 0
    assert result["bytes"] == len(b"not a real media file")


def test_probe_and_decode_report_media_codecs(tmp_path):
    ffmpeg_path = _ffmpeg_or_skip()
    samples = generate_sample_set(tmp_path / "samples", ffmpeg_path)
    sample = samples["mp4"]
    assert sample.exists()

    server, base = start_media_server(sample.parent)
    try:
        result = VirtualMediaFetcher(ffmpeg_path).decode(f"{base}/{sample.name}")
    finally:
        server.shutdown()
        server.server_close()

    assert result["http_status"] == 200
    assert result["decoded"] is True
    assert result["container"] == "mov,mp4,m4a,3gp,3g2,mj2"
    assert result["video_codec"] == "h264"
    assert result["audio_codec"] == "aac"
    assert result["duration"] > 0


def test_invalid_media_reports_nonzero_and_stderr_summary(tmp_path):
    ffmpeg_path = _ffmpeg_or_skip()
    invalid = tmp_path / "broken.mp4"
    invalid.write_bytes(b"broken")
    server, base = start_media_server(tmp_path)
    try:
        result = VirtualMediaFetcher(ffmpeg_path).decode(f"{base}/{invalid.name}")
    finally:
        server.shutdown()
        server.server_close()

    assert result["decoded"] is False
    assert result["returncode"] != 0
    assert result["stderr_summary"]
    assert "broken.mp4" not in result["stderr_summary"]


def test_virtual_renderer_runs_real_media_and_reports_transport_state(tmp_path):
    ffmpeg_path = _ffmpeg_or_skip()
    samples = generate_sample_set(tmp_path / "samples", ffmpeg_path)
    media_server, base = start_media_server(samples["mp4"].parent)
    renderer = VirtualRenderer(ffmpeg_path=ffmpeg_path, fetch_timeout=10.0)
    try:
        descriptor = renderer.start()
        client = DlnaClient(descriptor)
        uri = f"{base}/{samples['mp4'].name}"

        client.set_av_transport_uri(uri, "sample", "video/mp4")
        assert client.get_transport_info()["state"] == "TRANSITIONING"
        client.play()
        assert client.get_transport_info()["state"] == "PLAYING"
        assert client.get_position_info()["duration"] > 0

        client.pause()
        assert client.get_transport_info()["state"] == "PAUSED_PLAYBACK"
        client.seek(1)
        assert client.get_position_info()["position"] == 1
        client.play()
        assert client.get_transport_info()["state"] == "PLAYING"
        client.stop()
        assert client.get_transport_info()["state"] == "STOPPED"
    finally:
        renderer.stop()
        media_server.shutdown()
        media_server.server_close()


def test_virtual_renderer_rejects_media_that_cannot_be_decoded(tmp_path):
    ffmpeg_path = _ffmpeg_or_skip()
    invalid = tmp_path / "private-token.mp4"
    invalid.write_bytes(b"broken")
    media_server, base = start_media_server(tmp_path)
    renderer = VirtualRenderer(ffmpeg_path=ffmpeg_path, fetch_timeout=10.0)
    uri = f"{base}/{invalid.name}?token=secret"
    try:
        client = DlnaClient(renderer.start())
        client.set_av_transport_uri(uri, "broken", "video/mp4")
        with pytest.raises(DlnaError) as exc_info:
            client.play()

        assert client.get_transport_info()["state"] == "ERROR"
        assert "private-token.mp4" not in str(exc_info.value)
        assert "token=secret" not in str(exc_info.value)
    finally:
        renderer.stop()
        media_server.shutdown()
        media_server.server_close()
