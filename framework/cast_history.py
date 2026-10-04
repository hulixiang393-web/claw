"""投屏设备历史记忆（cast_history.py）。

独立 JSON 文件 data/cast_history.json（与 app_config.json 无关）记录
最近投屏成功的 DLNA 设备，供两条链路复用：

1. 播放页直投上次设备（取流完成即 start，不等 SSDP 扫描）；
2. 设备选择对话框把历史设备置顶展示、上次使用默认选中。

条目 = DlnaRenderer 的 6 个字段 + last_used 时间戳（epoch 秒）。
存储按 UDN 去重置顶、最多 MAX_ENTRIES 条、原子写入（tmp + replace）；
JSON 缺失/损坏/含非法条目一律容错为空，不抛给调用方。

纯逻辑无 Qt：GUI 层（gui/pages/reader/video_view.py、
gui/components/cast_dialog.py）只经本模块读写历史。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

# 历史条数上限（最近 N 台）
MAX_ENTRIES = 10

# DlnaRenderer 完整序列化的字段（与 framework.dlna.DlnaRenderer 一致）
RENDERER_FIELDS = (
    "udn",
    "friendly_name",
    "location",
    "av_transport_control_url",
    "rendering_control_url",
    "service_type",
)

# 还原 DlnaRenderer 必需的字段（rendering_control_url 可为 None）
_REQUIRED_FIELDS = tuple(f for f in RENDERER_FIELDS if f != "rendering_control_url")


def _is_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def entry_from_renderer(renderer) -> dict | None:
    """DlnaRenderer（或同字段 dict）→ 历史条目；非法输入返回 None。"""
    if renderer is None:
        return None
    if isinstance(renderer, dict):
        get = renderer.get
    else:
        get = lambda key: getattr(renderer, key, None)  # noqa: E731
    entry = {field: get(field) for field in RENDERER_FIELDS}
    for field in _REQUIRED_FIELDS:
        if not _is_str(entry.get(field)):
            return None
    rc = entry.get("rendering_control_url")
    if rc is not None and not isinstance(rc, str):
        return None
    return entry


def renderer_from_entry(entry) -> "DlnaRenderer | None":
    """历史条目 → DlnaRenderer；条目非法（非 dict / 缺字段）返回 None。"""
    if not isinstance(entry, dict):
        return None
    values = {field: entry.get(field) for field in RENDERER_FIELDS}
    for field in _REQUIRED_FIELDS:
        if not _is_str(values.get(field)):
            return None
    rc = values.get("rendering_control_url")
    if rc is not None and not isinstance(rc, str):
        return None
    from framework.dlna import DlnaRenderer

    return DlnaRenderer(**values)


class CastHistory:
    """投屏设备历史（一个 JSON 文件的读写门面）。

    用法::

        history = CastHistory(Path("data/cast_history.json"))
        history.record(renderer)      # 投屏成功即记录（UDN 去重置顶）
        last = history.last()         # 上次使用（dict 或 None）
        history.entries()             # 全部条目（最近在前）
        history.clear()               # 用户点「清除历史」

    所有读取对损坏文件容错；写入为 tmp + os.replace 原子替换，
    落盘失败只记日志（内存状态仍正确，本次会话可继续用）。
    """

    def __init__(self, path):
        self.path = Path(path)
        self._entries: list[dict] = self._load()

    # ---- 读 ---- #
    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError, UnicodeDecodeError):
            log.warning("投屏历史文件不可读，按空历史处理：%s", self.path,
                        exc_info=True)
            return []
        if not isinstance(raw, list):
            return []
        entries = []
        for item in raw:
            if entry_from_renderer(item) is None:
                continue
            entries.append(dict(item))
        return entries[:MAX_ENTRIES]

    def entries(self) -> list[dict]:
        """全部条目（最近使用在前）；返回副本。"""
        return [dict(entry) for entry in self._entries]

    def last(self) -> dict | None:
        """上次投屏成功的设备条目；无历史返回 None。"""
        return dict(self._entries[0]) if self._entries else None

    # ---- 写 ---- #
    def record(self, renderer, now=None) -> dict | None:
        """记录一次成功投屏：UDN 去重置顶、截断到上限、写盘。

        renderer 非法（无 udn / 字段不全）返回 None 且不落盘。
        now 可注入时间戳（epoch 秒），默认 time.time()。
        """
        entry = entry_from_renderer(renderer)
        if entry is None:
            log.warning("投屏历史忽略非法设备记录：%r", renderer)
            return None
        entry["last_used"] = float(time.time() if now is None else now)
        udn = entry["udn"]
        self._entries = [e for e in self._entries if e.get("udn") != udn]
        self._entries.insert(0, entry)
        del self._entries[MAX_ENTRIES:]
        self._save()
        return dict(entry)

    def clear(self) -> None:
        """清空历史（内存 + 文件）。"""
        self._entries = []
        self._save()

    def _save(self) -> None:
        """原子写入：同目录 tmp 文件 + os.replace；失败只记日志。"""
        temp_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp",
                dir=str(self.path.parent),
            )
            os.close(fd)
            temp_path = Path(temp_name)
            temp_path.write_text(
                json.dumps(self._entries, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)  # 同卷原子替换
            temp_path = None
        except (OSError, TypeError, ValueError):
            log.warning("投屏历史写入失败：%s", self.path, exc_info=True)
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
