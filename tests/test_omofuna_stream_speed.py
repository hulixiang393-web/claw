# -*- coding: utf-8 -*-
"""omofuna 取流提速回归（离线，不联网）。

覆盖：
- `_swap_line_sid`：分集 URL 的线路坐标（`/play/<sid>/` 路径段 + `sid=` 参数）
  一起换掉；不含坐标的 URL 不产出假候选。
- `_switch_candidate_episode_urls_fast`：线路集合直接读已缓存的播放页线路 tab，
  **零 HTTP 请求**就给出同集其它线路 URL（慢站换线不再等十几秒）。
- 配置回归：`play_url.hls_relay` 已移除（中继会让相对分片拼成中继地址 → 404/
  SSL，播放反复重试就是卡顿），播放地址只保留 CDN 原地址。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.parser import Parser  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SOURCE_PATH = os.path.join(ROOT, "sources", "omofuna.json")
_EP = "https://www.omofuna.com/anime/beff239b9a321b24f8a2c4ac/play/2/3.html?sid=2"

_PLAY_HTML = """<!DOCTYPE html><html><body>
<div class="play-channel">
  <a class="play-channel-item" data-sid="1">精品</a>
  <a class="play-channel-item" data-sid="2">暴风</a>
  <a class="play-channel-item" data-sid="3">天堂</a>
  <a class="play-channel-item" data-sid="4">量子</a>
</div>
</body></html>"""


class _BoomHttp:
    """任何请求都算失败：快路径必须是零请求。"""

    defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _fail(*args, **kwargs):
            self.calls.append((name, args[:1]))
            raise AssertionError(f"快路径不应发请求：{name}{args[:1]}")

        return _fail

    def close(self):
        pass


def _source() -> SourceConfig:
    with open(_SOURCE_PATH, encoding="utf-8") as handle:
        return SourceConfig.from_dict(json.load(handle), _SOURCE_PATH)


def _content(http=None):
    return Content(http or _BoomHttp(), Parser(), None)


def test_swap_line_sid_moves_path_and_query():
    from framework.content.video import VideoMixin

    out = VideoMixin._swap_line_sid(_EP, "2", "3")
    assert out == "https://www.omofuna.com/anime/beff239b9a321b24f8a2c4ac/play/3/3.html?sid=3"
    # 集数不能被动到：/3.html 里的 3 是集号
    assert out.count("/3.html") == 1


def test_swap_line_sid_requires_line_coordinates():
    from framework.content.video import VideoMixin

    # 只有路径段没有 sid 参数：仍可按路径换线（站点两处坐标是冗余的）
    assert VideoMixin._swap_line_sid(
        "https://x/anime/abc/play/2/3.html", "2", "3"
    ) == "https://x/anime/abc/play/3/3.html"
    # 同一条线路（cur == sid）不产出候选
    assert VideoMixin._swap_line_sid(_EP, "2", "2") == ""
    # 完全没有线路坐标的 URL 不产出假候选
    assert VideoMixin._swap_line_sid(
        "https://x/anime/abc/3.html", "2", "3"
    ) == ""


def test_fast_candidates_read_cached_play_page_without_requests():
    import time

    http = _BoomHttp()
    content = _content(http)
    source = _source()
    abs_ep = content._abs_url(source, _EP)
    content._play_page_cache = {
        (source.source_id, abs_ep): (time.monotonic() + 60, _PLAY_HTML),
    }

    candidates = content._switch_candidate_episode_urls_fast(source, _EP)

    assert http.calls == []           # 零请求
    assert [c.rsplit("sid=", 1)[-1] for c in candidates] == ["1", "3", "4"]
    assert all("/play/" in c and "?sid=" in c for c in candidates)
    # 当前线路（sid=2）不重复出现，集号保持 3
    assert not any("/play/2/" in c for c in candidates)
    assert all("/3.html" in c for c in candidates)


def test_fast_candidates_empty_without_cached_page():
    content = _content()
    assert content._switch_candidate_episode_urls_fast(_source(), _EP) == []


def test_config_has_no_hls_relay_and_keeps_direct_playlist():
    """中继已停用：保留中继会让相对分片解析错（播放卡顿的根因）。"""
    source = _source()
    play_cfg = (
        source.raw["endpoints"]["content"]["episode"]["play_url"]
    )
    assert "hls_relay" not in play_cfg
    # 播放地址只留原地址：不再被包成 https://m3u8xx.sgzm.net:2087/…
    wrapped = Content._with_relay_candidates(
        play_cfg, ["https://fengbao13.com/video/x/index.m3u8"]
    )
    assert wrapped == ["https://fengbao13.com/video/x/index.m3u8"]
