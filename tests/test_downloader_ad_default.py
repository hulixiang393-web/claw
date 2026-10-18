# -*- coding: utf-8 -*-
"""下载路径 m3u8 广告过滤默认启用（无 ad_block 源也生效）。"""
from pathlib import Path

from framework.downloader import Downloader


_AD_M3U8 = (
    "#EXTM3U\n"
    "#EXT-X-CUE-OUT\n"
    "#EXTINF:5.0,\n/seg/ad1.ts\n"
    "#EXTINF:5.0,\n/seg/ad2.ts\n"
    "#EXT-X-CUE-IN\n"
    "#EXTINF:10.0,\n/seg/001.ts\n"
    "#EXT-X-ENDLIST\n"
)


class _FakeHttp:
    def get_text(self, url, headers=None, timeout=0, retries=0):
        return _AD_M3U8


class _NoAdBlockSource:
    raw = {"content_type": "video"}

    def request_headers(self):
        return {"Referer": "https://src.example/"}


class _DisabledSource:
    raw = {"ad_block": {"enabled": False}, "content_type": "video"}

    def request_headers(self):
        return {"Referer": "https://src.example/"}


def test_filter_m3u8_for_download_default_on(tmp_path):
    d = Downloader(content=None, http=_FakeHttp(), settings=None)
    out = d._filter_m3u8_for_download(
        _NoAdBlockSource(), "https://cdn.example.com/hls/i.m3u8", tmp_path
    )
    assert out is not None
    txt = out.read_text(encoding="utf-8")
    assert "ad1.ts" not in txt and "ad2.ts" not in txt
    assert "001.ts" in txt
    out.unlink()


def test_filter_m3u8_for_download_respects_disabled(tmp_path):
    d = Downloader(content=None, http=_FakeHttp(), settings=None)
    out = d._filter_m3u8_for_download(
        _DisabledSource(), "https://cdn.example.com/hls/i.m3u8", tmp_path
    )
    assert out is None


def test_filter_manifest_uses_unique_ffmpeg_uris_and_merge_cleans_it(tmp_path, monkeypatch):
    source = _NoAdBlockSource()
    source.request_headers = lambda: {"Referer": "https://src.example/"}
    d = Downloader(content=None, http=_FakeHttp(), settings=None)
    first = d._filter_m3u8_for_download(
        source, "https://cdn.example.com/hls/i.m3u8", tmp_path
    )
    second = d._filter_m3u8_for_download(
        source, "https://cdn.example.com/hls/i.m3u8", tmp_path
    )
    assert first is not None and second is not None and first != second
    assert "/seg/001.ts" in first.read_text(encoding="utf-8")
    first.unlink()
    second.unlink()

    class _FailingMerger:
        def __init__(self, **kwargs):
            pass

        def merge(self, video, audio, path, **kwargs):
            Path(path).write_bytes(b"partial")
            raise RuntimeError("merge failed")

    monkeypatch.setattr("framework.ffmpeg_merger.FFmpegMerger", _FailingMerger)
    task = type("Task", (), {"image_progress_cb": None, "cancel_evt": None, "pause_evt": None})()
    try:
        d._merge_video_ffmpeg(
            source, task, tmp_path / "out.mp4", "https://cdn.example.com/hls/i.m3u8", "",
        )
    except RuntimeError:
        pass
    assert not list(tmp_path.glob(".claw_clean_*.m3u8"))


def test_merge_failure_falls_back_to_original_stream_without_partial(tmp_path, monkeypatch):
    class _Source:
        source_id = "other"
        raw = {"content_type": "video"}

        def request_headers(self):
            return {}

    class _Content:
        def fetch_video_streams(self, source, url, quality=""):
            return "https://cdn.example.com/stream", "https://cdn.example.com/audio"

    class _Settings:
        def get(self, section, key, default=None):
            if section == "download" and key == "output_dir":
                return str(tmp_path)
            return default

    d = Downloader(_Content(), _FakeHttp(), _Settings())
    original = "https://cdn.example.com/stream"
    calls = []

    def _fail_merge(source, task, path, video, audio, provider=""):
        Path(path).write_bytes(b"partial")
        raise RuntimeError("merge failed")

    monkeypatch.setattr(d, "_merge_video_ffmpeg", _fail_merge)
    monkeypatch.setattr(
        d, "_download_via_ytdlp",
        lambda video, path, fmt, progress, cancel, pause: calls.append(video) or 7,
    )
    task = type("Task", (), {
        "title": "test", "source_id": "other", "content_type": "video", "quality": "", "image_progress_cb": None,
        "cancel_evt": None, "pause_evt": None,
    })()
    chapter = type("Chapter", (), {"url": "https://site/episode", "title": "episode", "provider": ""})()
    assert d._download_video(_Source(), task, chapter, 0) == 7
    assert calls == [original]
    assert not (tmp_path / "out.mp4").exists()
