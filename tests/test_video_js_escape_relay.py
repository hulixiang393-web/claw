# -*- coding: utf-8 -*-
"""JS escape（%uXXXX）混编地址的解码 + 源站中继候选。

现场（omofuna 实测，2026-10）：
  `player_aaaa.url` 解出来是
  `https://c1.rrcdnbf6.com/video/.../%u7B2C%32%32%u96C6/%69%6E%64%65%78%2E%6D%33%75%38`
  —— `%u7B2C`/`%u96C6` 是 JS escape（第/集），其余是 percent。`%u` 在 URL 里
  非法，mpv 拿去请求必然 404（真实目录是 UTF-8 的 `%E7%AC%AC22%E9%9B%86`，
  正片分片实测 200），用户看到的是「清单有、一直卡/拖动卡死」。

另外该站的**清单**只经站点自己的中继 `m3u8xx.sgzm.net:2087/<原始URL>` 可取
（裸 CDN 清单 404、分片 200），所以 `play_url.hls_relay` 要能生成中继候选。
"""
from __future__ import annotations

import framework.content.video as video_mod
from framework.content.video import VideoMixin

# 站上抓到的原始串（%u 与 percent 混编）
_OMOFUNA_RAW = (
    "%68%74%74%70%73%3A%2F%2F%63%31%2E%72%72%63%64%6E%62%66%36%2E%63%6F%6D"
    "%2F%76%69%64%65%6F%2F%71%69%6D%75%6E%61%6E%78%69%6F%6E%67%64%65%7A%61"
    "%69%6E%61%6E%64%69%79%69%6A%69%2F%u7B2C%32%32%u96C6%2F%69%6E%64%65%78"
    "%2E%6D%33%75%38"
)
_OMOFUNA_EXPECTED = (
    "https://c1.rrcdnbf6.com/video/qimunanxiongdezainandiyiji/"
    "%E7%AC%AC22%E9%9B%86/index.m3u8"
)


def test_js_escape_url_is_decoded_to_utf8_percent():
    """%uXXXX 组成的「第22集」必须还原成 UTF-8 percent，否则播放器 404。"""
    out = VideoMixin._normalize_js_encoded_url(_OMOFUNA_RAW)
    assert out == _OMOFUNA_EXPECTED
    assert "%u" not in out


def test_js_escape_backslash_form_is_decoded():
    """`\\uXXXX` 形态（JSON 里常见）同样处理。"""
    out = VideoMixin._normalize_js_encoded_url(
        r"https://a.test/video/\u7B2C01\u96C6/index.m3u8"
    )
    assert "%E7%AC%AC01%E9%9B%86" in out
    assert "\\u" not in out


def test_plain_url_is_untouched():
    """干净地址不做任何重新编码（避免把 `%2F` 之类解坏）。"""
    for url in (
        "https://a.test/x/index.m3u8",
        "https://a.test/%E7%AC%AC22%E9%9B%86/index.m3u8",
        "https://a.test/x/index.m3u8?a=1&b=2",
    ):
        assert VideoMixin._normalize_js_encoded_url(url) == url


def test_relay_candidate_is_prepended_and_original_kept():
    """配了 hls_relay 时：中继版排最前，原地址保留兜底。"""
    cfg = {"hls_relay": "https://relay.test:2087/{url}"}
    bare = _OMOFUNA_EXPECTED
    out = VideoMixin._with_relay_candidates(cfg, [bare])
    assert out[0] == "https://relay.test:2087/" + bare
    assert bare in out
    assert len(out) == 2


def test_relay_without_config_returns_input():
    bare = _OMOFUNA_EXPECTED
    assert VideoMixin._with_relay_candidates({}, [bare]) == [bare]
    assert VideoMixin._with_relay_candidates({"hls_relay": ""}, [bare]) == [bare]


def test_relay_does_not_wrap_already_relayed_url():
    """已经是中继地址的候选不再重复套前缀（否则 404）。"""
    cfg = {"hls_relay": "https://relay.test:2087/{url}"}
    relayed = "https://relay.test:2087/" + _OMOFUNA_EXPECTED
    out = VideoMixin._with_relay_candidates(cfg, [relayed])
    assert out == [relayed]


def test_relay_skips_non_manifest_candidates():
    """只有 .m3u8 清单套中继；mp4 直链不动。"""
    cfg = {"hls_relay": "https://relay.test:2087/{url}"}
    mp4 = "https://cdn.test/a/b.mp4"
    out = VideoMixin._with_relay_candidates(cfg, [mp4])
    assert out == [mp4]


def test_omofuna_source_does_not_relay_manifest():
    """回归守卫：omofuna 的清单**不**走站点中继。

    中继 `m3u8xx.sgzm.net:2087` 实测已停用（清单经中继 7.7s、直取 3.5s；且中继会
    把相对分片 0000000.ts 拼成中继地址 → 404/SSL EOF，播放器反复重试 = 卡顿）。
    现在只保留 `hls_relay_note` 记录原因，配置里不得再出现 `hls_relay`——
    一旦重新打开，取流会重新慢一倍并开始丢分片。
    """
    import json
    from pathlib import Path

    root = Path(video_mod.__file__).resolve().parents[2]
    data = json.loads((root / "sources" / "omofuna.json").read_text(
        encoding="utf-8-sig"))
    play = data["endpoints"]["content"]["episode"]["play_url"]
    assert not play.get("hls_relay")
    assert "m3u8xx.sgzm.net" in play.get("hls_relay_note", "")
