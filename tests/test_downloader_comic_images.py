# -*- coding: utf-8 -*-
"""漫画图片并发下载测试（test_downloader_comic_images.py）。

覆盖：批量路径单话内图片并发上限 8、完成序不影响页序、单图失败有界重试、
永久失败仅缺图不整批失败、批量与非批量结果一致、渲染失败仍整批失败。
全部 mock，不联网。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from framework.downloader import Downloader

URLS = [f"https://img.example/{i}.jpg" for i in range(20)]


class _Settings:
    def get(self, section, key, default=None):
        return default


class _Content:
    def __init__(self, pages, fail_urls=()):
        self.pages = pages
        self.fail_urls = set(fail_urls)

    def fetch_comic_pages(self, source, url):
        if url in self.fail_urls:
            return None
        return list(self.pages.get(url, []))

    def fetch_comic_pages_batch(self, source, urls):
        return {
            url: None if url in self.fail_urls else list(self.pages.get(url, []))
            for url in urls
        }


def _task(count=1):
    chapters = [
        SimpleNamespace(title=f"Ch{i}", url=f"https://site.example/ch/{i}")
        for i in range(count)
    ]
    return SimpleNamespace(
        task_id="task-1",
        title="Book",
        source_id="source-1",
        content_type="comic",
        chapters=chapters,
        selected=[True] * count,
        done_chapters=[],
        epub_chapters=[],
    )


class _FakeImageBytes:
    def __init__(self, fail_times=None, fail_always=(), delay=0.02):
        self.fail_times = dict(fail_times or {})
        self.fail_always = set(fail_always)
        self.delay = delay
        self.calls = {}
        self._active = 0
        self._peak = 0
        self._lock = threading.Lock()

    def __call__(self, img, referer):
        with self._lock:
            self._active += 1
            self._peak = max(self._peak, self._active)
            self.calls[img] = self.calls.get(img, 0) + 1
            attempt = self.calls[img]
        try:
            time.sleep(self.delay)
            if img in self.fail_always or attempt <= self.fail_times.get(img, 0):
                raise RuntimeError(f"boom {img}")
            return img.encode()
        finally:
            with self._lock:
                self._active -= 1

    @property
    def peak(self):
        return self._peak


def _downloader(pages, fail_urls=()):
    return Downloader(_Content(pages, fail_urls), None, _Settings())


def test_batch_images_parallel_bounded_and_order_kept(monkeypatch):
    d = _downloader({"https://site.example/ch/0": URLS})
    fake = _FakeImageBytes()
    monkeypatch.setattr(d, "_image_bytes", fake)
    task = _task()

    result = d.download_comic_batch(None, task, task.chapters, [0])

    assert fake.peak <= 8
    assert fake.peak >= 2
    assert result == {0: sum(len(u.encode()) for u in URLS)}
    assert task.epub_chapters == [
        ("Ch0", [u.encode() for u in URLS]),
    ]


def test_batch_single_image_retries_then_succeeds(monkeypatch):
    urls = URLS[:3]
    d = _downloader({"https://site.example/ch/0": urls})
    fake = _FakeImageBytes(fail_times={urls[1]: 2})
    monkeypatch.setattr(d, "_image_bytes", fake)
    task = _task()

    result = d.download_comic_batch(None, task, task.chapters, [0])

    assert fake.calls[urls[1]] == 3
    assert result == {0: sum(len(u.encode()) for u in urls)}
    assert task.epub_chapters[0][1] == [u.encode() for u in urls]


def test_batch_permanent_image_failure_skips_without_failing_batch(monkeypatch):
    urls = URLS[:3]
    d = _downloader({"https://site.example/ch/0": urls})
    fake = _FakeImageBytes(fail_always={urls[1]})
    monkeypatch.setattr(d, "_image_bytes", fake)
    task = _task()

    result = d.download_comic_batch(None, task, task.chapters, [0])

    assert fake.calls[urls[1]] == 3
    assert task.epub_chapters[0][1] == [urls[0].encode(), urls[2].encode()]
    assert result == {0: len(urls[0].encode()) + len(urls[2].encode())}


def test_batch_and_non_batch_results_match(monkeypatch):
    urls = URLS[:6]
    pages = {"https://site.example/ch/0": urls}
    chapter = SimpleNamespace(title="Ch0", url="https://site.example/ch/0")

    batch_d = _downloader(pages)
    monkeypatch.setattr(
        batch_d, "_image_bytes",
        _FakeImageBytes(fail_times={urls[1]: 1}, fail_always={urls[4]}),
    )
    batch_task = _task()
    batch_d.download_comic_batch(None, batch_task, [chapter], [0])

    single_d = _downloader(pages)
    monkeypatch.setattr(
        single_d, "_image_bytes",
        _FakeImageBytes(fail_times={urls[1]: 1}, fail_always={urls[4]}),
    )
    single_task = _task()
    size = single_d._download_comic(None, single_task, chapter, 0)

    batch_bytes = batch_task.epub_chapters[0][1]
    single_bytes = single_task.epub_chapters[0][1]
    assert batch_bytes == single_bytes
    assert size == sum(len(b) for b in single_bytes)
    assert batch_bytes == [urls[0].encode(), urls[1].encode(), urls[2].encode(), urls[3].encode(), urls[5].encode()]


def test_batch_render_failure_still_fails_whole_batch(monkeypatch):
    chapter_url = "https://site.example/ch/0"
    d = _downloader({}, fail_urls={chapter_url})
    monkeypatch.setattr(d, "_image_bytes", _FakeImageBytes())
    task = _task()

    with pytest.raises(RuntimeError):
        d.download_comic_batch(None, task, task.chapters, [0])

    assert task.epub_chapters == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
