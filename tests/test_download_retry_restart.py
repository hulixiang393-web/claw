# -*- coding: utf-8 -*-
"""下载失败重试回档 + 重新下载按钮测试（Q3）。

retry_task 语义（回档到出错前状态）：
- failed_idx 非空（单章失败记录正常）→ 只勾选失败章节重下，已完成章节记忆
  （done / bytes_written / epub_chapters / done_chapters / selected）全部保留；
- failed_idx 为空（早期整体失败）→ 不再整本清空记忆，保留已完成章节与
  bytes/epub 材料，进度回落到已成功章节数续下未完成部分。

restart_task 语义（整本从零）：
- 清空 done / bytes_written / epub_chapters / done_chapters / failed，按原勾选
  选集全部重下，用户勾选不受影响。

GUI：失败任务卡提供「重试 / 重新下载 / 忽略」三按钮，「重新下载」点击
绑定 queue.restart_task。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from framework.download_queue import DownloadQueue, DownloadTask, TaskStatus

N = 5


class _SettingsGetter:
    """settings.get(sec, key, default) → 固定并发数 1（测试不真正派发）。"""

    def get(self, *args, **kwargs):
        return 1


def _make_queue(monkeypatch=None):
    q = DownloadQueue(content=None, http=None, settings=_SettingsGetter(), source_manager=None)
    dispatched = []
    q._maybe_dispatch = lambda: dispatched.append(True)
    return q, dispatched


def _task(
    done_chapters=(0, 1, 2),
    failed_idx=(3,),
    done=2,
    bytes_written=54321,
    status=TaskStatus.FAILED,
    selected=None,
    failed=None,
):
    chapters = [object() for _ in range(N)]
    sel = list(selected) if selected is not None else [True] * N
    return DownloadTask(
        task_id="t1",
        source_id="s1",
        content_type="novel",
        url="https://x/book",
        title="测试书",
        chapters=chapters,
        selected=sel,
        total=sum(sel),
        done=done,
        bytes_written=bytes_written,
        epub_chapters=["para-0", "para-1"],
        done_chapters=list(done_chapters),
        failed=list(failed) if failed is not None else ["章节3"],
        failed_idx=list(failed_idx),
        status=status,
    )


# --------------------------------------------------------------------- #
# retry_task：回档到出错前状态
# --------------------------------------------------------------------- #
def test_retry_only_failed_chapters_keeps_memory():
    q, dispatched = _make_queue()
    t = _task(done_chapters=(0, 1, 2), failed_idx=(3,), done=2, bytes_written=54321)
    q._tasks.append(t)

    q.retry_task("t1")

    assert t.selected == [False, False, False, True, False]  # 只重下失败章
    assert t.done == 3  # 已完成且非失败（0,1,2）
    assert t.done_chapters == [0, 1, 2]  # 记忆保留
    assert t.epub_chapters == ["para-0", "para-1"]  # 文本材料保留
    assert t.bytes_written == 54321  # 字节保留
    assert t.status == TaskStatus.WAITING
    assert t.failed == [] and t.failed_idx == []
    assert dispatched == [True]  # 触发重新调度


def test_retry_overall_failure_keeps_memory_not_wipe():
    """整体失败（failed_idx 为空）→ 不再清空记忆（修复前整本重下）。"""
    q, dispatched = _make_queue()
    t = _task(
        done_chapters=(0, 1, 2),
        failed_idx=(),
        done=2,
        bytes_written=54321,
        failed=["（整体）"],
    )
    q._tasks.append(t)

    q.retry_task("t1")

    assert t.done_chapters == [0, 1, 2]  # 已完成章节保留
    assert t.epub_chapters == ["para-0", "para-1"]  # epub 材料保留
    assert t.bytes_written == 54321  # 字节保留
    assert t.selected == [True] * N  # 原勾选保留 → _download_all 按 done_chapters 续下
    assert t.done == 3  # 进度回落到已成功章节数（修复前 = 0 整本重下）
    assert t.status == TaskStatus.WAITING
    assert t.failed == [] and t.failed_idx == []


def test_retry_keep_bytes_and_epub_for_novel_partial():
    """整体失败前已下载大半 → 重试保留已写入文件与累积文本，不推倒重来。"""
    q, _ = _make_queue()
    t = _task(done_chapters=(i for i in range(N - 1)), failed_idx=(), done=N - 1,
              bytes_written=999999, failed=["（整体）"])
    q._tasks.append(t)
    q.retry_task("t1")
    assert t.done == N - 1
    assert t.bytes_written == 999999
    assert len(t.epub_chapters) == 2


def test_retry_noop_for_non_failed_task():
    q, dispatched = _make_queue()
    t = _task(status=TaskStatus.DONE)
    q._tasks.append(t)
    q.retry_task("t1")
    assert t.status == TaskStatus.DONE
    assert dispatched == []  # 无调度


def test_retry_unknown_task_noop():
    q, _ = _make_queue()
    q.retry_task("nope")  # 不崩、无副作用


# --------------------------------------------------------------------- #
# restart_task：整本从零
# --------------------------------------------------------------------- #
def test_restart_wipes_memory_keeps_selection():
    q, dispatched = _make_queue()
    sel = [True, False, True, True, False]
    t = _task(selected=sel, done_chapters=(0, 2), failed_idx=(2,), done=2,
              bytes_written=54321, failed=["章节2"])
    q._tasks.append(t)

    q.restart_task("t1")

    assert t.selected == sel  # 用户勾选不受影响
    assert t.done == 0
    assert t.bytes_written == 0
    assert t.epub_chapters == []
    assert t.done_chapters == []
    assert t.failed == [] and t.failed_idx == []
    assert t.status == TaskStatus.WAITING
    assert dispatched == [True]


def test_restart_noop_for_non_failed_task():
    q, dispatched = _make_queue()
    t = _task(status=TaskStatus.DOWNLOADING)
    q._tasks.append(t)
    q.restart_task("t1")
    assert t.status == TaskStatus.DOWNLOADING
    assert dispatched == []


def test_restart_unknown_task_noop():
    q, _ = _make_queue()
    q.restart_task("nope")


# --------------------------------------------------------------------- #
# GUI：失败任务卡按钮
# --------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _card_button_texts(card):
    return [
        card.btn_row.itemAt(i).widget().text()
        for i in range(card.btn_row.count())
        if card.btn_row.itemAt(i).widget()
    ]


def test_failed_card_has_retry_restart_ignore_buttons(app):
    from gui.pages.download_page import _TaskCard

    calls = []

    class _FakeQueue:
        def retry_task(self, tid):
            calls.append(("retry", tid))

        def restart_task(self, tid):
            calls.append(("restart", tid))

        def remove_done(self, tid):
            calls.append(("ignore", tid))

    class _FakePage:
        _queue = _FakeQueue()

    t = _task()
    card = _TaskCard(t, _FakePage())

    texts = _card_button_texts(card)
    assert texts[:3] == ["重试", "重新下载", "忽略"]  # 顺序：续下 → 从零 → 忽略

    # 点「重新下载」→ 绑定 restart_task
    card.btn_row.itemAt(1).widget().click()
    assert calls == [("restart", "t1")]