# -*- coding: utf-8 -*-
"""下载源级并发上限测试（test_download_source_concurrency.py）。

背景：强反爬站（如 17k）短时间内并发请求即返回 405 封禁，默认并发（设置
max_parallel_chapters=3）会在下到十几章后被封 → 「下载不完全」。源可用
constraints.max_concurrency 声明下载并发上限；add_task 取「设置并发」与
「源上限」的较小值。

离线：假 source_manager / detail，不触达网络。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from framework.download_queue import DownloadQueue


class _SettingsGetter:
    def __init__(self, parallel=3):
        self._parallel = parallel

    def get(self, sec, key, default=None):
        if (sec, key) == ("download", "max_parallel_chapters"):
            return self._parallel
        if (sec, key) == ("download", "max_concurrent_downloads"):
            return 6
        return default


class _FakeSource:
    def __init__(self, max_concurrency=None):
        c = {}
        if max_concurrency is not None:
            c["max_concurrency"] = max_concurrency
        self.raw = {"constraints": c}


class _FakeManager:
    def __init__(self, source):
        self._source = source

    def get(self, sid):
        return self._source


class _Detail:
    source_id = "qk17"
    content_type = "novel"
    url = "https://www.17k.com/book/1.html"
    title = "测试书"
    chapters = [object() for _ in range(5)]


def _make_queue(source, parallel=3):
    q = DownloadQueue(
        content=None, http=None,
        settings=_SettingsGetter(parallel),
        source_manager=_FakeManager(source),
    )
    q._downloader.book_dir = lambda task: Path(".")
    q._maybe_dispatch = lambda: None
    return q


def test_source_cap_limits_parallel():
    q = _make_queue(_FakeSource(max_concurrency=1), parallel=3)
    t = q.add_task(_Detail())
    assert t.parallel == 1


def test_source_cap_does_not_raise_parallel():
    # 源上限高于设置 → 取设置值（不放大）
    q = _make_queue(_FakeSource(max_concurrency=8), parallel=3)
    t = q.add_task(_Detail())
    assert t.parallel == 3


def test_no_source_cap_uses_setting():
    q = _make_queue(_FakeSource(max_concurrency=None), parallel=3)
    t = q.add_task(_Detail())
    assert t.parallel == 3


def test_missing_manager_uses_setting():
    q = DownloadQueue(content=None, http=None, settings=_SettingsGetter(4),
                      source_manager=None)
    q._downloader.book_dir = lambda task: Path(".")
    q._maybe_dispatch = lambda: None
    t = q.add_task(_Detail())
    assert t.parallel == 4


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
