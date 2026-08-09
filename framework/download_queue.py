"""下载队列（download_queue.py）。

管理下载任务的并发调度、暂停/继续/取消、失败重试与进度广播。

- 并发槽：active 数 < concurrent 时从 WAITING 队首取任务启动 worker
- 事件：经 EventBus 广播（source_id = task_id，供 MiniProgress 划归）
- 框架层纯 Python，不依赖 GUI；UI 线程安全由 GUI 侧订阅方自行桥接

事件契约：
    DOWNLOAD_STARTED   payload={"task_id","title","total"}
    DOWNLOAD_PROGRESS  payload={"task_id","done","total","speed_mbs","title"}
    DOWNLOAD_COMPLETED payload={"task_id","title","done","total"}
    DOWNLOAD_FAILED    payload={"task_id","title","error"}
对应 core.md「download_queue.py 下载队列」与 ui-download.md。
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

try:
    from PySide6.QtCore import QThreadPool, QRunnable
except ImportError:  # 纯 Python 环境（无 Qt）时降级 threading
    QThreadPool = None
    QRunnable = None

from .downloader import Downloader
from .errors import SourceError
from .events import (
    Event,
    EVENT_DOWNLOAD_COMPLETED,
    EVENT_DOWNLOAD_FAILED,
    EVENT_DOWNLOAD_PROGRESS,
    EVENT_DOWNLOAD_STARTED,
)


class TaskStatus:
    """任务状态常量。"""

    WAITING = "waiting"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    CANCELED = "canceled"
    DONE = "done"
    FAILED = "failed"


# 单章失败最大尝试次数（含首次）
MAX_ATTEMPTS = 3


@dataclass
class DownloadTask:
    """一个下载任务（一本书/一部作品的全部或指定章节）。"""

    task_id: str
    source_id: str
    content_type: str
    url: str
    title: str
    chapters: List = field(default_factory=list)      # List[Chapter]
    selected: List[bool] = field(default_factory=list)  # 与 chapters 等长的勾选
    quality: str = ""                                    # 视频画质（best/1080p/...），非视频为空
    epub_chapters: List = field(default_factory=list)    # epub 累积材料（novel:文本 / comic:图字节）
    epub_ok: bool = False                                # epub 是否已成功合成
    status: str = TaskStatus.WAITING
    done: int = 0
    total: int = 0
    failed: List[str] = field(default_factory=list)   # 失败章节标题
    error: str = ""                                    # 最近一次错误信息
    bytes_written: int = 0
    done_chapters: List[int] = field(default_factory=list)  # 已完成章节索引（resume 重跑时跳过）
    dispatched: bool = False                        # 「已派发未完成」标记（resume 判断 worker 是否在跑）
    merge_progress: int = 0        # 视频合并已输出字节（UI 显示用）
    has_ads: bool = False          # 视频任务：是否检测到流内广告段（加任务时预检）
    ad_segments: dict = field(default_factory=dict)  # {章节索引: 广告段序号列表}
    start_time: float = 0.0
    end_time: float = 0.0
    out_dir: str = ""
    cancel_evt: threading.Event = field(default_factory=threading.Event)
    pause_evt: threading.Event = field(default_factory=threading.Event)
    worker: Optional[threading.Thread] = None          # 当前 worker 线程

    # ------------------------------------------------------------------ #
    @property
    def elapsed(self) -> float:
        """已用秒数（含暂停期，简单估算）。"""
        if self.start_time <= 0:
            return 0.0
        end = self.end_time if self.end_time > 0 else time.time()
        return max(0.0, end - self.start_time)

    @property
    def remaining_s(self) -> float:
        """剩余时间估算：剩余章节 × (已用/已下)。ui-download.md 公式。"""
        if self.done <= 0 or self.total <= 0:
            return 0.0
        return max(0.0, (self.total - self.done) * self.elapsed / self.done)

    @property
    def speed_mbs(self) -> float:
        """平均下载速度（MB/s）。"""
        if self.elapsed <= 0 or self.bytes_written <= 0:
            return 0.0
        return (self.bytes_written / 1048576.0) / self.elapsed


class DownloadQueue:
    """并发下载队列（线程安全）。"""

    def __init__(self, content, http, settings, source_manager, event_bus=None):
        self._content = content
        self._http = http
        self._settings = settings
        self._manager = source_manager
        self._bus = event_bus
        self._downloader = Downloader(content, http, settings)
        self._lock = threading.RLock()
        self._tasks: List[DownloadTask] = []
        self._seq = itertools.count(1)
        self._concurrent = max(
            1, int(settings.get("download", "max_concurrent_downloads", 6))
        )
        self._active_workers = 0

    # ------------------------------------------------------------------ #
    # 事件广播
    # ------------------------------------------------------------------ #
    def _emit(self, etype: str, payload: dict, task: DownloadTask) -> None:
        if self._bus is None:
            return
        try:
            self._bus.emit(Event(etype, payload, source_id=task.task_id))
        except Exception:
            pass  # 订阅者异常不影响下载

    # ------------------------------------------------------------------ #
    # 对外 API
    # ------------------------------------------------------------------ #
    def add_task(self, detail, selected: Optional[List[bool]] = None, quality: str = "") -> DownloadTask:
        """加入下载任务。selected 与 detail.chapters 等长（False 跳过）；省略则全选。
        quality: 视频画质（best/1080p/...），非视频忽略。"""
        chapters = list(detail.chapters or [])
        if selected is None:
            selected = [True] * len(chapters)
        selected = list(selected)
        if len(selected) < len(chapters):
            selected.extend([True] * (len(chapters) - len(selected)))
        selected = selected[: len(chapters)]

        with self._lock:
            task = DownloadTask(
                task_id=f"dl-{next(self._seq)}",
                source_id=detail.source_id,
                content_type=detail.content_type,
                url=detail.url,
                title=detail.title or detail.url,
                chapters=chapters,
                selected=selected,
                quality=quality,
                total=sum(selected),
            )
            task.out_dir = str(self._downloader.book_dir(task))
            self._tasks.append(task)
        self._emit(
            EVENT_DOWNLOAD_STARTED,
            {"task_id": task.task_id, "title": task.title, "total": task.total},
            task,
        )
        # 视频任务：后台预检 m3u8 流内广告段（不阻塞入队；失败静默，下载时再过滤）
        if detail.content_type == "video" and task.total > 0:
            self._spawn_ad_precheck(task)
        self._maybe_dispatch()
        return task

    def _spawn_ad_precheck(self, task: DownloadTask) -> None:
        """后台预检视频集 m3u8 广告段：取流 → detect_m3u8_ads → 标记 has_ads。

        选中的每集都取一次流 URL（fetch_video_streams 带缓存/解密），对 HLS
        播放列表做广告段检测。结果写入 task.has_ads / task.ad_segments，
        UI 可据此展示「含广告，下载将自动剔除」。失败（网络/超时/限流）静默：
        不影响入队，下载时 filter_m3u8 兜底。
        """
        def _precheck() -> None:
            from .adblock import adblock_for

            source = self._manager.get(task.source_id) if self._manager else None
            if source is None:
                return
            ad = adblock_for(source)
            if not ad.enabled:
                return
            found: dict = {}
            for i, ch in enumerate(task.chapters):
                if not task.selected[i]:
                    continue
                if task.cancel_evt.is_set():
                    return
                try:
                    video, _audio = self._content.fetch_video_streams(
                        source, ch.url, quality=task.quality or ""
                    )
                except Exception:  # noqa: BLE001
                    continue  # 取流失败：静默跳过，下载时再试
                if not video or ".m3u8" not in video.lower():
                    continue  # 非 HLS（mp4/dash）不走 m3u8 广告段检测
                try:
                    text = self._http.get_text(
                        video, headers=source.request_headers(), timeout=15, retries=1
                    )
                except Exception:  # noqa: BLE001
                    continue
                if not text or "#EXTM3U" not in text:
                    continue
                ad_segs, _cleaned = ad.detect_m3u8_ads(text, video)
                if ad_segs:
                    found[i] = ad_segs
            if found:
                with self._lock:
                    task.has_ads = True
                    task.ad_segments = found
                # 通知 UI：任务卡片可展示广告检测结果
                self._emit(
                    EVENT_DOWNLOAD_PROGRESS,
                    {
                        "task_id": task.task_id,
                        "done": task.done,
                        "total": task.total,
                        "speed_mbs": task.speed_mbs,
                        "title": task.title,
                        "has_ads": True,
                    },
                    task,
                )

        t = threading.Thread(target=_precheck, daemon=True, name=f"adcheck-{task.task_id}")
        t.start()

    def tasks(self) -> List[DownloadTask]:
        """全部任务快照（调用方自行按状态分组）。"""
        with self._lock:
            return list(self._tasks)

    def get(self, task_id: str) -> Optional[DownloadTask]:
        with self._lock:
            return self._find(task_id)

    # ---- 单任务控制 ---------------------------------------------------- #
    def pause_task(self, task_id: str) -> None:
        with self._lock:
            t = self._find(task_id)
            if t is None:
                return
            if t.status == TaskStatus.WAITING:
                t.status = TaskStatus.PAUSED  # 未启动即暂停
            elif t.status in (TaskStatus.DOWNLOADING, TaskStatus.PAUSED):
                # 同步置 PAUSED：resume_task 据此判断，避免"暂停→立即继续"竞态
                # （否则 worker 到章节边界才置 PAUSED，期间点继续会被 status!=PAUSED 挡掉）
                t.pause_evt.set()
                t.status = TaskStatus.PAUSED
            else:
                return
        # 通知 UI：暂停后按钮变「继续」（下载页据此刷新卡片）
        self._emit_refresh(t)

    def resume_task(self, task_id: str) -> None:
        with self._lock:
            t = self._find(task_id)
            if t is None or t.status != TaskStatus.PAUSED:
                return
            t.pause_evt.clear()
            if t.dispatched:
                t.status = TaskStatus.DOWNLOADING  # worker 仍在运行，直接恢复
            else:
                # worker 已结束（或从未启动）：重新派发。done_chapters 保留，
                # 重跑时跳过已完成章并补计数；epub_chapters 保留（对应章内容已累积），
                # 避免整本重跑后 done/epub_chapters 翻倍或缺内容。
                t.status = TaskStatus.WAITING
                t.done = 0
                t.bytes_written = 0
        self._maybe_dispatch()
        self._emit_refresh(t)

    def cancel_task(self, task_id: str) -> None:
        with self._lock:
            t = self._find(task_id)
            if t is None:
                return
            t.cancel_evt.set()
            t.pause_evt.clear()  # 解除暂停阻塞，让 worker 尽快退出
            if t.status in (TaskStatus.WAITING, TaskStatus.PAUSED):
                # 无运行中 worker，直接标记；运行中由 worker 在章节边界退出
                t.status = TaskStatus.CANCELED
                t.end_time = time.time()
        # 通知 UI：立即反映取消（下载页刷新卡片）
        self._emit_refresh(t)

    def _emit_refresh(self, task: DownloadTask) -> None:
        """发一个进度事件驱动下载页刷新（暂停/继续/取消后按钮状态变化）。"""
        with self._lock:
            done = task.done
            total = task.total
        self._emit(
            EVENT_DOWNLOAD_PROGRESS,
            {"task_id": task.task_id, "done": done, "total": total,
             "speed_mbs": task.speed_mbs, "title": task.title},
            task,
        )

    def retry_task(self, task_id: str) -> None:
        """失败任务清零计数重新入队。"""
        with self._lock:
            t = self._find(task_id)
            if t is None or t.status != TaskStatus.FAILED:
                return
            t.status = TaskStatus.WAITING
            t.failed = []
            t.error = ""
            t.done = 0
            t.bytes_written = 0
            t.epub_chapters = []      # 重试前清空累积，避免重下后内容翻倍
            t.done_chapters = []      # 全新下载：清空已完成记录，避免跳过章节
            t.start_time = 0.0
            t.end_time = 0.0
            t.cancel_evt = threading.Event()
            t.pause_evt = threading.Event()
        self._maybe_dispatch()

    def remove_done(self, task_id: str) -> None:
        """从列表移除（文件保留）。仅限终态：DONE / FAILED / CANCELED。"""
        with self._lock:
            t = self._find(task_id)
            if t is None:
                return
            if t.status in (
                TaskStatus.DONE,
                TaskStatus.FAILED,
                TaskStatus.CANCELED,
            ):
                self._tasks.remove(t)

    # ---- 队列级控制 ---------------------------------------------------- #
    def pause_all(self) -> None:
        with self._lock:
            for t in self._tasks:
                if t.status in (TaskStatus.WAITING, TaskStatus.DOWNLOADING):
                    self._do_pause(t)

    def resume_all(self) -> None:
        with self._lock:
            for t in self._tasks:
                if t.status == TaskStatus.PAUSED:
                    t.pause_evt.clear()
                    if t.dispatched:
                        t.status = TaskStatus.DOWNLOADING
                    else:
                        t.status = TaskStatus.WAITING
                        t.done = 0
                        t.bytes_written = 0
        self._maybe_dispatch()

    def cancel_all(self) -> None:
        with self._lock:
            for t in self._tasks:
                t.cancel_evt.set()
                t.pause_evt.clear()
                if t.status in (TaskStatus.WAITING, TaskStatus.PAUSED):
                    t.status = TaskStatus.CANCELED
                    t.end_time = time.time()

    def set_concurrency(self, n: int) -> None:
        """运行时调整并发槽（clamp 1~8）。"""
        with self._lock:
            self._concurrent = max(1, min(8, int(n)))
        self._maybe_dispatch()

    @property
    def concurrent(self) -> int:
        with self._lock:
            return self._concurrent

    # ------------------------------------------------------------------ #
    # 调度
    # ------------------------------------------------------------------ #
    def _find(self, task_id: str) -> Optional[DownloadTask]:
        return next((t for t in self._tasks if t.task_id == task_id), None)

    @staticmethod
    def _do_pause(t: DownloadTask) -> None:
        if t.status == TaskStatus.WAITING:
            t.status = TaskStatus.PAUSED
        elif t.status == TaskStatus.DOWNLOADING:
            t.pause_evt.set()
            t.status = TaskStatus.PAUSED

    def _maybe_dispatch(self) -> None:
        """并发槽有空位且队首有 WAITING → 启动 worker。

        派发时**立即把任务标记为 DOWNLOADING**（而非等 worker 线程起来再标）：
        否则在启动线程的窗口内，另一次 dispatch 会再次选到该 WAITING 任务，
        导致同一任务被派发两次、双 worker 同时下载（done 翻倍）。
        """
        while True:
            with self._lock:
                if self._active_workers >= self._concurrent:
                    return
                nxt = next(
                    (t for t in self._tasks if t.status == TaskStatus.WAITING), None
                )
                if nxt is None:
                    return
                # 先标记，杜绝重复派发竞态
                nxt.status = TaskStatus.DOWNLOADING
                nxt.dispatched = True  # 「已派发未完成」标记（resume 判断 worker 在跑用）
                self._active_workers += 1
            self._spawn_worker(nxt)

    def _spawn_worker(self, task: DownloadTask) -> None:
        """启动 worker：优先 Qt QThreadPool（GUI 响应不卡顿），降级 threading。"""
        if QThreadPool is not None:
            runner = _DownloadWorker(self, task)
            with self._lock:
                task.worker = runner  # ref（QRunnable 也做句柄）
            QThreadPool.globalInstance().start(runner)
            return
        th = threading.Thread(
            target=self._run_task, args=(task,), name=f"dl-{task.task_id}", daemon=True
        )
        with self._lock:
            task.worker = th
        th.start()

    # ------------------------------------------------------------------ #
    # worker 执行
    # ------------------------------------------------------------------ #
    def _run_task(self, task: DownloadTask) -> None:
        with self._lock:
            task.status = TaskStatus.DOWNLOADING
            task.start_time = time.time()
        try:
            self._download_all(task)
            # 小说/漫画：全部章节下载完后合成为单本 epub（只产出 epub）
            if task.content_type in ("novel", "comic"):
                epub_path = self._downloader.finalize_epub(task)
                with self._lock:
                    task.epub_ok = True
                    task.out_dir = str(epub_path)  # 记录 epub 落盘位置
        except Exception as exc:  # noqa: BLE001 —— 兜底，整任务失败
            with self._lock:
                task.error = str(exc)
                task.failed.append("（整体）")
                task.status = TaskStatus.FAILED
        finally:
            with self._lock:
                task.worker = None
                task.dispatched = False  # 真正结束才清位（供 resume 判断）
                self._active_workers -= 1
                task.end_time = time.time()
            self._emit_final(task)
            self._maybe_dispatch()

    def _download_all(self, task: DownloadTask) -> None:
        source = self._manager.get(task.source_id) if self._manager else None
        if source is None:
            raise SourceError(f"源不存在：{task.source_id}")
        if task.total <= 0:
            # 空选集/全跳过：无下载内容，直接置 DONE（避免卡在 DOWNLOADING 无终态）
            with self._lock:
                task.status = TaskStatus.DONE
            return

        # ── 漫画批量路径：所有待下载话一次性 Playwright 渲染，避免 N 次启动浏览器 ──
        if task.content_type == "comic":
            self._download_comic_batch(task, source)
            return

        # ── 普通路径（novel / video，逐章串行） ──
        i = 0
        n = len(task.chapters)
        while i < n:
            ch = task.chapters[i]
            if not task.selected[i]:
                i += 1
                continue
            # 取消 / 暂停检查（每章边界）
            if task.cancel_evt.is_set():
                break
            while task.pause_evt.is_set():
                with self._lock:
                    task.status = TaskStatus.PAUSED
                if task.cancel_evt.wait(0.3):
                    break
            if task.cancel_evt.is_set():
                break
            # 已完成章节（resume 整本重跑时跳过，计数补上）
            if i in task.done_chapters:
                with self._lock:
                    task.done += 1
                i += 1
                continue
            with self._lock:
                task.status = TaskStatus.DOWNLOADING

            # 单章下载（失败自动重试 MAX_ATTEMPTS 次）
            last_err = ""
            for attempt in range(MAX_ATTEMPTS):
                # 重试前检查取消/暂停：避免退避 sleep 后仍浪费一整章/批下载
                if task.cancel_evt.is_set():
                    break
                while task.pause_evt.is_set():
                    with self._lock:
                        task.status = TaskStatus.PAUSED
                    if task.cancel_evt.wait(0.3):
                        break
                if task.cancel_evt.is_set():
                    break
                try:
                    # 视频：ffmpeg 合并期间经 image_progress_cb 实时上报输出字节
                    task.image_progress_cb = (
                        lambda nb, _t=task: self._on_video_bytes(_t, nb)
                    )
                    nbytes = self._downloader.download_chapter(source, task, ch, i)
                    task.image_progress_cb = None
                    with self._lock:
                        task.done += 1
                        task.bytes_written += max(0, nbytes)
                        task.done_chapters.append(i)
                    i += 1
                    break
                except Exception as exc:  # noqa: BLE001
                    task.image_progress_cb = None
                    last_err = str(exc)
                    if task.cancel_evt.is_set():
                        break  # 用户取消：停止下载，走取消终态
                    if task.pause_evt.is_set():
                        # 用户暂停：中断当前合并，任务进入暂停；恢复后重下当前章
                        # （不递增 i，外层 while 重新尝试本章 —— 修复暂停中断章永久丢失）
                        with self._lock:
                            task.status = TaskStatus.PAUSED
                            task.bytes_written = 0  # 当前集未完整落盘
                        self._emit_refresh(task)
                        while task.pause_evt.is_set():
                            with self._lock:
                                task.status = TaskStatus.PAUSED
                            if task.cancel_evt.wait(0.3):
                                break
                        break
                    if attempt < MAX_ATTEMPTS - 1:
                        time.sleep(min(0.5 * (2 ** attempt), 2.0))  # 退避
            else:
                with self._lock:
                    task.failed.append(ch.title or f"第{i + 1}章")
                    task.error = last_err
                i += 1

            with self._lock:
                if task.cancel_evt.is_set():
                    break
                task.end_time = time.time()
                done = task.done
                total = task.total
                speed = task.speed_mbs
            self._emit(
                EVENT_DOWNLOAD_PROGRESS,
                {
                    "task_id": task.task_id,
                    "done": done,
                    "total": total,
                    "speed_mbs": speed,
                    "title": task.title,
                },
                task,
            )

        # 终态判定（novel / video 串行循环）
        with self._lock:
            task.end_time = time.time()
            if task.status == TaskStatus.PAUSED:
                pass  # 暂停：保持 PAUSED（不覆盖）
            elif task.cancel_evt.is_set():
                task.status = TaskStatus.CANCELED
            elif task.failed:
                task.status = TaskStatus.FAILED
            else:
                task.status = TaskStatus.DONE

    def _download_comic_batch(self, task: DownloadTask, source) -> None:
        """漫画批量下载：Playwright 源走批量渲染，HTML 源回退串行下载。"""

        # 检测源是否使用 Playwright 渲染（content.page.render 配置）
        content_cfg = source.raw.get("endpoints", {}).get("content", {}).get("page", {})
        body_cfg = content_cfg.get("body") or {}
        render_mode = content_cfg.get("render") or body_cfg.get("render")
        use_playwright = render_mode == "playwright"

        # 非 Playwright 源：回退到普通串行下载路径
        if not use_playwright:
            self._download_comic_serial(task, source)
            return

        # Playwright 源：批量渲染（一次浏览器启动，多话图片）
        import math

        BATCH_SIZE = 12  # 每批渲染话数（控制 Chromium 峰值内存）

        # 收集待下载的章节索引
        pending = [i for i in range(len(task.chapters)) if task.selected[i]]
        if not pending:
            with self._lock:
                task.status = TaskStatus.DONE
            return

        # 批处理
        num_batches = math.ceil(len(pending) / BATCH_SIZE)
        for batch_idx in range(num_batches):
            if task.cancel_evt.is_set():
                break
            # 暂停等待
            while task.pause_evt.is_set():
                with self._lock:
                    task.status = TaskStatus.PAUSED
                if task.cancel_evt.wait(0.3):
                    break
            if task.cancel_evt.is_set():
                break
            with self._lock:
                task.status = TaskStatus.DOWNLOADING

            batch_indices = pending[batch_idx * BATCH_SIZE:(batch_idx + 1) * BATCH_SIZE]
            batch_chapters = [task.chapters[i] for i in batch_indices]

            # 重试逻辑（batch 失败时重试最多 MAX_ATTEMPTS 次）
            last_err = ""
            for attempt in range(MAX_ATTEMPTS):
                try:
                    result = self._downloader.download_comic_batch(
                        source, task, batch_chapters, batch_indices,
                    )
                    # 成功：更新进度（含跳过章节，done 也 +1）
                    with self._lock:
                        for idx, nbytes in result.items():
                            task.done += 1
                            task.bytes_written += max(0, nbytes)
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = str(exc)
                    if task.cancel_evt.is_set():
                        break
                    if attempt < MAX_ATTEMPTS - 1:
                        time.sleep(min(0.5 * (2 ** attempt), 2.0))
            else:
                # 全部重试仍失败
                with self._lock:
                    for idx in batch_indices:
                        ch = task.chapters[idx]
                        task.failed.append(ch.title or f"第{idx + 1}话")
                    task.error = last_err

            # 发进度
            with self._lock:
                task.end_time = time.time()
                done = task.done
                total = task.total
                speed = task.speed_mbs
            self._emit(
                EVENT_DOWNLOAD_PROGRESS,
                {
                    "task_id": task.task_id,
                    "done": done,
                    "total": total,
                    "speed_mbs": speed,
                    "title": task.title,
                },
                task,
            )

        # 终态判定
        with self._lock:
            task.end_time = time.time()
            if task.cancel_evt.is_set():
                task.status = TaskStatus.CANCELED
            elif task.failed:
                task.status = TaskStatus.FAILED
            else:
                task.status = TaskStatus.DONE

    def _download_comic_serial(self, task: DownloadTask, source) -> None:
        """非 Playwright 漫画：逐章串行下载图片，复用 fetch_comic_pages。"""

        i = 0
        n = len(task.chapters)
        while i < n:
            ch = task.chapters[i]
            if not task.selected[i]:
                i += 1
                continue
            if task.cancel_evt.is_set():
                break
            while task.pause_evt.is_set():
                with self._lock:
                    task.status = TaskStatus.PAUSED
                if task.cancel_evt.wait(0.3):
                    break
            if task.cancel_evt.is_set():
                break
            # 已完成话（resume 整本重跑时跳过，计数补上）
            if i in task.done_chapters:
                with self._lock:
                    task.done += 1
                i += 1
                continue
            with self._lock:
                task.status = TaskStatus.DOWNLOADING

            last_err = ""
            for attempt in range(MAX_ATTEMPTS):
                try:
                    # 设置图片进度回调：每张图下载后实时更新 bytes_written
                    task.image_progress_cb = lambda nb: self._on_image_bytes(task, nb)
                    nbytes = self._downloader.download_chapter(source, task, ch, i)
                    task.image_progress_cb = None
                    with self._lock:
                        task.done += 1
                        task.bytes_written += max(0, nbytes)
                        task.done_chapters.append(i)
                    i += 1
                    break
                except Exception as exc:  # noqa: BLE001
                    task.image_progress_cb = None
                    last_err = str(exc)
                    if task.cancel_evt.is_set():
                        break
                    if attempt < MAX_ATTEMPTS - 1:
                        time.sleep(min(0.5 * (2 ** attempt), 2.0))
            else:
                with self._lock:
                    task.failed.append(ch.title or f"第{i + 1}话")
                    task.error = last_err
                i += 1

            with self._lock:
                if task.cancel_evt.is_set():
                    break
                task.end_time = time.time()
            self._emit(
                EVENT_DOWNLOAD_PROGRESS,
                {
                    "task_id": task.task_id,
                    "done": task.done,
                    "total": task.total,
                    "speed_mbs": task.speed_mbs,
                    "title": task.title,
                },
                task,
            )

        # 终态判定（与 _download_comic_batch 一致）
        with self._lock:
            task.end_time = time.time()
            if task.cancel_evt.is_set():
                task.status = TaskStatus.CANCELED
            elif task.failed:
                task.status = TaskStatus.FAILED
            else:
                task.status = TaskStatus.DONE

    def _on_image_bytes(self, task: DownloadTask, nbytes: int) -> None:
        """图片下载进度回调：实时更新 bytes_written + 节流发进度。"""
        with self._lock:
            task.bytes_written += max(0, nbytes)
        # 节流：~1 秒最多发一次进度，避免 UI 刷屏
        now = time.time()
        if not hasattr(self, "_last_img_emit"):
            self._last_img_emit = {}
        if now - self._last_img_emit.get(task.task_id, 0) < 1.0:
            return
        self._last_img_emit[task.task_id] = now
        with self._lock:
            done = task.done
            total = task.total
        self._emit(
            EVENT_DOWNLOAD_PROGRESS,
            {"task_id": task.task_id, "done": done, "total": total,
             "speed_mbs": task.speed_mbs, "title": task.title},
            task,
        )

    def _on_video_bytes(self, task: DownloadTask, nbytes: int) -> None:
        """视频合并进度回调：ffmpeg 输出字节实时上报。

        更新 task.merge_progress（UI 显示"已合并 X MB"），发进度事件驱动滚动。
        bytes_written 由 _download_all 在章节结束时一次性加。
        """
        with self._lock:
            task.merge_progress = max(0, nbytes)
        now = time.time()
        if not hasattr(self, "_last_img_emit"):
            self._last_img_emit = {}
        if now - self._last_img_emit.get(task.task_id, 0) < 1.0:
            return
        self._last_img_emit[task.task_id] = now
        with self._lock:
            done = task.done
            total = task.total
        self._emit(
            EVENT_DOWNLOAD_PROGRESS,
            {"task_id": task.task_id, "done": done, "total": total,
             "speed_mbs": 0, "title": task.title,
             "merge_bytes": nbytes},
            task,
        )

    def _emit_final(self, task: DownloadTask) -> None:
        if task.status == TaskStatus.DONE:
            self._emit(
                EVENT_DOWNLOAD_COMPLETED,
                {
                    "task_id": task.task_id,
                    "title": task.title,
                    "done": task.done,
                    "total": task.total,
                },
                task,
            )
        elif task.status == TaskStatus.FAILED:
            self._emit(
                EVENT_DOWNLOAD_FAILED,
                {"task_id": task.task_id, "title": task.title, "error": task.error},
                task,
            )
        # CANCELED 不广播终态（用户主动取消，无需通知）


# --------------------------------------------------------------------------- #
# Qt worker（可选）：QThreadPool 调度的下载任务
# --------------------------------------------------------------------------- #
if QRunnable is not None:

    class _DownloadWorker(QRunnable):
        """在 Qt 线程池中执行单个下载任务。

        Qt 线程池复用池内线程、AMQ 模型并发，直连 GUI 主线程的事件循环，
        比 raw threading.Thread 对 UI 响应更友好（下载时不卡界面）。
        保留 queue 的暂停/取消/进度语义（经 task 的 Event + queue._run_task）。
        """

        def __init__(self, queue, task):
            super().__init__()
            self.setAutoDelete(True)
            self._queue = queue
            self._task = task
            self._running = threading.Event()

        def is_alive(self) -> bool:
            """是否正在 run()（兼容 threading.Thread.is_alive，供 resume_* 判断）。"""
            return self._running.is_set()

        def run(self) -> None:
            self._running.set()
            try:
                self._queue._run_task(self._task)
            finally:
                self._running.clear()
