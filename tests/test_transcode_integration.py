from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

from framework.dlna import DlnaClient
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


def test_transcoded_hls_is_served_over_lan_and_decoded_by_virtual_renderer(tmp_path):
    ffmpeg_path = _ffmpeg_or_skip()
    samples = generate_sample_set(tmp_path / "samples", ffmpeg_path)
    source_server, source_base = start_media_server(samples["mp4"].parent)
    proxy = MediaProxy(cache=_OffCache())
    renderer = VirtualRenderer(ffmpeg_path=ffmpeg_path, fetch_timeout=20.0)
    session = TranscodeSession(
        ffmpeg_path=ffmpeg_path,
        temp_root=tmp_path,
        startup_timeout=20.0,
        media_proxy=proxy,
    )
    try:
        lan_base = proxy.start_lan_endpoint("127.0.0.1")
        local_url = proxy.build_url(f"{source_base}/{samples['mp4'].name}")
        playlist_url = session.start(local_url, {}, lan_base)

        playlist = requests.get(playlist_url, timeout=10)
        assert playlist.status_code == 200
        assert "#EXTM3U" in playlist.text
        segment_name = next(line for line in playlist.text.splitlines() if line.endswith(".ts"))
        segment_url = f"{playlist_url.rsplit('/', 1)[0]}/{segment_name}"
        segment = requests.get(segment_url, timeout=10)
        assert segment.status_code == 200
        assert segment.content

        client = DlnaClient(renderer.start())
        client.set_av_transport_uri(playlist_url, "transcoded", "application/vnd.apple.mpegurl")
        client.play()
        assert client.get_transport_info()["state"] == "PLAYING"
        assert renderer.diagnostics()["decoded"] is True
    finally:
        renderer.stop()
        session.stop()
        proxy.stop_lan_endpoint()
        proxy.stop()
        source_server.shutdown()
        source_server.server_close()


def test_transcode_directory_rejects_traversal_and_requires_active_lan(tmp_path):
    proxy = MediaProxy(cache=_OffCache())
    directory = tmp_path / "hls"
    directory.mkdir()
    (directory / "index.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="public_base"):
            proxy.register_transcode_directory(directory, "http://127.0.0.1:1")
        lan_base = proxy.start_lan_endpoint("127.0.0.1")
        transcode_url = proxy.register_transcode_directory(directory, lan_base)
        assert requests.get(f"{transcode_url}/index.m3u8", timeout=10).status_code == 200
        token = urlsplit(transcode_url).path.split("/")[2]
        traversal = f"{lan_base}/t/{token}/%2e%2e/secret.txt"
        assert requests.get(traversal, timeout=10).status_code == 404
    finally:
        proxy.stop_lan_endpoint()
        proxy.stop()


def test_lan_stop_invalidates_transcode_url_but_keeps_local_token(tmp_path):
    proxy = MediaProxy(cache=_OffCache())
    directory = tmp_path / "hls"
    directory.mkdir()
    (directory / "index.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    try:
        local_url = proxy.build_url("https://example.test/local.m3u8")
        lan_base = proxy.start_lan_endpoint("127.0.0.1")
        transcode_url = proxy.register_transcode_directory(directory, lan_base)
        assert requests.get(f"{transcode_url}/index.m3u8", timeout=10).status_code == 200
        transcode_token = urlsplit(transcode_url).path.split("/")[2]
        local_token = urlsplit(local_url).path.rsplit("/", 1)[-1]

        proxy.stop_lan_endpoint()

        assert transcode_token not in proxy._transcode_dirs
        assert local_token in proxy._tokens
    finally:
        proxy.stop_lan_endpoint()
        proxy.stop()
