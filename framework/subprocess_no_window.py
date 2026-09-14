"""Windows 下静默子进程执行（不弹控制台窗口）。

Python 执行外部程序（yt-dlp/ffmpeg/VLC 等）时，若不用
CREATE_NO_WINDOW 标志，Windows 会为每个子进程新建一个可见的
控制台窗口——批量加载/下载时会出现大量黑色「Python 弹窗」。

仅 Windows 生效：非 Windows 平台返回空 kwargs，自动跳过。
"""

import os
import subprocess
import sys

_CREATE_NO_WINDOW = 0x08000000


def no_window_kwargs() -> dict:
    """返回可传给 subprocess.run/Popen 的 kwargs，抑制子进程控制台窗口。

    用法：
        subprocess.run(cmd, ..., **no_window_kwargs())
        subprocess.Popen(cmd, ..., **no_window_kwargs())
    """
    if os.name == "nt":
        return {"creationflags": _CREATE_NO_WINDOW}
    return {}


def run(cmd, **kwargs) -> "subprocess.CompletedProcess":
    """subprocess.run 的无窗口版本（Windows 静默，其他平台等价）。"""
    return subprocess.run(cmd, **no_window_kwargs(), **kwargs)


def popen(cmd, **kwargs) -> "subprocess.Popen":
    """subprocess.Popen 的无窗口版本（Windows 静默，其他平台等价）。"""
    return subprocess.Popen(cmd, **no_window_kwargs(), **kwargs)


__all__ = ["no_window_kwargs", "run", "popen", "_CREATE_NO_WINDOW"]