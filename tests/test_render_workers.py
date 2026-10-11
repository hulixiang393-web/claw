# -*- coding: utf-8 -*-
"""渲染 worker 有界并发测试（test_render_workers.py）。

覆盖 D5：worker 数钳制在 1..8、默认为 1（保持原单线程行为）、单 worker 下
路由恒为 0。仅测试纯逻辑，不启动线程/浏览器。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework import playwright_helper as ph


@pytest.fixture(autouse=True)
def _restore_workers():
    saved = ph._RENDER_WORKERS
    saved_queues = list(ph._RENDER_QUEUES)
    saved_threads = list(ph._RENDER_THREADS)
    yield
    ph._RENDER_WORKERS = saved
    ph._RENDER_QUEUES = saved_queues
    ph._RENDER_THREADS = saved_threads


def test_default_render_workers_is_bounded():
    # 默认（未设 set/env）应为 1..8 之间；本项目默认 1 = 原单线程行为
    assert 1 <= ph._RENDER_WORKERS <= 8


def test_set_render_workers_clamps_to_1_8():
    assert ph.set_render_workers(4) == 4
    assert ph._RENDER_WORKERS == 4
    assert ph.set_render_workers(0) == 1
    assert ph.set_render_workers(-5) == 1
    assert ph.set_render_workers(99) == 8
    assert ph.set_render_workers(1000) == 8


def test_set_render_workers_bad_input_keeps_current():
    ph.set_render_workers(3)
    assert ph.set_render_workers("bogus") == 3
    assert ph._RENDER_WORKERS == 3


def test_single_worker_routes_to_zero():
    ph._RENDER_QUEUES = []
    ph._RENDER_THREADS = []
    ph.set_render_workers(1)
    # 默认 1 worker：无论 key 是什么都路由到 0（与旧版单线程一致）
    assert ph._route_worker(None) == 0
    assert ph._route_worker("https://example.test/a") == 0


def test_multi_worker_route_is_stable_for_same_key():
    ph._RENDER_QUEUES = [None, None, None, None]  # 模拟 4 个队列（不启动线程）
    ph._RENDER_THREADS = [None, None, None, None]
    first = ph._route_worker("https://example.test/same")
    assert all(
        ph._route_worker("https://example.test/same") == first for _ in range(5)
    )
    assert 0 <= first < 4


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
