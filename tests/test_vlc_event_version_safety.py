# -*- coding: utf-8 -*-
"""libvlc 事件绑定必须跨 python-vlc 版本安全（「播放报错」现场回归）。

现场故障：内嵌播放器一点就弹「播放失败」。根因不是流、不是网络——
python-vlc 3.0.21203 的 `vlc.EventType` 里**没有** `MediaPlayerStateChanged`
这个常量（libvlc 的状态变化是一组离散事件），而 `_install_events` 硬写了它：

    em.event_attach(vlc.EventType.MediaPlayerStateChanged, self._cb_state)
    # AttributeError: type object 'EventType' has no attribute 'MediaPlayerStateChanged'

异常发生在 `_ensure_player` 里 → 播放器根本没建起来 → 界面只剩「播放失败」。

修法：按表绑定离散事件（NothingSpecial/Opening/Playing/Paused/Stopped/
EndReached/Error），缺哪个就跳过哪个；合并式状态事件有就额外接上。
本文件只测绑定表本身（不加载 libvlc）。
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from framework import vlc_player  # noqa: E402
from framework.vlc_player import (  # noqa: E402
    _STATE_EVENT_TABLE,
    _attach_state_events,
    _make_union_state_cb,
)


class _FakeEventType:
    """模拟 python-vlc 的 EventType：只有给定的属性。"""

    def __init__(self, names):
        for i, name in enumerate(names):
            setattr(self, name, f"evt:{name}:{i}")


# 3.0.21203 实测存在的离散事件（没有 MediaPlayerStateChanged）
_DISCRETE = [name for name, _code in _STATE_EVENT_TABLE]
# 任何构建都不该缺这些：libvlc 从 2.x 起就有
_MUST_HAVE = ("MediaPlayerOpening", "MediaPlayerPlaying", "MediaPlayerPaused",
              "MediaPlayerStopped", "MediaPlayerEndReached")


def test_discrete_table_has_core_events():
    for name in _MUST_HAVE:
        assert name in _DISCRETE


def test_attach_works_when_state_changed_absent():
    """关键回归：枚举里没有 MediaPlayerStateChanged 也必须绑得上。"""
    event_type = _FakeEventType(_DISCRETE)     # 故意不给 MediaPlayerStateChanged
    attached, emitted = [], []

    names = _attach_state_events(event_type, lambda ev, cb: attached.append((ev, cb)),
                                 emitted.append)

    assert names == list(_DISCRETE)
    assert len(attached) == len(_DISCRETE)
    # 每个离散事件回调必须发出自己的状态码
    for (_ev, cb), (_name, code) in zip(attached, _STATE_EVENT_TABLE):
        cb()
        assert emitted[-1] == code


def test_missing_events_are_skipped_not_raised():
    """只提供部分事件时跳过缺失项，绝不抛异常。"""
    event_type = _FakeEventType(["MediaPlayerPlaying", "MediaPlayerPaused"])
    attached, emitted = [], []

    names = _attach_state_events(event_type, lambda ev, cb: attached.append(cb),
                                 emitted.append)

    assert names == ["MediaPlayerPlaying", "MediaPlayerPaused"]
    attached[0]()
    assert emitted[-1] == 3
    attached[1]()
    assert emitted[-1] == 4


def test_union_event_used_when_present():
    """有合并式事件（MediaStateChanged）时顺手接上，读 u.new_state。"""
    event_type = _FakeEventType(["MediaPlayerPlaying", "MediaStateChanged"])
    attached, emitted = [], []

    names = _attach_state_events(event_type, lambda ev, cb: attached.append(cb),
                                 emitted.append)

    assert "MediaStateChanged" in names
    union_cb = attached[-1]
    union_cb(SimpleNamespace(u=SimpleNamespace(new_state=2)))
    assert emitted[-1] == 2
    # 字段形态不同（bytes / 枚举）也要能读
    union_cb(SimpleNamespace(u=SimpleNamespace(new_state=b"\x03\x00\x00\x00")))
    assert emitted[-1] == 3


def test_union_event_ignores_unknown_payload():
    emitted = []
    cb = _make_union_state_cb(emitted.append)
    cb(None)                                    # 无 payload
    cb(SimpleNamespace(u=SimpleNamespace()))    # 无 new_state 字段
    assert emitted == []


def test_attach_failure_on_one_event_does_not_stop_others():
    """单个事件绑不上（libvlc 报错）时，其余事件照常绑定。"""
    event_type = _FakeEventType(["MediaPlayerPlaying", "MediaPlayerPaused"])
    calls = []

    def attach(ev, cb):
        if "Playing" in str(ev):
            raise RuntimeError("libvlc 不认这个事件")
        calls.append(ev)

    names = _attach_state_events(event_type, attach, lambda _c: None)

    assert names == ["MediaPlayerPaused"]
    assert len(calls) == 1


def test_no_hardcoded_state_changed_constant():
    """可执行代码里不得再出现硬编码的 EventType.MediaPlayerStateChanged。

    只查代码行（注释里讲由来是允许的），并且只查真正会执行的属性访问。
    """
    src = Path(vlc_player.__file__).read_text(encoding="utf-8")
    code_lines = [ln for ln in src.splitlines()
                  if not ln.lstrip().startswith("#")]
    bad = [ln.strip() for ln in code_lines
           if "EventType.MediaPlayerStateChanged" in ln]
    assert bad == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
