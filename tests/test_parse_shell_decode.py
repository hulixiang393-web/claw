# -*- coding: utf-8 -*-
"""解析站外壳页 → 媒体直链（test_parse_shell_decode.py）。

真实形态（omofuna 实测）：
    player_aaaa = {"url": "%68%74%74%70%73%3A%2F%2Fvip.example.com%2F...index.m3u8",
                   "parse": "https://art.v2player.top:8989/player/", "ps": "1"}
代码会把解析站外壳 `parse?url=<编码值>` 当候选返回。mpv/VLC 打开这种网页只有
空白窗口 —— 所以要把编码里的真实 m3u8 解出来排在外壳前面；实在解不出时才报
「只解析到播放器外壳页」，别给用户一个空窗口。

运行：python -m pytest tests/test_parse_shell_decode.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from framework.content import Content  # noqa: E402
from framework.content.video import ContentMissingError, VideoMixin  # noqa: E402
from framework.parser import Parser  # noqa: E402

EP = "https://www.omofuna.com/anime/188747f10e1c748933bba3e4/play/3/1.html?sid=3"
INNER = "https://vip.example-cdn.com/20250122/15546_abc/index.m3u8"

PERCENT = "".join(f"%{ord(ch):02X}" for ch in INNER)          # 逐字符 percent
PLAIN_PERCENT = (
    "https%3A%2F%2Fvip.example-cdn.com%2F20250122%2F15546_abc%2Findex.m3u8"
)
HEX = INNER.encode("utf-8").hex()


def _page(raw_url: str, ps: str = "1") -> str:
    return (
        '<script>var player_aaaa={"flag":"play","encrypt":2,'
        f'"url":"{raw_url}","ps":"{ps}",'
        '"parse":"https://art.v2player.top:8989/player/"}</script>'
    )


def _candidates(html: str) -> list:
    content = Content(http=None, parser=Parser(), checker=None)
    return content._extract_play_url_candidates(html, None, {}, {}, EP, EP)


# ---------------------------------------------------------------------- #
# 解码本身
# ---------------------------------------------------------------------- #
def test_decode_percent_encoded_target():
    assert VideoMixin._decode_player_target(PERCENT) == INNER


def test_decode_plain_percent():
    assert VideoMixin._decode_player_target(PLAIN_PERCENT) == INNER


def test_decode_hex_target():
    assert VideoMixin._decode_player_target(HEX) == INNER


def test_decode_garbage_returns_empty():
    assert VideoMixin._decode_player_target("not-an-address") == ""
    assert VideoMixin._decode_player_target("") == ""


# ---------------------------------------------------------------------- #
# 候选排序：直链在前，外壳在后
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", [PERCENT, PLAIN_PERCENT, HEX])
def test_decoded_media_precedes_shell(raw):
    cands = _candidates(_page(raw))
    assert cands, "应解析出候选"
    assert cands[0] == INNER, cands[:3]
    assert any("/player/?url=" in c for c in cands), "外壳页仍作为后备保留"
    assert cands.index(INNER) < next(
        i for i, c in enumerate(cands) if "/player/?url=" in c)


def test_shell_only_when_target_is_opaque():
    """目标是不透明 hash（解不出 http 地址）时，不许凭空造直链。"""
    cands = _candidates(_page("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"))
    assert cands == ["https://art.v2player.top:8989/player/?url="
                     "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"], cands


# ---------------------------------------------------------------------- #
# 外壳页不得当播放地址返回
# ---------------------------------------------------------------------- #
class _Resolver:
    def __init__(self, result):
        self._result = result

    def resolve(self, *args, **kwargs):
        return self._result

    def invalidate(self, *args, **kwargs):
        return None


def _content_with(result) -> Content:
    content = Content(http=None, parser=Parser(), checker=None)
    content._stream_resolver = _Resolver(result)
    return content


def test_parser_shell_result_raises_instead_of_playing_blank():
    content = _content_with(("https://art.v2player.top:8989/player/?url=abc123", ""))
    source = type("S", (), {"source_id": "omofuna"})()
    with pytest.raises(ContentMissingError) as excinfo:
        content.fetch_video_streams(source, EP)
    assert "播放器外壳页" in str(excinfo.value)


def test_real_stream_result_passes_through():
    content = _content_with((INNER, ""))
    source = type("S", (), {"source_id": "omofuna"})()
    assert content.fetch_video_streams(source, EP) == (INNER, "")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
