# -*- coding: utf-8 -*-
"""下载路径 m3u8 广告过滤默认启用（无 ad_block 源也生效）。"""
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


def test_filter_m3u8_for_download_respects_disabled(tmp_path):
    d = Downloader(content=None, http=_FakeHttp(), settings=None)
    out = d._filter_m3u8_for_download(
        _DisabledSource(), "https://cdn.example.com/hls/i.m3u8", tmp_path
    )
    assert out is None
