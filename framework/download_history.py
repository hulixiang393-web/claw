"""下载记录持久化（download_history.py）。

DownloadQueue 的任务纯内存、退出即丢；本模块把任务序列化到 data/downloads.json，
重启后由 DownloadQueue 恢复：

- 恢复规则：DONE 任务原样保留；其余状态（WAITING/DOWNLOADING/PAUSED/CANCELED/
  FAILED）统一归一化为 FAILED，error 改为「程序退出时任务未完成」——未完成任务的
  重试/重下走 queue 既有 retry_task/restart_task 逻辑，不自动续传、不自动派发。
- 只持久化「展示 + 恢复」白名单字段；epub_chapters（漫画图片字节）等大型/运行时
  字段绝不入 JSON。文件损坏/缺失静默返回空，不抛异常（与 library.json 同风格）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

HISTORY_VERSION = 1

# 恢复未完成任务的统一错误提示
EXIT_UNFINISHED_MSG = "程序退出时任务未完成"


def _serialize(task) -> dict:
    """DownloadTask → dict（白名单，排除运行时/大型字段）。"""
    return {
        "task_id": task.task_id,
        "source_id": task.source_id,
        "content_type": task.content_type,
        "url": task.url,
        "title": task.title,
        "chapters": [
            {"title": c.title, "url": c.url, "source_id": c.source_id, "cover": c.cover}
            for c in (task.chapters or [])
        ],
        "selected": list(task.selected),
        "quality": task.quality,
        "total": task.total,
        "status": task.status,
        "error": task.error,
        "out_dir": task.out_dir,
        "epub_ok": bool(task.epub_ok),
        "has_ads": bool(task.has_ads),
        "start_time": task.start_time or 0.0,
        "end_time": task.end_time or 0.0,
        "parallel": task.parallel,
    }


def _deserialize(data: dict):
    """dict → DownloadTask（未完成任务统一归一化为 FAILED）。

    除外层白名单字段外，运行时缺省全部重新初始化：cancel_evt/pause_evt 重建、
    epub_chapters/done/done_chapters/failed 等下载进度记忆置空——这样恢复出的
    FAILED 任务点「重试」会走 retry_task 的「整体失败」分支（整本安全重下），
    不依赖丢失的中间材料。
    """
    from .content import Chapter
    from .download_queue import DownloadTask, TaskStatus

    chapters = []
    for c in data.get("chapters") or []:
        if not isinstance(c, dict):
            continue
        chapters.append(
            Chapter(
                title=c.get("title") or "",
                url=c.get("url") or "",
                source_id=c.get("source_id") or "",
                cover=c.get("cover") or "",
            )
        )
    status = data.get("status") or TaskStatus.FAILED
    if status == TaskStatus.DONE:
        error = data.get("error") or ""
    else:
        status = TaskStatus.FAILED
        error = EXIT_UNFINISHED_MSG
    selected = list(data.get("selected") or [])
    return DownloadTask(
        task_id=data.get("task_id") or "",
        source_id=data.get("source_id") or "",
        content_type=data.get("content_type") or "novel",
        url=data.get("url") or "",
        title=data.get("title") or data.get("task_id") or "",
        chapters=chapters,
        selected=selected,
        quality=data.get("quality") or "",
        total=int(data.get("total") or 0),
        status=status,
        error=error,
        out_dir=data.get("out_dir") or "",
        epub_ok=bool(data.get("epub_ok")),
        has_ads=bool(data.get("has_ads")),
        start_time=float(data.get("start_time") or 0.0),
        end_time=float(data.get("end_time") or 0.0),
        parallel=int(data.get("parallel") or 1),
    )


class DownloadHistory:
    """data/downloads.json 读写。线程安全由调用方（DownloadQueue._save）保证。"""

    def __init__(self, path: str | Path):
        self._path = Path(path)

    # ------------------------------------------------------------------ #
    def load(self) -> List:
        """读取并恢复任务列表。文件缺失/损坏 → []（不抛异常）。"""
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        tasks = []
        for item in data.get("tasks") or []:
            if not isinstance(item, dict):
                continue
            try:
                tasks.append(_deserialize(item))
            except Exception:  # noqa: BLE001 —— 单条损坏跳过，不阻塞整体恢复
                continue
        return tasks

    def save(self, tasks) -> None:
        """全量写盘。写失败静默（下载不阻塞）。"""
        payload = {
            "version": HISTORY_VERSION,
            "tasks": [_serialize(t) for t in tasks],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError:
            pass

    def clear(self) -> None:
        """删除记录文件（需配合调用方清空 tasks）。"""
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass