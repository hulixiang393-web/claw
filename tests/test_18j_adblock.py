# -*- coding: utf-8 -*-
"""18j 源 ad_block 回归测试（2026-09-20 线上实测数据驱动）。

背景：18j 播放路径 m3u8 广告过滤。sources/18j.json 原 block_url_regex 配
"/stream/"（整串 URL 子串匹配，含 host/query/path），而真实 18j 的 HLS
分片路径存在于 video.18j2026.com/stream/<日期>/.../<promo 目录>/ 下——

线上实测（2026-09-20 抓取 5 个视频）形态一致：每个视频 m3u8 开头是 3 个
**共享 promo 预滚分片**（跨视频 URL 完全相同，目录指纹
6aa66a72a56c4f24af7064f2/feb629），随后 #EXT-X-DISCONTINUITY + AES KEY，
主内容都在 /videos/<日期>/<hash>/ 下，主体/中插不含 /stream/；顶层 m3u8
URL 本身也不含 /stream/。

"/stream/" 整串规则过宽：一旦 CDN 让正片也走 /stream/ 路径，整段正片会被
当广告剔除 → 播放缺片/断片/卡顿。修复：
1. 18j.json 规则收窄为 promo 的确定性目录指纹（6aa66a72a56c4f24af7064f2）
2. 源级补充正则只匹配 parsed.path（与内置路径特征同语义，不命中 host/query）

测试要求：
1. 真实形态：共享 promo 预滚分片（含 6aa66a72a56c4f24af7064f2）→ 仍剔除
2. 中性 /stream/ 主内容分片（其它目录，正片可能形态）→ 必须保留
3. 真实广告特征（/ad/ 等内置规则）→ 仍剔除
4. 顶层 m3u8 URL 不判广告 → content.py 顶层校验不误伤
"""
import json
import os

from framework.adblock import AdblockEngine
from framework.media_proxy import MediaProxy

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SOURCE_PATH = os.path.join(_REPO, "sources", "18j.json")

_PROMO = "6aa66a72a56c4f24af7064f2"
# 线上实测 promo 预滚分片（跨视频完全一致）
_PROMO_TS = (
    f"https://video.18j2026.com/stream/202609/13/{_PROMO}/feb629/index0.ts",
    f"https://video.18j2026.com/stream/202609/13/{_PROMO}/feb629/index1.ts",
    f"https://video.18j2026.com/stream/202609/13/{_PROMO}/feb629/index2.ts",
)
# 线上实测主内容分片（/videos/ 路径，真实视频 48506）
_MAIN_TS = tuple(
    f"https://video.18j2026.com/videos/202609/19/6aaeabf1e41c0c24f1613ba7/a5a88a/index{i}.ts"
    for i in range(5)
)

# 真实列表形态：promo 预滚(3) + DISCONTINUITY + AES KEY + 主内容
_REAL_M3U8 = (
    "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
    "#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n"
    "#EXTINF:4.000000,\n" + _PROMO_TS[0] + "\n"
    "#EXTINF:4.000000,\n" + _PROMO_TS[1] + "\n"
    "#EXTINF:3.708333,\n" + _PROMO_TS[2] + "\n"
    "#EXT-X-DISCONTINUITY\n"
    '#EXT-X-KEY:METHOD=AES-128,URI="/videos/202609/19/6aaeabf1e41c0c24f1613ba7/ts.key",IV=0x00000000000000000000000000000000\n'
    + "".join("#EXTINF:4.000000,\n" + u + "\n" for u in _MAIN_TS[:4])
    + "#EXTINF:2.600000,\n" + _MAIN_TS[4] + "\n"
    + "#EXT-X-ENDLIST\n"
)

# 中性 /stream/ 正片可能形态（不含 promo 指纹，真实 CDN 模式）
_NEUTRAL_STREAM_TS = (
    "https://video.18j2026.com/stream/202609/17/6aab924de41c0c24f1677e47/5fd07a/index0.ts",
    "https://video.18j2026.com/stream/202609/17/6aab924de41c0c24f1677e47/5fd07a/index1.ts",
)
_NEUTRAL_M3U8 = (
    "#EXTM3U\n#EXT-X-VERSION:3\n"
    + "".join("#EXTINF:4.0,\n" + u + "\n" for u in _NEUTRAL_STREAM_TS)
    + "#EXT-X-ENDLIST\n"
)

_AD_M3U8 = (
    "#EXTM3U\n#EXT-X-VERSION:3\n"
    "#EXTINF:4.0,\nhttps://video.18j2026.com/ad/promo/index0.ts\n"
    "#EXTINF:4.0,\nhttps://video.18j2026.com/seg/001.ts\n"
    "#EXT-X-ENDLIST\n"
)


def _load_ad_block():
    with open(_SOURCE_PATH, encoding="utf-8") as f:
        return json.load(f)["ad_block"]


def _engine():
    return AdblockEngine(type("S", (), {"raw": {"ad_block": _load_ad_block()}})())


def _segs(playlist):
    return [ln for ln in playlist.splitlines() if ln and not ln.startswith("#")]


class TestReal18jPlaylist:
    """真实 18j 列表：promo 预滚剔除、主内容保留、列表合法。"""

    def test_promo_preroll_stripped_content_kept(self):
        out = _engine().filter_m3u8(
            _REAL_M3U8,
            "https://m3u8.cdn202608.com/videos/202609/19/6aaeabf1e41c0c24f1613ba7/a5a88a/index.m3u8",
        )
        segs = _segs(out)
        # 预滚 promo 剔除
        assert not any(_PROMO in s for s in segs)
        # 主内容全部保留（无缺片）
        assert all(u in segs for u in _MAIN_TS)
        assert len(segs) == len(_MAIN_TS)
        # AES 加密 KEY 保留、DISCONTINUITY 随广告段一并清理
        assert "#EXT-X-KEY:METHOD=AES-128" in out
        assert "#EXT-X-DISCONTINUITY" not in out
        # 列表合法：EXTINF 数与段数一致
        _extinf = sum(1 for ln in out.splitlines() if ln.startswith("#EXTINF"))
        assert _extinf == len(segs)


class TestNeutralStreamPreserved:
    """中性 /stream/ 正片路径（不带 promo 指纹）→ 不得剔除（回归原误伤）。"""

    def test_neutral_stream_segments_kept(self):
        eng = _engine()
        out = eng.filter_m3u8(_NEUTRAL_M3U8, "https://cdn.example.com/hls/i.m3u8")
        assert out == _NEUTRAL_M3U8
        for u in _NEUTRAL_STREAM_TS:
            assert not eng.is_ad_url(u)

    def test_promo_fingerprint_is_ad(self):
        eng = _engine()
        for u in _PROMO_TS:
            assert eng.is_ad_url(u)


class TestRealAdStillBlocked:
    def test_ad_path_segment_removed(self):
        out = _engine().filter_m3u8(_AD_M3U8, "https://cdn.example.com/hls/i.m3u8")
        assert "/ad/promo/index0.ts" not in out
        assert "/seg/001.ts" in out


class TestTopLevelNotNulled:
    """顶层 m3u8 URL 不含广告特征 → 不置空（content.py 顶层校验不受影响）。"""

    def test_top_level_m3u8_not_ad(self):
        eng = _engine()
        assert not eng.is_ad_url(
            "https://m3u8.cdn202608.com/videos/202609/19/6aaeabf1e41c0c24f1613ba7/a5a88a/index.m3u8"
        )


class TestMediaProxyPlaybackPath:
    """播放代理路径（media_proxy._filter_ad_segments）用真实 18j ad_block。"""

    def test_real_playlist_via_proxy(self):
        ad_block = _load_ad_block()
        out = MediaProxy.instance()._filter_ad_segments(
            _REAL_M3U8,
            "https://m3u8.cdn202608.com/videos/202609/19/6aaeabf1e41c0c24f1613ba7/a5a88a/index.m3u8",
            ad_block,
        )
        segs = _segs(out)
        assert not any(_PROMO in s for s in segs)
        assert all(u in segs for u in _MAIN_TS)
        assert len(segs) == len(_MAIN_TS)