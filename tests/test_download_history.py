# -*- coding: utf-8 -*-
"""下载记录持久化 + 清除按钮测试（test_download_history.py）。

- round-trip:退出后重启恢复任务；DONE 原样保留、其余状态统一 FAILED。
- 序列化白名单:运行时/大型字段不入 JSON。
- 恢复后不自动派发；恢复出的 FAILED 任务可走既有 retry_task（整本安全重下）。
- GUI:下载页工具栏有「清除已完成」「清除记录」按钮；全清需确认框。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from framework.content import Chapter
from framework.download_history import EXIT_UNFINISHED_MSG, DownloadHistory
from framework.download_queue import DownloadQueue, DownloadTask, TaskStatus


class _SettingsGetter:
    """settings.get(sec, key, default) → 用默认值（并发 1 不真派发、不弹通知）。"""

    def get(self, sec, key, default=None):
        if sec == "download" and key == "show_notification":
            return False
        return default

    def save(self):
        pass


class _Manager:
    def __init__(self):
        self._src = None

    def get(self, sid):
        return self._src


class _Detail:
    source_id = "s1"
    content_type = "novel"
    url = "https://x/book"
    title = "测试书"

    def __init__(self, n=5):
        self.chapters = [Chapter(f"第{i+1}章", f"https://x/c{i+1}") for i in range(n)]


class _FakeBus:
    def subscribe(self, cb):
        pass


def _make_queue(tmp_path, name="downloads.json"):
    p = tmp_path / name
    q = DownloadQueue(
        content=None,
        http=None,
        settings=_SettingsGetter(),
        source_manager=_Manager(),
        history_path=p,
    )
    q._maybe_dispatch = lambda: None  # 测试不真派发 worker
    return q, p


def _done_task(q):
    t = q.add_task(_Detail())
    t.status = TaskStatus.DONE
    t.epub_ok = True
    t.out_dir = str(Path("out") / "测试书")
    t.error = ""
    return t


# --------------------------------------------------------------------- #
# 持久化 / 恢复
# --------------------------------------------------------------------- #
def test_roundtrip_done_kept_unfinished_failed(tmp_path):
    q, p = _make_queue(tmp_path)
    t_done = _done_task(q)
    t_wait = q.add_task(_Detail())
    t_wait.status = TaskStatus.WAITING
    t_canceled = q.add_task(_Detail())
    t_canceled.status = TaskStatus.CANCELED
    q._save()

    q2, _ = _make_queue(tmp_path)  # 模拟重启：同 history_path
    tasks = {t.task_id: t for t in q2.tasks()}
    assert len(tasks) == 3
    assert tasks[t_done.task_id].status == TaskStatus.DONE
    assert tasks[t_done.task_id].epub_ok is True
    assert tasks[t_done.task_id].out_dir == str(Path("out") / "测试书")
    assert tasks[t_wait.task_id].status == TaskStatus.FAILED
    assert tasks[t_wait.task_id].error == EXIT_UNFINISHED_MSG
    assert tasks[t_canceled.task_id].status == TaskStatus.FAILED
    assert tasks[t_canceled.task_id].error == EXIT_UNFINISHED_MSG


def test_roundtrip_preserves_chapters_and_selection(tmp_path):
    q, p = _make_queue(tmp_path)
    t = _done_task(q)
    q._save()
    q2, _ = _make_queue(tmp_path)
    r = next(x for x in q2.tasks() if x.task_id == t.task_id)
    assert [c.title for c in r.chapters] == ["第1章", "第2章", "第3章", "第4章", "第5章"]
    assert r.selected == [True] * 5
    assert r.total == 5


def test_json_excludes_runtime_and_material_fields(tmp_path):
    q, p = _make_queue(tmp_path)
    q._tasks[0] = _done_task(q)  # 先清空再加
    q._tasks.clear()
    _done_task(q)
    q._save()

    data = json.loads(p.read_text(encoding="utf-8"))
    t = data["tasks"][0]
    for key in (
        "worker", "cancel_evt", "pause_evt", "active_titles", "dispatched",
        "epub_chapters", "done_chapters", "done", "bytes_written",
        "merge_progress",
    ):
        assert key not in t, f"不应序列化运行时字段 {key}"
    assert t["chapters"][0]["title"] == "第1章"
    assert data["version"] == 1


def test_restored_tasks_never_auto_dispatch(tmp_path):
    q, p = _make_queue(tmp_path)
    _done_task(q)
    t_wait = q.add_task(_Detail())
    t_wait.status = TaskStatus.WAITING
    q._save()

    q2, _ = _make_queue(tmp_path)
    assert all(t.status in (TaskStatus.DONE, TaskStatus.FAILED) for t in q2.tasks())


def test_retry_restored_unfinished_task_safe(tmp_path):
    q, p = _make_queue(tmp_path)
    _done_task(q)
    t_wait = q.add_task(_Detail())
    t_wait.status = TaskStatus.PAUSED
    q._save()

    q2, _ = _make_queue(tmp_path)
    f = next(t for t in q2.tasks() if t.status == TaskStatus.FAILED)
    q2.retry_task(f.task_id)  # 复用既有错误逻辑
    assert f.status == TaskStatus.WAITING
    assert f.done == 0
    assert f.done_chapters == []
    assert f.failed == []


def test_download_history_direct_save_load(tmp_path):
    p = tmp_path / "h.json"
    h = DownloadHistory(p)
    t = DownloadTask(
        task_id="dl-7",
        source_id="s2",
        content_type="video",
        url="https://x/v",
        title="视频教程",
        chapters=[Chapter("第1集", "https://x/v1")],
        selected=[True],
        quality="1080p",
        total=1,
        status=TaskStatus.DONE,
        out_dir="out/v",
        epub_ok=True,
        parallel=2,
    )
    h.save([t])
    r = DownloadHistory(p).load()
    assert len(r) == 1
    assert r[0].quality == "1080p"
    assert r[0].epub_ok is True
    assert r[0].parallel == 2
    assert r[0].task_id == "dl-7"


def test_missing_file_loads_empty(tmp_path):
    assert DownloadHistory(tmp_path / "nope.json").load() == []


def test_corrupt_file_loads_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert DownloadHistory(p).load() == []


def test_seq_continues_after_restore(tmp_path):
    """恢复任务 dl-N 后，新任务 id 从 N+1 起，不冲突。"""
    q, p = _make_queue(tmp_path)
    t1 = q.add_task(_Detail())
    q._save()

    q2, _ = _make_queue(tmp_path)
    t2 = q2.add_task(_Detail())
    assert t2.task_id != t1.task_id
    n1 = int(t1.task_id[3:])
    n2 = int(t2.task_id[3:])
    assert n2 > n1


# --------------------------------------------------------------------- #
# 清除
# --------------------------------------------------------------------- #
def test_clear_done_removes_only_done(tmp_path):
    q, _ = _make_queue(tmp_path)
    _done_task(q)
    f = q.add_task(_Detail())
    f.status = TaskStatus.FAILED
    q._save()

    q.clear_done()
    assert [t.task_id for t in q.tasks()] == [f.task_id]


def test_clear_all_removes_everything_and_saves(tmp_path):
    q, p = _make_queue(tmp_path)
    _done_task(q)
    f = q.add_task(_Detail())
    f.status = TaskStatus.FAILED
    q._save()

    q.clear_all()
    assert q.tasks() == []
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["tasks"] == []


# --------------------------------------------------------------------- #
# GUI：清除按钮
# --------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_page_has_clear_buttons(app, tmp_path):
    from PySide6.QtWidgets import QPushButton

    from gui.pages.download_page import DownloadPage

    q, _ = _make_queue(tmp_path)
    page = DownloadPage(queue=q, event_bus=_FakeBus(), settings=_SettingsGetter())
    texts = [b.text() for b in page.findChildren(QPushButton)]
    assert "清除已完成" in texts
    assert "清除记录" in texts


def test_clear_all_button_confirms_then_clears(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox, QPushButton

    from gui.pages.download_page import DownloadPage

    q, _ = _make_queue(tmp_path)
    _done_task(q)
    page = DownloadPage(queue=q, event_bus=_FakeBus(), settings=_SettingsGetter())

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes)
    )
    btn = next(b for b in page.findChildren(QPushButton) if b.text() == "清除记录")
    btn.click()
    assert q.tasks() == []


def test_clear_done_button_keeps_failed(app, tmp_path):
    from PySide6.QtWidgets import QPushButton

    from gui.pages.download_page import DownloadPage

    q, _ = _make_queue(tmp_path)
    _done_task(q)
    f = q.add_task(_Detail())
    f.status = TaskStatus.FAILED
    page = DownloadPage(queue=q, event_bus=_FakeBus(), settings=_SettingsGetter())

    btn = next(b for b in page.findChildren(QPushButton) if b.text() == "清除已完成")
    btn.click()
    assert {t.status for t in q.tasks()} == {TaskStatus.FAILED}