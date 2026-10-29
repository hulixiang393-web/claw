# -*- coding: utf-8 -*-
"""omofuna（天堂线）/ ikanpp 广告严格过滤回归。

现场（用户反馈）：omofuna 的「天堂」线路与 ikanpp 还要放广告。

实测结构（缓存清单 + 站点真实清单）：
- 天堂/暴风这类「mp4 转 HLS」清单里，中插广告是一段**被 DISCONTINUITY 夹住
  的极短块**，段 URL 形如 `/video/adjump/time/<ts>.ts`；omofuna 源此前
  `ad_block` 只声明了 remux_timestamps，既没有 URL 规则也没有短块规则，
  于是整段广告原样播放（实测 9 片 / 26.04s 一条都没删）。
- ikanpp/json1080 的清单是 6 块结构：124 / 4(16.47s) / 324 / 5(17.67s) /
  243 / 4(16.47s)，广告块短且与正片同主机同目录。

过滤严格化之后必须同时满足：
1) 广告块全删；2) 正片块一片不少；3) 「5 片/块」的盘内 VOD（块划分只是
PTS 重置标记）不能被块规则误判成广告。
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from framework.adblock import AdblockEngine  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources"


def _raw(name):
    return json.loads((SOURCES / f"{name}.json").read_text(encoding="utf-8"))


def _engine(name):
    raw = _raw(name)
    eng = AdblockEngine()
    eng.configure(type("S", (), {"raw": {"ad_block": raw["ad_block"]}})(),
                  default_on=True)
    return eng


def _manifest(blocks, ad_prefix=""):
    """按块拼清单：块之间用 DISCONTINUITY 分隔。"""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    for bi, durs in enumerate(blocks):
        if bi:
            lines.append("#EXT-X-DISCONTINUITY")
        for si, dur in enumerate(durs):
            lines.append(f"#EXTINF:{dur:.3f},")
            lines.append(f"{ad_prefix}b{bi}_{si}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


BASE = "https://vip.dytt-cinema.com/20261002/x/index.m3u8"

# 实测的广告素材时长签名（block1 与 block5 完全相同 → 片头/片尾同素材）
AD4 = [6.63, 3.33, 4.8, 1.71]          # 4 片 / 16.47s
AD5 = [5.0, 4.0, 4.0, 3.0, 1.67]       # 5 片 / 17.67s


# ---------------------------------------------------------------------- #
# 源配置
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["omofuna", "ikanpp"])
def test_source_declares_adjump_url_rule(name):
    """`/video/adjump/` 是实测的中插广告路径，两个源都必须声明。"""
    rules = _raw(name)["ad_block"]["block_url_regex"]
    assert "/video/adjump(?:/|$)" in rules


@pytest.mark.parametrize("name", ["omofuna", "ikanpp"])
def test_source_declares_short_block_rule_and_remux(name):
    hls = _raw(name)["ad_block"]["hls_rules"]
    short = hls["drop_short_blocks"]
    assert short["enabled"] is True
    assert short["require_boundary"] is True
    assert 20 <= short["max_duration"] <= 60
    assert short["max_ratio"] <= 0.05
    # 删掉中插广告后一定留下时间轴缺口 → 必须同时开时间戳整理，
    # 否则播放器在拼接处的时间轴仍然跳（用户看到的「跳帧」）。
    assert hls["remux_timestamps"]["enabled"] is True


# ---------------------------------------------------------------------- #
# 天堂线：3 块清单（正片 / adjump 中插 / 正片）
# ---------------------------------------------------------------------- #
def test_omofuna_drops_adjump_midroll_block():
    """105 片 302.92s | 9 片 26.04s adjump | 373 片 1072.20s。"""
    playlist = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-MEDIA-SEQUENCE:0"]
    for i in range(105):
        playlist += [f"#EXTINF:{3.64 if i == 0 else 3.0:.3f},", f"{i:07d}.ts"]
    playlist.append("#EXT-X-DISCONTINUITY")
    for i in range(9):
        playlist += [f"#EXTINF:{3.0:.3f},",
                     f"/video/adjump/time/178731986593{i:08d}.ts"]
    playlist.append("#EXT-X-DISCONTINUITY")
    for i in range(373):
        playlist += [f"#EXTINF:{3.0:.3f},", f"{i + 105:07d}.ts"]
    playlist.append("#EXT-X-ENDLIST")
    text = "\n".join(playlist)

    for name in ("omofuna", "ikanpp"):
        ads, out = _engine(name).detect_m3u8_ads(text, BASE)
        assert len(ads) == 9, name
        assert "adjump" not in out, name
        # 正片一片不能少
        assert out.count("#EXTINF:") == 105 + 373, name


# ---------------------------------------------------------------------- #
# ikanpp：6 块清单（正片/广告交替）
# ---------------------------------------------------------------------- #
def test_short_blocks_removed_and_program_blocks_kept():
    blocks = [[4.0] * 124, AD4, [4.0] * 324, AD5, [4.0] * 243, AD4]
    text = _manifest(
        blocks, ad_prefix="https://s2.bfllvip.com/video/x/seg/")

    for name in ("omofuna", "ikanpp"):
        ads, out = _engine(name).detect_m3u8_ads(text, BASE)
        # 三个广告块（4 + 5 + 4 片）全删
        assert len(ads) == 13, name
        # 正片块原样保留
        assert out.count("#EXTINF:") == 124 + 324 + 243, name


# ---------------------------------------------------------------------- #
# 反例：盘内 VOD 的「5 片/块」不能被块规则误删
# ---------------------------------------------------------------------- #
def test_five_segment_blocks_are_not_treated_as_ads():
    """omofuna 天堂线的块划分只是播放器 PTS 重置标记。

    每块 5 片 → 没有任何「明显更长的正片块」，短块规则必须整体不生效，
    否则整条片子会被当成广告删光。
    """
    blocks = [[3.0] * 5 for _ in range(40)]
    text = _manifest(blocks, ad_prefix="https://cdn.example.com/md5/")

    for name in ("omofuna", "ikanpp"):
        ads, out = _engine(name).detect_m3u8_ads(text, BASE)
        assert ads == [], name
        assert out.count("#EXTINF:") == 200, name


# ---------------------------------------------------------------------- #
# 内置规则覆盖不到 adjump → 必须靠源级规则
# ---------------------------------------------------------------------- #
def test_adjump_path_needs_source_rule():
    url = "https://vip.dytt-cinema.com/video/adjump/time/1.ts"
    default_engine = AdblockEngine()          # 无源配置：内置启发式
    default_engine.configure(type("S", (), {"raw": {}})(), default_on=True)
    assert default_engine.is_ad_url(url) is False
    assert _engine("omofuna").is_ad_url(url) is True
    assert _engine("ikanpp").is_ad_url(url) is True
