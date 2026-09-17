# -*- coding: utf-8 -*-
"""播放代理 m3u8 广告过滤：无配置也用内置规则；显式关闭仍生效。"""
from framework.media_proxy import MediaProxy


_AD_M3U8 = (
    "#EXTM3U\n"
    "#EXT-X-CUE-OUT\n"
    "#EXTINF:5.0,\n/seg/ad1.ts\n"
    "#EXT-X-CUE-IN\n"
    "#EXTINF:10.0,\n/seg/001.ts\n"
    "#EXT-X-ENDLIST\n"
)


def test_filter_ad_segments_no_config_builtin():
    mp = MediaProxy.instance()
    out = mp._filter_ad_segments(_AD_M3U8, "https://cdn.example.com/hls/i.m3u8", None)
    assert "ad1.ts" not in out
    assert "001.ts" in out


def test_filter_ad_segments_respects_disabled():
    mp = MediaProxy.instance()
    out = mp._filter_ad_segments(
        _AD_M3U8, "https://cdn.example.com/hls/i.m3u8", {"enabled": False}
    )
    assert out == _AD_M3U8