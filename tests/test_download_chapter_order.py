from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework import epub_builder
from framework.downloader import Downloader
from framework.download_queue import DownloadQueue, DownloadTask, TaskStatus


class _Settings:
    def __init__(self, output_dir="."):
        self.output_dir = output_dir

    def get(self, section, key, default=None):
        if section == "download" and key == "output_dir":
            return self.output_dir
        return default


class _ComicContent:
    def fetch_comic_pages_batch(self, source, urls):
        return {url: [url] for url in urls}


class _BatchDownloader:
    def __init__(self):
        self.calls = []

    def download_comic_batch(self, source, task, chapters, indices):
        self.calls.append((chapters, list(indices)))
        return {index: index + 1 for index in indices}


def _task(content_type="novel", count=4, selected=None, done=0):
    chapters = [
        SimpleNamespace(title=f"Chapter {index}", url=f"https://example.test/{index}")
        for index in range(count)
    ]
    selected = list(selected) if selected is not None else [True] * count
    return DownloadTask(
        task_id="task-1",
        source_id="source-1",
        content_type=content_type,
        url="https://example.test/book",
        title="Book",
        chapters=chapters,
        selected=selected,
        total=sum(selected),
        done=done,
    )


def test_ordered_novel_materials_follow_original_indices():
    task = _task(count=3)
    downloader = Downloader(None, None, _Settings())

    downloader._store_epub_chapter(task, 2, "Chapter 3", "body-3")
    downloader._store_epub_chapter(task, 0, "Chapter 1", "body-1")

    assert downloader._ordered_epub_chapters(task) == [
        ("Chapter 1", "body-1"),
        ("Chapter 3", "body-3"),
    ]


def test_retrying_one_index_replaces_material_without_duplicates():
    task = _task(count=2)
    downloader = Downloader(None, None, _Settings())

    downloader._store_epub_chapter(task, 1, "Old title", "old body")
    downloader._store_epub_chapter(task, 1, "New title", "new body")

    assert task.epub_chapters == [None, ("New title", "new body")]
    assert downloader._ordered_epub_chapters(task) == [("New title", "new body")]


def test_comic_batch_stores_original_indices_and_queue_deduplicates_completion():
    task = _task(content_type="comic", count=4)
    downloader = Downloader(_ComicContent(), None, _Settings())
    downloader._image_bytes = lambda image, referer: referer.encode()

    result = downloader.download_comic_batch(
        None,
        task,
        [task.chapters[3], task.chapters[1]],
        [3, 1],
    )

    assert result == {3: len(task.chapters[3].url), 1: len(task.chapters[1].url)}
    assert task.epub_chapters == [
        None,
        ("Chapter 1", [task.chapters[1].url.encode()]),
        None,
        ("Chapter 3", [task.chapters[3].url.encode()]),
    ]

    queue = DownloadQueue(None, None, _Settings(), source_manager=None)
    batch_downloader = _BatchDownloader()
    queue._downloader = batch_downloader
    queue_task = _task(content_type="comic", count=3, done=1)
    queue_task.done_chapters = [1]
    source = SimpleNamespace(
        raw={"endpoints": {"content": {"page": {"render": "playwright"}}}}
    )

    queue._download_comic_batch(queue_task, source)
    queue._download_comic_batch(queue_task, source)

    assert [indices for _, indices in batch_downloader.calls] == [[0, 2]]
    assert sorted(queue_task.done_chapters) == [0, 1, 2]
    assert len(queue_task.done_chapters) == len(set(queue_task.done_chapters))
    assert queue_task.status == TaskStatus.DONE


def test_finalizer_passes_ordered_materials_to_novel_builder(tmp_path, monkeypatch):
    task = _task(count=3, selected=[True, False, True])
    task.epub_chapters = [
        ("Chapter 1", "body-1"),
        None,
        ("Chapter 3", "body-3"),
    ]
    task.done_chapters = [2, 0]
    downloader = Downloader(None, None, _Settings(tmp_path))
    captured = {}

    def fake_builder(title, output, chapters, merge=False, depths=None):
        captured.update(title=title, output=output, chapters=chapters, merge=merge)

    monkeypatch.setattr(epub_builder, "build_novel_epub", fake_builder)

    output = downloader.finalize_epub(task)

    assert captured["title"] == "Book"
    assert captured["chapters"] == [
        ("Chapter 1", "body-1"),
        ("Chapter 3", "body-3"),
    ]
    assert captured["merge"] is False
    assert output == tmp_path / "Book" / "Book.epub"


def test_ordered_epub_chapters_keeps_legacy_packed_tuples():
    task = _task(count=2)
    task.epub_chapters = [("Chapter 1", "body-1"), ("Chapter 2", "body-2")]
    task.done_chapters = []
    downloader = Downloader(None, None, _Settings())

    assert downloader._ordered_epub_chapters(task) == task.epub_chapters


def test_retry_finalize_keeps_completed_materials(tmp_path, monkeypatch):
    task = _task(count=3, done=2)
    task.epub_chapters = [
        ("Chapter 1", "body-1"),
        ("Chapter 2", "body-2"),
        None,
    ]
    task.done_chapters = [0, 1]
    task.failed = ["Chapter 3"]
    task.failed_idx = [2]
    task.status = TaskStatus.FAILED

    queue = DownloadQueue(None, None, _Settings(), source_manager=None)
    queue._maybe_dispatch = lambda: None
    queue._tasks.append(task)
    queue.retry_task(task.task_id)

    task.epub_chapters[2] = ("Chapter 3", "body-3")
    task.done_chapters.append(2)
    captured = {}

    def fake_builder(title, output, chapters, merge=False, depths=None):
        captured["chapters"] = chapters

    monkeypatch.setattr(epub_builder, "build_novel_epub", fake_builder)
    Downloader(None, None, _Settings(tmp_path)).finalize_epub(task)

    assert captured["chapters"] == [
        ("Chapter 1", "body-1"),
        ("Chapter 2", "body-2"),
        ("Chapter 3", "body-3"),
    ]


def test_comic_batch_resume_rebuilds_done_count():
    queue = DownloadQueue(None, None, _Settings(), source_manager=None)
    batch_downloader = _BatchDownloader()
    queue._downloader = batch_downloader
    task = _task(content_type="comic", count=3, done=1)
    task.done_chapters = [0]
    task.status = TaskStatus.PAUSED
    queue._tasks.append(task)
    queue._maybe_dispatch = lambda: None
    queue.resume_task(task.task_id)
    source = SimpleNamespace(
        raw={"endpoints": {"content": {"page": {"render": "playwright"}}}}
    )

    queue._download_comic_batch(task, source)

    assert [indices for _, indices in batch_downloader.calls] == [[1, 2]]
    assert task.done == task.total == 3
    assert task.done_chapters == [0, 1, 2]
    assert task.status == TaskStatus.DONE


def test_comic_batch_with_no_pending_restores_done_count():
    queue = DownloadQueue(None, None, _Settings(), source_manager=None)
    batch_downloader = _BatchDownloader()
    queue._downloader = batch_downloader
    task = _task(content_type="comic", count=3, selected=[True, True, False])
    task.done_chapters = [0, 1]
    task.status = TaskStatus.WAITING
    source = SimpleNamespace(
        raw={"endpoints": {"content": {"page": {"render": "playwright"}}}}
    )

    queue._download_comic_batch(task, source)

    assert batch_downloader.calls == []
    assert task.done == task.total == 2
    assert task.status == TaskStatus.DONE


def test_finalize_status_preserves_paused_and_priority():
    queue = DownloadQueue(None, None, _Settings(), source_manager=None)
    task = _task(count=1)

    task.status = TaskStatus.PAUSED
    queue._finalize_status(task)
    assert task.status == TaskStatus.PAUSED  # 鏆傚仠淇濇寔锛屼笉琚鐩?

    task.status = TaskStatus.DOWNLOADING
    task.failed = ["x"]
    queue._finalize_status(task)
    assert task.status == TaskStatus.FAILED

    task.cancel_evt.set()  # 鍙栨秷浼樺厛浜庡け璐?
    queue._finalize_status(task)
    assert task.status == TaskStatus.CANCELED

    task.cancel_evt.clear()
    task.failed = []
    task.status = TaskStatus.DOWNLOADING
    queue._finalize_status(task)
    assert task.status == TaskStatus.DONE


class _SpillSettings(_Settings):
    def get(self, section, key, default=None):
        if section == "download" and key == "epub_material_bytes":
            return 10
        return super().get(section, key, default)


def test_large_materials_spill_to_disk_and_resolve(tmp_path, monkeypatch):
    downloader = Downloader(None, None, _SpillSettings(tmp_path))
    task = _task(count=2)

    downloader._store_epub_chapter(task, 0, "C1", "1234567890")  # = 闃堝€硷紝浠嶉┗鍐呭瓨
    assert task.epub_chapters[0] == ("C1", "1234567890")

    downloader._store_epub_chapter(task, 1, "C2", "abcdefghij")  # 瓒呴槇鍊?鈫?鍏ㄩ儴钀界洏
    assert not isinstance(task.epub_chapters[0][1], str)
    assert not isinstance(task.epub_chapters[1][1], str)

    assert downloader._ordered_epub_chapters(task) == [
        ("C1", "1234567890"),
        ("C2", "abcdefghij"),
    ]

    monkeypatch.setattr(epub_builder, "build_novel_epub", lambda *a, **k: None)
    downloader.finalize_epub(task)
    assert not downloader._material_dir(task).exists()  # 鍚堟垚鎴愬姛鍚庢竻鐞嗘潗鏂?


def test_large_comic_materials_spill_and_cleanup_on_discard(tmp_path):
    downloader = Downloader(None, None, _SpillSettings(tmp_path))
    task = _task(content_type="comic", count=1)

    downloader._store_epub_chapter(task, 0, "C1", [b"x" * 6, b"y" * 6])  # 瓒呴槇鍊?
    marker = task.epub_chapters[0][1]
    assert not isinstance(marker, (str, list))
    assert downloader._ordered_epub_chapters(task) == [("C1", [b"x" * 6, b"y" * 6])]

    downloader.discard_materials(task)  # 鍙栨秷/閲嶅惎锛氭竻鐞嗚惤鐩樻潗鏂欏苟澶嶄綅婧㈠嚭鐘舵€?
    assert not downloader._material_dir(task).exists()
    assert getattr(task, "_spill_active", False) is False
