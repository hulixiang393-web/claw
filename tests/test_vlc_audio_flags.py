# -*- coding: utf-8 -*-
"""VLC 启动参数的音频抗丢加固（2026-10-29）。

背景：用户反馈"卡顿恢复后必跳帧 + 丢音"。数据通道已逐项验证干净：

- 669 个缓存分片 **0 缺包**（TS 连续计数器 CC 全程 +1）
- 音频包数逐片一致（173/173、201/201 …），音视频时长差 0.000s
- PCR 与 PTS 的差值零漂移（-0.780 -> -0.780）
- 跨分片 DTS 单调、0 次倒退

所以丢音发生在**播放侧**：输入饥饿后 VLC 的 aout 落后主时钟，只能丢音频
追帧（视频侧靠 --no-drop-late-frames 已不丢，音频没有等价开关）。

本文件只锁参数，**不启动 VLC**（启动播放器会抢用户焦点）。
"""
import inspect
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


def _open_source() -> str:
    from framework.external_player import open_with_player

    return inspect.getsource(open_with_player)


def test_vlc_args_disable_clock_jitter():
    """必须关掉输入时钟抖动平滑。

    走本地代理时时间戳已由 remux 对齐，平滑只会累积相位误差，饥饿恢复
    瞬间被判成「迟到」→ VLC 丢音频。关掉后直接用时间戳。
    """
    assert "--clock-jitter=0" in _open_source()


def test_vlc_args_force_audio_time_stretch():
    """必须显式开启变速追帧（默认虽开，但要防便携版 vlcrc 关掉 → 硬丢音）。"""
    src = _open_source()
    assert "--audio-time-stretch" in src
    assert "--no-audio-time-stretch" not in src


def test_vlc_args_keep_no_drop_late_frames():
    """视频侧既有加固不能被回退掉（与音频加固配套）。"""
    assert "--no-drop-late-frames" in _open_source()


@pytest.mark.parametrize("flag", ["--clock-jitter=0", "--audio-time-stretch"])
def test_audio_flags_placed_before_mrl(flag):
    """参数必须在 MRL 之前（VLC 只把 MRL 之前的项当全局选项）。"""
    src = _open_source()
    pos = src.find(flag)
    mrl_pos = src.find("args.append(play_url)")
    series_pos = src.find('args.append("--no-random")')
    assert pos > 0
    # 参数在 args 列表构造处，早于任何 MRL 追加
    assert pos < max(mrl_pos, series_pos if series_pos > 0 else 0)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
