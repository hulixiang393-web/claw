# -*- coding: utf-8 -*-
"""播放记录门禁 + 取流预算回归测试。

覆盖两件事：
1. `player_launch_ok`：只有真正拉起播放器才允许回写播放进度
   （视频取流失败/播放器启动失败不得记成「最近阅读」）。
2. StreamResolver：**最后一个候选给足剩余总预算**，不再被 10s 单项超时
   丢弃——冷启动首次连接超过 10s 是「第一次打开播放失败、重试就好」的根因。
"""
from __future__ import annotations

import threading
import time

import pytest

from framework.external_player import player_launch_ok
from framework.stream_resolver import StreamBudget, StreamResolver


# --------------------------------------------------------------------------- #
# 播放记录门禁
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "msg, expected",
    [
        ("", False),                       # 取流失败：没有可播地址
        (None, False),
        ("   ", False),
        ("浏览器打开失败", False),           # 兜底也失败
        ("已用外部播放器打开", True),
        ("已用外部播放器打开（列表已截断，共 120 集）", True),
        ("已在浏览器中打开", True),
    ],
)
def test_player_launch_ok(msg, expected):
    assert player_launch_ok(msg) is expected


# --------------------------------------------------------------------------- #
# 取流预算：最后一个候选不被单项超时丢弃
# --------------------------------------------------------------------------- #
class _Pickr:
    """最小 Content 替身：_load_candidates 可注入候选回调列表。"""

    def __init__(self, candidates):
        self._candidates = candidates

    def resolve_video_stream_candidates(self, source, episode_url, quality, merged,
                                        deadline=None):
        return self._candidates

    def fetch_play_url(self, *args, **kwargs):  # pragma: no cover - 未使用
        return ""


def _resolver(candidates, *, candidate_timeout=0.1, total_budget=3.0):
    content = _Pickr(candidates)
    resolver = StreamResolver(
        content,
        total_budget=total_budget,
        candidate_timeout=candidate_timeout,
        max_candidates=6,
    )
    # 绕过 _load_candidates 的源解析细节，直接返回注入的候选
    resolver._load_candidates = (  # noqa: SLF001 - 测试替身
        lambda source, episode_url, quality, merged, context, key, source_lock: list(candidates)
    )
    return resolver


def _slow(value, delay):
    def run():
        time.sleep(delay)
        return value
    return run


def test_last_slow_candidate_is_awaited_instead_of_dropped():
    """唯一候选超出单项超时、但仍在宽限内 → 必须等到结果。

    candidate_timeout=0.1 → 宽限 0.2s；候选耗时 0.15s。
    旧行为在 0.1s 就放弃（返回空），新行为等到 0.15s 拿到地址。
    """
    resolver = _resolver([_slow("https://cdn/live.m3u8", 0.15)],
                         candidate_timeout=0.1, total_budget=3.0)
    started = time.monotonic()
    video, audio = resolver.resolve(object(), "episode")
    elapsed = time.monotonic() - started
    assert video == "https://cdn/live.m3u8"   # 旧实现会超时丢成空串
    assert elapsed >= 0.12


def test_non_last_candidate_still_times_out_fast():
    """还有后续候选时，单项超时仍然生效（快速失败换线路的语义不变）。"""
    calls = []

    def slow_first():
        calls.append("first")
        time.sleep(0.4)
        return "https://cdn/first.m3u8"

    def fast_second():
        calls.append("second")
        return "https://cdn/second.m3u8"

    resolver = _resolver([slow_first, fast_second],
                         candidate_timeout=0.1, total_budget=5.0)
    video, _audio = resolver.resolve(object(), "episode")
    assert video == "https://cdn/second.m3u8"
    assert calls[0] == "first" and "second" in calls


def test_last_candidate_grace_is_bounded_not_whole_budget():
    """宽限是**有上限**的（单项超时 ×2），不会把总预算耗在一个候选上。"""
    resolver = _resolver([_slow("https://cdn/live.m3u8", 1.5)],
                         candidate_timeout=0.1, total_budget=5.0)
    started = time.monotonic()
    video, audio = resolver.resolve(object(), "episode")
    elapsed = time.monotonic() - started
    assert (video, audio) == ("", "")      # 超出宽限仍按契约静默失败
    assert elapsed < 1.0                   # 没有等满 1.5s，更没有等满 5s 预算


def test_all_candidates_fail_returns_empty():
    resolver = _resolver([_slow("", 0.05)], candidate_timeout=0.1, total_budget=2.0)
    video, audio = resolver.resolve(object(), "episode")
    assert (video, audio) == ("", "")


def test_direct_media_url_short_circuits_without_candidates():
    resolver = _resolver([])
    video, _audio = resolver.resolve(object(), "https://cdn/a/index.m3u8")
    assert video == "https://cdn/a/index.m3u8"


def test_progress_gate_matches_requirement():
    """需求表述：视频只有拿到可播地址并成功启动播放器才记录；小说漫画点开即记。"""
    assert player_launch_ok("已用外部播放器打开") is True
    assert player_launch_ok("") is False
