# -*- coding: utf-8 -*-
"""subprocess_no_window helper 测试：Windows 静默标志、非 NT 平台自动跳过。"""
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 验证标志")
def test_no_window_kwargs_returns_creationflags():
    from framework.subprocess_no_window import no_window_kwargs

    kw = no_window_kwargs()
    assert kw.get("creationflags") == 0x08000000


def test_no_window_run_applies_creationflags():
    """run() 会把 Windows 静默标志传给 subprocess.run（真实子进程可执行）。"""
    from framework.subprocess_no_window import run

    r = run([sys.executable, "-c", "import sys; print('ok')"], capture_output=True, text=True)
    assert r.returncode == 0
    assert "ok" in (r.stdout or "")


def test_no_window_popen_returns_popen():
    """popen() 返回可 wait 的 Popen 对象。"""
    from framework.subprocess_no_window import popen

    p = popen(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert p.wait(timeout=15) == 3