# -*- coding: utf-8 -*-
"""提线影院（trtag）暴风线 m3u8 广告：`/video/adjump/` 中插块必须剔除。

2026-11 实测（苍兰诀 第18集，暴风线 bfzym3u8）真实清单：
687 片 / 2729.56 s / 5 个 `#EXT-X-DISCONTINUITY`，两个 9 片 25.76 s 的中插
广告块（片序号 75..83 与 609..617），分片 URL 形如
`https://p.bvvvvvvvvv1f.com/video/adjump/time/17932818683880000000.ts`。

关键事实：广告块与正片**同主机、同目录**，段时长 3.0 s（末片 1.76 s），
唯一独立的广告信号是路径里的 `adjump`；块结构只表现为「被 DISCONTINUITY
夹住的短块」。因此源配置用 `block_url_regex` 直接命中路径，
`hls_rules.drop_short_blocks` 兜底（CDN 轮换路径时仍能删掉）。

本文件用与真实清单同构的离线样例锁定三条行为：
1. 命中 `/video/adjump/` 的段全部删除，`EXTINF` 与媒体行数量一致；
2. 广告路径被 CDN 改名（不含 adjump）时，短块规则仍能删除；
3. 无 DISCONTINUITY 的普通清单原样返回，不能被规则误删。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework.adblock import AdblockEngine
from framework.media_proxy import MediaProxy

_TRTAG_JSON = Path(__file__).resolve().parent.parent / "sources" / "trtag.json"
_BASE = "https://p.bvvvvvvvvv1f.com/video/canglanjue/%E7%AC%AC18%E9%9B%86/index.m3u8"

_CONTENT_HEAD = 75      # 片头正片块（4 s/片，实测 301.08 s）
_AD_COUNT = 9           # 中插广告块（3 s/片 + 末片 1.76 s，实测 25.76 s）
_CONTENT_MID = 525      # 中段正片块
_CONTENT_TAIL = 69      # 片尾正片块

_AD_URL_RE = "/video/adjump/time/17932818683880000000.ts"


def _source_obj():
    raw = json.loads(_TRTAG_JSON.read_text(encoding="utf-8"))
    return type("Source", (), {"raw": raw, "source_id": "trtag"})()


def _engine():
    engine = AdblockEngine()
    engine.configure(_source_obj(), default_on=True)
    return engine


def _ad_block():
    return json.loads(_TRTAG_JSON.read_text(encoding="utf-8"))["ad_block"]


def _seg(name: str, dur: float) -> str:
    return f"#EXTINF:{dur},\n{name}"


def _playlist(ad_name: str = _AD_URL_RE) -> str:
    """与苍兰诀 第18集同构的清单：正片 ×3 块 + 两个 DISCONTINUITY 广告块。"""
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-TARGETDURATION:8",
        "#EXT-X-DISCONTINUITY",
    ]
    lines += [_seg(f"{i:07d}.ts", 4.0) for i in range(_CONTENT_HEAD)]
    lines += ["#EXT-X-DISCONTINUITY"]
    lines += [_seg(ad_name, 3.0) for _ in range(_AD_COUNT - 1)]
    lines += [_seg(ad_name, 1.76)]
    lines += ["#EXT-X-DISCONTINUITY"]
    lines += [_seg(f"{i:07d}.ts", 4.0) for i in range(_CONTENT_HEAD, _CONTENT_HEAD + _CONTENT_MID)]
    lines += ["#EXT-X-DISCONTINUITY"]
    lines += [_seg(ad_name, 3.0) for _ in range(_AD_COUNT - 1)]
    lines += [_seg(ad_name, 1.76)]
    lines += ["#EXT-X-DISCONTINUITY"]
    start = _CONTENT_HEAD + _CONTENT_MID
    lines += [_seg(f"{i:07d}.ts", 4.0) for i in range(start, start + _CONTENT_TAIL)]
    lines += ["#EXT-X-ENDLIST", ""]
    return "\n".join(lines)


def _media_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def _total_dur(text: str) -> float:
    import re

    return round(
        sum(float(m.group(1)) for m in re.finditer(r"#EXTINF:\s*([\d.]+)", text)), 2
    )


@pytest.fixture(scope="module")
def playlist() -> str:
    return _playlist()


def test_source_declares_adjump_rule():
    """源配置必须保留暴风 CDN 的广告路径规则，删掉就会退回「广告原样播放」。"""
    ad = _ad_block()
    assert ad["enabled"] is True
    assert "/video/adjump(?:/|$)" in ad["block_url_regex"]
    short_rule = ad["hls_rules"]["drop_short_blocks"]
    assert short_rule["enabled"] is True
    assert short_rule["max_duration"] >= 26.0   # 实测广告块 25.76 s
    assert short_rule["max_ratio"] <= 0.05


def test_adjump_segments_removed(playlist):
    ad_segs, cleaned = _engine().detect_m3u8_ads(playlist, _BASE)

    expected = set(range(_CONTENT_HEAD, _CONTENT_HEAD + _AD_COUNT)) | set(
        range(_CONTENT_HEAD + _AD_COUNT + _CONTENT_MID,
              _CONTENT_HEAD + _AD_COUNT + _CONTENT_MID + _AD_COUNT)
    )
    assert set(ad_segs) == expected
    assert "adjump" not in cleaned
    assert len(_media_lines(cleaned)) == len(_media_lines(playlist)) - 2 * _AD_COUNT
    assert cleaned.count("#EXTINF") == len(_media_lines(cleaned))
    # 首个正片分片与结尾都要保留，广告块删除后不能留下孤立边界
    assert _media_lines(cleaned)[0] == "0000000.ts"
    assert cleaned.rstrip().endswith("#EXT-X-ENDLIST")
    assert not cleaned.rstrip().splitlines()[-2].startswith("#EXT-X-DISCONTINUITY")
    assert _total_dur(cleaned) == round(_total_dur(playlist) - 2 * 25.76, 2)


def test_short_block_rule_catches_renamed_ad_url(playlist):
    """广告 CDN 换路径（分片名不含 adjump）时，短块规则仍要删掉两个广告块。"""
    renamed = playlist.replace(_AD_URL_RE, "/video/normal/time/17932818683880000000.ts")
    assert "adjump" not in renamed

    ad_segs, cleaned = _engine().detect_m3u8_ads(renamed, _BASE)

    assert len(ad_segs) == 2 * _AD_COUNT
    assert len(_media_lines(cleaned)) == len(_media_lines(playlist)) - 2 * _AD_COUNT


def test_plain_playlist_without_boundaries_untouched():
    """无 DISCONTINUITY 的普通清单（同时长的短视频）不能被短块规则误删。"""
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:4"]
    lines += [_seg(f"{i:07d}.ts", 4.0) for i in range(30)]
    lines += ["#EXT-X-ENDLIST", ""]
    text = "\n".join(lines)

    ad_segs, cleaned = _engine().detect_m3u8_ads(text, _BASE)

    assert ad_segs == []
    assert cleaned == text


def test_media_proxy_filters_before_serving(playlist, monkeypatch):
    """播放代理拿到源 ad_block 后必须先过滤再重写，否则播放器还是会播广告。"""
    proxy = MediaProxy(cache=type("Cache", (), {"enabled": False})())
    sent: list[str] = []
    monkeypatch.setattr(proxy, "_rewrite_m3u8", lambda text, *a, **kw: text)
    monkeypatch.setattr(proxy, "_emit_m3u8", lambda handler, text: sent.append(text))

    proxy._serve_m3u8_text(object(), playlist, _BASE, {}, _ad_block(), source_id="trtag")

    assert sent and "adjump" not in sent[0]
    assert len(_media_lines(sent[0])) == len(_media_lines(playlist)) - 2 * _AD_COUNT
    proxy.stop()
