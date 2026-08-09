# -*- coding: utf-8 -*-
"""视频流内广告段过滤验证脚本（tools_verify_ad_stream.py）。

背景：用户反馈「某源明明有广告但没被修复」。实测各源真实 m3u8 发现
xuandm（XDM动漫）每个视频都被插入**片头/片尾推广广告块**：同一组 4 段素材
（路径 /20260726/1AS9nSvi/hls/，KEY:METHOD=NONE 无加密、时长固定
5s/5s/5s/2.567s，与主流 AES-128 加密的 ~2s 段完全不同）在每个播放列表里
重复插入两次（中间一次 + 末尾一次）。

旧 adblock 为什么没滤掉：
1. is_ad_url 不命中——广告段 URL 是随机串路径（VWd1eJQz.ts 等），无广告特征词
2. 短段启发式（DISCONTINUITY 后 <3s）不命中——广告段时长 5s ≥ 3s
3. 反而误删了 DISCONTINUITY 后紧跟的正常短段（xuandm 实测 seg96 2s 是
   正片第一段，被旧规则当成广告删掉）

新增启发式（framework/adblock.py）：
- R2 重复段：同一段 URL 在列表中累计出现 ≥2 次 → 该 URL 全部段判广告
  （正常 VOD 每个分片 URL 唯一；广告素材被重复插入列表是强信号）
- R3 孤立短块：被 DISCONTINUITY 分隔、块内段总时长 <3s 的**独立块** → 判广告
  （替代旧「DISCONTINUITY 后单段 <3s」：旧规则误删长正常块开头的正常短段）

本脚本覆盖：
1. xuandm 广告块实测场景：构造同结构的 m3u8（AES 主段 + 重复插入的
   KEY:NONE 广告块）→ 断言两处广告块共 8 段全剔除、正常段保留
2. 回归：构造用例（URL 特征 / 短块 / 相对路径）仍正确剔除
3. 反例：正常 m3u8 不被误删（含 DISCONTINUITY 后正常短段）
4. 合法性：剔除后 EXTINF 数与段数一致、AES-128 KEY 保留、无广告残留

用法：python -u tools_verify_ad_stream.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from framework.adblock import AdblockEngine  # noqa: E402

_failed = 0


def check(name: str, ok: bool) -> None:
    global _failed
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}")
    if not ok:
        _failed += 1


def _parse_segs(text: str):
    """提取段 URL 列表 / EXTINF 数 / DISCONTINUITY 数 / KEY 行。"""
    segs, extinf, disc, keys = [], 0, 0, []
    for line in text.splitlines():
        if line.startswith("#EXTINF"):
            extinf += 1
        elif line.startswith("#EXT-X-DISCONTINUITY"):
            disc += 1
        elif line.startswith("#EXT-X-KEY"):
            keys.append(line)
        elif line and not line.startswith("#"):
            segs.append(line)
    return segs, extinf, disc, keys


# ------------------------------------------------------------------ #
# xuandm 实测结构还原（2026-08-09 抓取）：AES 主段 + 两处 KEY:NONE 广告块
# ------------------------------------------------------------------ #
XUANDM_BASE = "https://vod1.maowushi.com/20260808/aVF6QKrz/1527kb/hls/index.m3u8"

XUANDM_M3U8 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-KEY:METHOD=AES-128,URI="/seg/key.key",IV=0x00000000000000000000000000000000
#EXTINF:2.0,
/seg/001.ts
#EXTINF:2.0,
/seg/002.ts
#EXT-X-DISCONTINUITY
#EXT-X-KEY:METHOD=NONE
#EXTINF:5.0,
/20260726/1AS9nSvi/hls/VWd1eJQz.ts
#EXTINF:5.0,
/20260726/1AS9nSvi/hls/6UkoUQLF.ts
#EXTINF:5.0,
/20260726/1AS9nSvi/hls/bOwoV4Fj.ts
#EXTINF:2.567,
/20260726/1AS9nSvi/hls/jbzvAmZ9.ts
#EXT-X-DISCONTINUITY
#EXT-X-KEY:METHOD=AES-128,URI="/seg/key.key",IV=0x00000000000000000000000000000000
#EXTINF:2.0,
/seg/003.ts
#EXTINF:2.0,
/seg/004.ts
#EXTINF:2.0,
/seg/005.ts
#EXT-X-DISCONTINUITY
#EXT-X-KEY:METHOD=NONE
#EXTINF:5.0,
/20260726/1AS9nSvi/hls/VWd1eJQz.ts
#EXTINF:5.0,
/20260726/1AS9nSvi/hls/6UkoUQLF.ts
#EXTINF:5.0,
/20260726/1AS9nSvi/hls/bOwoV4Fj.ts
#EXTINF:2.567,
/20260726/1AS9nSvi/hls/jbzvAmZ9.ts
#EXT-X-ENDLIST
"""
# 预期：保留 001~005 五个正片段；两处广告块（VWd1eJQz/6UkoUQLF/bOwoV4Fj/jbzvAmZ9）共 8 段剔除
XUANDM_KEPT = [
    "/seg/001.ts", "/seg/002.ts", "/seg/003.ts", "/seg/004.ts", "/seg/005.ts",
]
XUANDM_AD = [
    "VWd1eJQz", "6UkoUQLF", "bOwoV4Fj", "jbzvAmZ9",
]


def test_xuandm_ad_block() -> None:
    print("== 1. xuandm 重复插入广告块（实测场景）==")
    engine = AdblockEngine()
    out = engine.filter_m3u8(XUANDM_M3U8, XUANDM_BASE)
    segs, extinf, disc, keys = _parse_segs(out)
    check("两处广告块共 8 段全剔除", not any(a in out for a in XUANDM_AD))
    check("正片段 001~005 全保留", segs == XUANDM_KEPT)
    check("EXTINF 数与段数一致（列表合法）", extinf == len(segs))
    check("DISCONTINUITY 清理干净", disc == 0)
    check("AES-128 加密 KEY 保留（后续段可解密）", any("METHOD=AES-128" in k for k in keys))
    check("无广告块专用 KEY:NONE 残留", not any(k.strip() == "#EXT-X-KEY:METHOD=NONE" for k in keys))
    check("头尾保留", out.startswith("#EXTM3U") and out.rstrip().endswith("#EXT-X-ENDLIST"))


# ------------------------------------------------------------------ #
# 回归：既有用例（URL 特征 / DISCONTINUITY 短段 / 相对路径）
# ------------------------------------------------------------------ #
M3U8_URL_AD = """#EXTM3U
#EXT-X-VERSION:3
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
# 旧启发式语义：ad1.ts（URL 特征）+ 1003.ts（DISCONTINUITY 后孤立短块）剔除
M3U8_REGRESSION_1 = [
    "https://cdn.example.com/seg/1001.ts",
    "https://cdn.example.com/seg/1002.ts",
    "https://cdn.example.com/seg/1004.ts",
]

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


def test_regression() -> None:
    print("== 2. 既有用例回归（URL 特征 / 孤立短块 / 相对路径）==")
    engine = AdblockEngine()
    out = engine.filter_m3u8(M3U8_URL_AD, "https://cdn.example.com/play/list.m3u8")
    segs, extinf, disc, _ = _parse_segs(out)
    check("ad1.ts(URL特征)+1003.ts(短块) 剔除", segs == M3U8_REGRESSION_1)
    check("回归1 EXTINF 数与段数一致", extinf == len(segs) and disc == 0)

    out2 = engine.filter_m3u8(M3U8_RELATIVE, "https://cdn.example.com/v/playlist.m3u8")
    segs2, extinf2, disc2, _ = _parse_segs(out2)
    check("相对路径 ads/ 广告段剔除", segs2 == ["seg/3001.ts", "seg/3003.ts"])
    check("回归2 EXTINF 数与段数一致", extinf2 == len(segs2) and disc2 == 0)


# ------------------------------------------------------------------ #
# 反例：正常 m3u8 不被误删（含 DISCONTINUITY 后正常短段）
# ------------------------------------------------------------------ #
def test_normal_not_touched() -> None:
    print("== 3. 反例：正常 m3u8 不被误删 ==")
    engine = AdblockEngine()
    # 干净列表（含 DISCONTINUITY 分隔的正片 + 正常 1.5s 段但块总时长不短）
    clean = (
        "#EXTM3U\n#EXT-X-VERSION:3\n"
        "#EXT-X-DISCONTINUITY\n#EXTINF:10.0,\nhttps://cdn.example.com/seg/5001.ts\n"
        "#EXTINF:1.5,\nhttps://cdn.example.com/seg/5002.ts\n"
        "#EXTINF:10.0,\nhttps://cdn.example.com/seg/5003.ts\n#EXT-X-ENDLIST\n"
    )
    check("无广告列表原样返回（不写 _clean）", engine.filter_m3u8(clean) == clean)
    # 单个段 URL 唯一 → 不触发重复段规则
    uniq = "#EXTM3U\n#EXTINF:2.0,\nhttps://cdn.example.com/seg/u1.ts\n" \
           "#EXTINF:2.0,\nhttps://cdn.example.com/seg/u2.ts\n#EXT-X-ENDLIST\n"
    check("唯一 URL 短段不被误删", engine.filter_m3u8(uniq) == uniq)
    # 重复段反例边界：同一 URL 出现 2 次但确实是同段（直播循环）——按规则判广告是
    # 预期行为（VOD 里重复段即广告素材），此处仅确认不崩溃、剔除合法
    loop = "#EXTM3U\n#EXTINF:2.0,\nhttps://cdn.example.com/seg/l1.ts\n" \
           "#EXTINF:2.0,\nhttps://cdn.example.com/seg/l1.ts\n#EXT-X-ENDLIST\n"
    out = engine.filter_m3u8(loop)
    segs, extinf, _, _ = _parse_segs(out)
    check("重复段剔除后列表合法", extinf == len(segs))


# ------------------------------------------------------------------ #
def test_detect_m3u8_ads_indices() -> None:
    print("== 4. detect_m3u8_ads 广告段索引 ==")
    engine = AdblockEngine()
    ad_segs, cleaned = engine.detect_m3u8_ads(XUANDM_M3U8, XUANDM_BASE)
    check("两处广告块索引正确（共 8 段）", len(ad_segs) == 8)
    check("索引与剔除结果一致", len([s for s in cleaned.splitlines() if s and not s.startswith("#")]) == 5)
    # 无广告时返回 [] + 原文
    ad2, c2 = engine.detect_m3u8_ads(
        "#EXTM3U\n#EXTINF:2.0,\nseg/1.ts\n#EXT-X-ENDLIST\n"
    )
    check("无广告返回 [] + 原文", ad2 == [] and c2 == "#EXTM3U\n#EXTINF:2.0,\nseg/1.ts\n#EXT-X-ENDLIST\n")


def main() -> int:
    global _failed
    _failed = 0
    print("=" * 60)
    print("视频流内广告段过滤验证（tools_verify_ad_stream.py）")
    print("=" * 60)
    test_xuandm_ad_block()
    print()
    test_regression()
    print()
    test_normal_not_touched()
    print()
    test_detect_m3u8_ads_indices()
    print()
    print(f"全部断言{'通过' if _failed == 0 else '失败 ' + str(_failed) + ' 项'}")
    print("=" * 60)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
