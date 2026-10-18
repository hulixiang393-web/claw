from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from framework.dlna import DlnaClient, DlnaError
from framework.media_proxy import MediaProxy
from framework.transcode_session import TranscodeSession
from tools.generate_cast_samples import generate_sample_set
from tools.virtual_dlna_renderer import VirtualRenderer, find_ffmpeg, start_media_server


class _OffCache:
    enabled = False


def _ffmpeg_or_skip():
    try:
        return find_ffmpeg()
    except FileNotFoundError as exc:
        pytest.skip(str(exc))


def test_incompatible_webm_falls_back_to_h264_aac_hls_and_reaches_playing(tmp_path):
    ffmpeg_path = _ffmpeg_or_skip()
    samples = generate_sample_set(tmp_path / "samples", ffmpeg_path)
    webm = samples.get("webm")
    if webm is None:
        pytest.skip((samples.get("capability") or {}).get("webm", "WebM fixture unavailable"))

    source_server, source_base = start_media_server(webm.parent)
    (tmp_path / "transcode").mkdir()
    proxy = MediaProxy(cache=_OffCache())
    renderer = VirtualRenderer(
        ffmpeg_path=ffmpeg_path,
        fetch_timeout=30.0,
        supported_video_codecs={"h264"},
        supported_audio_codecs={"aac"},
    )
    session = TranscodeSession(
        ffmpeg_path=ffmpeg_path,
        temp_root=tmp_path / "transcode",
        startup_timeout=30.0,
        media_proxy=proxy,
    )
    original_url = f"{source_base}/{webm.name}"
    try:
        renderer.start()
        client = DlnaClient(renderer._descriptor(), timeout=30.0)
        client.set_av_transport_uri(original_url, "original", "video/webm")
        with pytest.raises(DlnaError, match="Play failed"):
            client.play()
        assert renderer.state()["state"] == "ERROR"
        original_result = renderer.diagnostics()

        lan_base = proxy.start_lan_endpoint("127.0.0.1")
        started = time.perf_counter()
        playlist_url = session.start(original_url, {}, lan_base)
        first_playlist_ms = (time.perf_counter() - started) * 1000
        client.set_av_transport_uri(playlist_url, "transcoded", "application/vnd.apple.mpegurl")
        client.play()
        playing_ms = (time.perf_counter() - started) * 1000

        state = client.get_transport_info()
        diagnostics = renderer.diagnostics()
        result = {
            "original": original_result,
            "transcode": {
                "session": session.diagnostics,
                "renderer": diagnostics,
                "first_playlist_ms": round(first_playlist_ms, 1),
                "playing_ms": round(playing_ms, 1),
            },
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        assert state["state"] == "PLAYING"
        assert diagnostics["decoded"] is True
        assert diagnostics["video_codec"] == "h264"
        assert diagnostics["audio_codec"] == "aac"
        assert session.diagnostics["startup_time"] is not None
    finally:
        renderer.stop()
        session.stop()
        proxy.stop_lan_endpoint()
        proxy.stop()
        source_server.shutdown()
        source_server.server_close()

    assert list((tmp_path / "transcode").iterdir()) == []


def test_compatible_mp4_does_not_require_transcode(tmp_path):
    ffmpeg_path = _ffmpeg_or_skip()
    samples = generate_sample_set(tmp_path / "samples", ffmpeg_path)
    mp4 = samples.get("mp4")
    if mp4 is None:
        pytest.skip((samples.get("capability") or {}).get("mp4", "MP4 fixture unavailable"))

    source_server, source_base = start_media_server(mp4.parent)
    renderer = VirtualRenderer(
        ffmpeg_path=ffmpeg_path,
        fetch_timeout=30.0,
        supported_video_codecs={"h264"},
        supported_audio_codecs={"aac"},
    )
    try:
        renderer.start()
        client = DlnaClient(renderer._descriptor(), timeout=30.0)
        client.set_av_transport_uri(f"{source_base}/{mp4.name}", "original", "video/mp4")
        client.play()
        assert client.get_transport_info()["state"] == "PLAYING"
        assert renderer.diagnostics()["video_codec"] == "h264"
    finally:
        renderer.stop()
        source_server.shutdown()
        source_server.server_close()
