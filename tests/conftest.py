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


@pytest.fixture(autouse=True)
def _reset_media_proxy_globals():
    """每个用例前后清掉媒体代理的跨请求记忆。

    `media_proxy` 会按主机记「证书有病」「直连不通」「出口偏好」，还缓存一个
    专用降级会话——这些都是**进程级**状态。某个用例模拟一次 SSLError 之后，
    同一进程里的下一个用例就会带着"该主机已降级"的记忆跑，于是本该走假
    session 的请求跑到真实网络上去（表现为一堆本该通过的测试集体失败）。

    生产上这是期望行为（同主机复用判断），测试之间必须清干净。
    """
    import framework.media_proxy as mp

    def _reset():
        for name in ("_reset_media_insecure_hosts", "_reset_upstream_route_diagnostics",
                     "_reset_direct_failures"):
            func = getattr(mp, name, None)
            if callable(func):
                try:
                    func()
                except Exception:  # noqa: BLE001 —— 清理失败不该影响用例
                    pass

    _reset()
    yield
    _reset()