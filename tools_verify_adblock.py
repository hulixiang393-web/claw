# -*- coding: utf-8 -*-
"""adblock 广告过滤引擎验证脚本（tools_verify_adblock.py）。

覆盖：
1. is_ad_url 正例（真实广告 URL 样本：域名黑名单 / 路径特征 / query 特征）
2. is_ad_url 反例（正常 URL 样本：视频 CDN / 图片 CDN / m3u8 段 / 边界词，
   验证不误伤）
3. filter_m3u8（构造含广告段的 m3u8，验证剔除后列表合法：
   保留正常段，广告段及其 DISCONTINUITY/EXTINF 被清理）
4. filter_urls（批量过滤）
5. 现有源配置冒烟（加载 sources/*.json 构造 AdblockEngine 不报错，
   源级补充规则生效）

用法：python -u tools_verify_adblock.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from framework.adblock import AdblockEngine  # noqa: E402

SOURCES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources")

# ------------------------------------------------------------------ #
# 正例：广告 URL（应判为广告）—— 覆盖域名黑名单 / 路径特征 / query 特征
# ------------------------------------------------------------------ #
AD_URLS = [
    # —— 域名黑名单（Google 系）——
    "https://ad.doubleclick.net/ddm/trackimp/N1234.abc/B5678;dc_trk_aid=1",
    "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js",
    "https://www.google-analytics.com/collect?v=1&t=pageview",
    "https://www.googletagmanager.com/gtag/js?id=G-XXXXXX",
    # —— 域名黑名单（中文统计/推广）——
    "https://hm.baidu.com/hm.js?6a9c8f4d0b0e5e6f1a2b3c4d5e6f7a8b",
    "https://c.cnzz.com/core.php?web_id=123456&t=z",
    "https://v1.eclick.baidu.com/fp.js?t=1&ts=1700000000",
    "https://pos.baidu.com/auto_dup?di=123456&w=300&h=250",
    "https://123.51.la/t/1.js",
    # —— 域名黑名单（欧美 DSP / 原生广告）——
    "https://cdn.taboola.com/libtrc/xxx-network/loader.js",
    "https://cds.smartadserver.com/config.js?nwid=123",
    "https://s.media.net/banners/1234/1.png",
    "https://ads.pubmatic.com/AdServer/js/showad.js",
    "https://widgets.outbrain.com/outbrain.js",
    # —— 路径特征 ——
    "https://cdn.example.com/ad/123/456.mp4",
    "https://img.example.com/ads/1024x768.jpg",
    "https://cdn.example.com/gg/banner1.gif",
    "https://img.example.com/guanggao/xxx.png",
    "https://cdn.example.com/adserver/unit1.ts",
    "https://img.example.com/banner_468x60.jpg",
    "https://cdn.example.com/promo/ad.mp4",
    "https://img.example.com/pics/ad_click.jpg",
    "https://cdn.example.com/adv/1.ts",
    "https://img.example.com/xx_ad.png",
    "https://cdn.example.com/advert/1.ts",
    "https://img.example.com/gg_123.jpg",
    "https://cdn.example.com/tuiguang/xxx.png",
    "https://img.example.com/promotion/1.mp4",
    "https://cdn.example.com/ggad/xxx.png",
    "https://img.example.com/adview/creative.png",
    # —— query 参数特征 ——
    "https://example.com/video.mp4?ad_id=123",
    "https://example.com/video.mp4?affid=xyz",
    "https://example.com/v.mp4?clickid=abc123",
    "https://example.com/img.jpg?ad=1",
    "https://example.com/xxx.png?banner=1",
    "https://example.com/v.mp4?affiliate=abc",
    "https://example.com/video.ts?placement_id=99",
]

# ------------------------------------------------------------------ #
# 反例：正常 URL（不应判为广告）—— 视频 CDN / 图片 CDN / m3u8 段 / 边界词
# ------------------------------------------------------------------ #
NORMAL_URLS = [
    # —— 视频 CDN ——
    "https://upos-sz-mirrorcos.bilivideo.com/upgcx/code/1234/5678/001.mp4?deadline=1700000000&gen=abc",
    "https://v.phncdn.com/hls/2024/06/15/abc/1000_2000.ts",
    "https://v.missav.com/xxx/yyy/1080p.mp4",
    "https://cdn.example.com/play/230901/ep123.m3u8",
    # —— 图片 CDN ——
    "https://i0.hdslb.com/bfs/archive/5f3a9c1b2d3e4f5a6b7c8d9e0f1a2b3c.jpg",
    "https://img.example.com/comics/1234/056_001.jpg",
    "https://static.zongheng.com/cover/2018/05/15/1234567.jpg",
    "https://img.example.com/reader/2023/08/01/1234.jpg?sign=xyz",
    # —— m3u8 分片（数字/随机路径，必须不误伤）——
    "https://cdn.example.com/segment/1700000000001.ts",
    "https://cdn.example.com/hls/abc/def/1.ts?token=abc123",
    "https://cdn.example.com/m3u8/1700000000001/1000.ts",
    # —— 边界词（含 ad/banner/promo 单词片段但非广告路径）——
    "https://cdn.example.com/promotional/video/1.ts",
    "https://cdn.example.com/adventure/ep1.ts",
    "https://img.example.com/madagascar/cover.jpg",
    "https://cdn.example.com/media/230901/ep123.m3u8",
    # —— query 值含广告词但参数名不是广告参数 ——
    "https://example.com/video.mp4?from=ad&ref=advertising",
    "https://example.com/video.mp4?u=ad_unit_1",
    "https://example.com/callback?id=ad123",
]

# ------------------------------------------------------------------ #
# filter_m3u8 用例
# ------------------------------------------------------------------ #
# 构造：正常段 1001/1002/1004 + 广告段 ad1.ts（URL 特征）+ 短段 1003.ts
#（DISCONTINUITY 后 <3s，短时长启发式）
M3U8_IN = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-DISCONTINUITY
#EXTINF:3.0,
https://cdn.example.com/ads/ad1.ts
#EXTINF:5.0,
https://cdn.example.com/seg/1001.ts
#EXTINF:10.0,
https://cdn.example.com/seg/1002.ts
#EXT-X-DISCONTINUITY
#EXTINF:1.5,
https://cdn.example.com/seg/1003.ts
#EXT-X-DISCONTINUITY
#EXTINF:10.0,
https://cdn.example.com/seg/1004.ts
#EXT-X-ENDLIST
"""
# 预期：ad1.ts（URL 特征）与 1003.ts（DISCONTINUITY+短时长）被剔除；
# 所有 DISCONTINUITY 清理；保留段 1001/1002/1004 及其 EXTINF；列表合法
M3U8_EXPECTED_SEGS = [
    "https://cdn.example.com/seg/1001.ts",
    "https://cdn.example.com/seg/1002.ts",
    "https://cdn.example.com/seg/1004.ts",
]

# 广告段在列表开头的场景
M3U8_AD_FIRST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-DISCONTINUITY
#EXTINF:2.0,
https://cdn.example.com/ggad/start.ts
#EXTINF:10.0,
https://cdn.example.com/seg/2001.ts
#EXTINF:10.0,
https://cdn.example.com/seg/2002.ts
#EXT-X-ENDLIST
"""

# 相对路径段（base_url 解析）场景
M3U8_RELATIVE = """#EXTM3U
#EXT-X-VERSION:3
#EXTINF:10.0,
seg/3001.ts
#EXT-X-DISCONTINUITY
#EXTINF:2.5,
ads/seg/3002.ts
#EXTINF:10.0,
seg/3003.ts
#EXT-X-ENDLIST
"""


def check(name: str, ok: bool) -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}")
    if not ok:
        global _failed
        _failed += 1


def test_is_ad_url() -> None:
    print("== 1. is_ad_url 正反用例 ==")
    engine = AdblockEngine()
    ad_hit = 0
    for u in AD_URLS:
        if engine.is_ad_url(u):
            ad_hit += 1
        else:
            check(f"广告 URL 未命中（应判广告）: {u}", False)
    check(f"广告正例全部命中（{ad_hit}/{len(AD_URLS)}）", ad_hit == len(AD_URLS))

    normal_hit = 0
    for u in NORMAL_URLS:
        if engine.is_ad_url(u):
            check(f"正常 URL 误伤: {u}", False)
        else:
            normal_hit += 1
    check(f"正常反例全部放行（{normal_hit}/{len(NORMAL_URLS)}）", normal_hit == len(NORMAL_URLS))


def test_filter_urls() -> None:
    print("== 2. filter_urls 批量过滤 ==")
    engine = AdblockEngine()
    mixed = [
        "https://cdn.example.com/seg/1.ts",
        "https://cdn.example.com/ad/1.ts",
        "https://img.example.com/cover.jpg",
        "https://hm.baidu.com/hm.js?x=1",
    ]
    out = engine.filter_urls(mixed)
    check("剔除 2 个广告、保留 2 个正常", out == [
        "https://cdn.example.com/seg/1.ts",
        "https://img.example.com/cover.jpg",
    ])


def _parse_m3u8_segs(text: str) -> list:
    """提取 m3u8 中的段 URL 与 EXTINF/DISCONTINUITY 数量（粗校验）。"""
    segs, extinf, disc = [], 0, 0
    for line in text.splitlines():
        if line.startswith("#EXTINF"):
            extinf += 1
        elif line.startswith("#EXT-X-DISCONTINUITY"):
            disc += 1
        elif line and not line.startswith("#"):
            segs.append(line)
    return segs, extinf, disc


def test_filter_m3u8() -> None:
    print("== 3. filter_m3u8 广告段剔除 ==")
    engine = AdblockEngine()
    # 用例 A：常规广告段 + 短时长广告段
    out = engine.filter_m3u8(M3U8_IN, "https://cdn.example.com/play/list.m3u8")
    segs, extinf, disc = _parse_m3u8_segs(out)
    check("剔除后保留 3 个正常段", segs == M3U8_EXPECTED_SEGS)
    check("DISCONTINUITY 全部清理", disc == 0)
    check("EXTINF 数量与段数一致（列表合法）", extinf == len(segs))
    check("无 ad1.ts / 1003.ts 残留", "ad1.ts" not in out and "1003.ts" not in out)
    check("列表头尾保留", out.startswith("#EXTM3U") and out.rstrip().endswith("#EXT-X-ENDLIST"))
    # 用例 B：广告段在列表开头
    out2 = engine.filter_m3u8(M3U8_AD_FIRST, "https://cdn.example.com/play/a.m3u8")
    segs2, extinf2, disc2 = _parse_m3u8_segs(out2)
    check("开头广告段被剔除", segs2 == [
        "https://cdn.example.com/seg/2001.ts",
        "https://cdn.example.com/seg/2002.ts",
    ])
    check("用例B EXTINF 与段数一致", extinf2 == len(segs2) and disc2 == 0)
    # 用例 C：相对路径 + base_url 解析
    out3 = engine.filter_m3u8(M3U8_RELATIVE, "https://cdn.example.com/v/playlist.m3u8")
    segs3, extinf3, disc3 = _parse_m3u8_segs(out3)
    check("相对路径广告段（ads/）被剔除", segs3 == [
        "seg/3001.ts",
        "seg/3003.ts",
    ])
    check("用例C EXTINF 与段数一致", extinf3 == len(segs3) and disc3 == 0)
    # 用例 D：无广告列表原样返回
    clean = "#EXTM3U\n#EXTINF:10.0,\nseg/1.ts\n#EXT-X-ENDLIST\n"
    check("无广告列表原样返回", engine.filter_m3u8(clean) == clean)


def test_sources_smoke() -> None:
    print("== 4. 现有源配置冒烟 ==")
    from framework.config import load_source

    targets = [
        "bilibili.json",   # 带 ad_block.enabled
        "pornhub.json",
        "fdzys.json",
        "18mh-video.json",
        "missav.json",
        "zongheng.json",
    ]
    ok_all = True
    for name in targets:
        path = os.path.join(SOURCES_DIR, name)
        if not os.path.exists(path):
            check(f"{name} 不存在", False)
            ok_all = False
            continue
        src = load_source(path)
        engine = AdblockEngine(src)
        check(f"{name}: 构造成功（enabled={engine.enabled}）", engine.enabled is True)
    # 源级补充规则生效（构造一个带 block_domains/block_url_regex 的假源）
    fake = {
        "$id": "fake-ad", "$name": "假源", "$type": "video",
        "ad_block": {
            "enabled": True,
            "block_domains": ["*.padd.top"],
            "block_url_regex": [r"/zaodian/"],
        },
    }
    from framework.config import SourceConfig

    engine = AdblockEngine(SourceConfig.from_dict(fake))
    check("源级 block_domains 生效", engine.is_ad_url("https://img.padd.top/x.jpg"))
    check("源级 block_url_regex 生效", engine.is_ad_url("https://cdn.example.com/zaodian/1.ts"))
    check("源级规则不误伤无关 URL", not engine.is_ad_url("https://cdn.example.com/zao/1.ts"))
    check("源级补充开关关闭时全部放行", (
        AdblockEngine(SourceConfig.from_dict({**fake, "ad_block": {"enabled": False}}))
        .is_ad_url("https://img.padd.top/x.jpg") is False
    ))


def main() -> int:
    global _failed
    _failed = 0
    print("=" * 60)
    print("adblock 广告过滤引擎验证")
    print("=" * 60)
    test_is_ad_url()
    print()
    test_filter_urls()
    print()
    test_filter_m3u8()
    print()
    test_sources_smoke()
    print()
    print(f"正例 {len(AD_URLS)} 个 / 反例 {len(NORMAL_URLS)} 个，全部断言通过，"
          f"失败 {_failed} 项")
    print("=" * 60)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
