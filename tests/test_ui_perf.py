"""UI 卡顿探针（framework/ui_perf）离线单测。"""
from __future__ import annotations

import logging
import time

from framework import ui_perf


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CLAW_UI_PERF", raising=False)
    ui_perf.set_enabled(None)
    assert ui_perf.enabled() is False
    # 关闭时 op() 是空操作，不产生日志副作用
    with ui_perf.op("noop"):
        pass
    ui_perf.set_enabled(False)


def test_enabled_by_env(monkeypatch):
    ui_perf.set_enabled(None)
    monkeypatch.setenv("CLAW_UI_PERF", "1")
    assert ui_perf.enabled() is True
    assert ui_perf.should_enable() is True
    ui_perf.set_enabled(None)
    monkeypatch.delenv("CLAW_UI_PERF")
    assert ui_perf.enabled() is False
    ui_perf.set_enabled(False)


def test_should_enable_by_settings():
    class _Settings:
        def get(self, section, key, default=None):
            return True if (section, key) == ("diagnostics", "ui_perf_logging") else default

    assert ui_perf.should_enable(settings=_Settings()) is True
    assert ui_perf.should_enable(settings=None) is False


def test_should_enable_by_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAW_UI_PERF", raising=False)
    assert ui_perf.should_enable(base_dir=tmp_path) is False
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "ui_perf.on").write_text("1", encoding="utf-8")
    assert ui_perf.should_enable(base_dir=tmp_path) is True


def test_op_logs_when_slow(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_UI_PERF_DIR", str(tmp_path))
    ui_perf.set_enabled(True)
    monkeypatch.setattr(ui_perf, "OP_THRESHOLD_MS", 10)
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = ui_perf._get_logger()
    handler = _Capture()
    logger.addHandler(handler)
    try:
        with ui_perf.op("slow_op"):
            time.sleep(0.03)
        with ui_perf.op("fast_op"):
            pass
    finally:
        logger.removeHandler(handler)
        ui_perf.set_enabled(False)

    assert any("OP slow_op" in m for m in records), records
    assert not any("fast_op" in m for m in records), records


def test_op_no_log_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_UI_PERF_DIR", str(tmp_path))
    ui_perf.set_enabled(False)
    monkeypatch.setattr(ui_perf, "OP_THRESHOLD_MS", 0)
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = ui_perf._get_logger()
    handler = _Capture()
    logger.addHandler(handler)
    try:
        with ui_perf.op("disabled_op"):
            time.sleep(0.01)
    finally:
        logger.removeHandler(handler)
    assert records == [], records
