import threading
import time

import framework.external_player as ep


class _FakeMonitor:
    def __init__(self, with_lock=True):
        if with_lock:
            self._lock = threading.Lock()
        self.calls = []

    def _publish(self, force=False):
        self.calls.append(force)


def test_flush_does_not_block_when_lock_held(monkeypatch):
    monitor = _FakeMonitor()
    monkeypatch.setattr(ep, "_progress_monitor", monitor)

    holding = threading.Event()

    def holder():
        with monitor._lock:
            holding.set()
            time.sleep(0.5)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert holding.wait(1.0)
        started = time.monotonic()
        ep.flush_player_progress()
        elapsed = time.monotonic() - started
    finally:
        thread.join()

    assert elapsed < 0.2
    assert monitor.calls == []


def test_flush_publishes_after_lock_released(monkeypatch):
    monitor = _FakeMonitor()
    monkeypatch.setattr(ep, "_progress_monitor", monitor)

    ep.flush_player_progress()

    assert monitor.calls == [True]


def test_flush_tolerates_monitor_without_lock(monkeypatch):
    monitor = _FakeMonitor(with_lock=False)
    monkeypatch.setattr(ep, "_progress_monitor", monitor)

    ep.flush_player_progress()

    assert monitor.calls == [True]


def test_flush_tolerates_missing_monitor(monkeypatch):
    monkeypatch.setattr(ep, "_progress_monitor", None)

    ep.flush_player_progress()
