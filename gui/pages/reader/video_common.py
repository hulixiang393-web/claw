"""视频视图公共小工具（log、取流错误归一、投屏历史路径、时间格式化）。

从 video_view.py 拆分而来（B4）：供 VideoView 与 video_tasks.py 共用，
避免拆分后出现模块循环导入。
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


def _safe_error_text(exc) -> str:
    text = str(exc) if exc is not None else ""
    text = re.sub(r"https?://[^\s)]+", "播放地址", text)
    text = re.sub(r"([?&])[^\s=]+=[^\s&)]*", "", text)
    lowered = text.lower()
    if "deadline" in lowered or "timeout" in lowered or "超时" in text:
        return "取流超时"
    return text or (exc.__class__.__name__ if exc is not None else "取流失败")


def _cast_history_path():
    """投屏历史文件路径：应用根目录 data/cast_history.json。

    与 library.json / reading_progress.json 同一 data 目录约定；
    复用 media_cache._base_dir（frozen 时为 exe 目录，开发时为项目根）。
    测试经 monkeypatch 本函数隔离到临时目录。
    """
    from framework.media_cache import _base_dir

    return _base_dir() / "data" / "cast_history.json"


def _fmt_time(ms: int) -> str:
    """毫秒 → mm:ss。"""
    ms = max(0, int(ms or 0))
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def _fmt_hms(seconds) -> str:
    """秒 → hh:mm:ss（投屏进度标签：电视端给的是秒，且可能超过 1 小时）。"""
    s = max(0, int(seconds or 0))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
