from framework.media_tuner import classify


def test_default_hls_buffer_is_vlc_maximum():
    profile = classify("https://cdn.example/video/index.m3u8")
    assert profile.kind == "hls"
    assert profile.buffer_ms == 60000
    assert profile.max_buffer_ms == 60000
