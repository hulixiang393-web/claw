# -*- coding: utf-8 -*-
"""tests 共享 fixture（conftest.py）。

多个测试文件各自定义 session 级 _qapp（autouse）会重复创建 QApplication，
libshiboken 报错。统一在此提供：跨文件共享单个离屏 QApplication。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

_QAPP = None


@pytest.fixture(scope="session")
def _qapp():
    """会话级共享 QApplication（离屏）。已有实例则复用，避免重复创建冲突。"""
    global _QAPP
    from PySide6.QtWidgets import QApplication

    if _QAPP is None:
        _QAPP = QApplication.instance() or QApplication([])
    return _QAPP