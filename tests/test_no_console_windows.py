# -*- coding: utf-8 -*-
"""回归闸门：运行时不得出现「会弹控制台窗口」的子进程调用。

背景（2026-10 实测）：媒体代理曾对**每个 HLS 分片**起一次 `ffmpeg -c copy`
做时间轴重封装，omofuna 一集 46 片 → Windows 上弹了 46 个黑色控制台窗口；
分片交付被进程启动拖慢，播放器缓冲见底 → 跳帧 + 音频断断续续。

项目早就有 `framework/subprocess_no_window.py`，问题在于新写的调用点容易漏。
本测试静态扫描 `framework/` 与 `gui/`：凡是调用 `subprocess.run/Popen/call/
check_output/check_call` 的地方，必须带 `**no_window_kwargs()`（或显式
`creationflags=`），除非该文件只在 posix 分支调用（Windows 走 os.startfile）。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_CALLS = {"run", "Popen", "call", "check_output", "check_call"}

# 允许的例外：只在 posix 上执行（xdg-open），Windows 走 os.startfile
_POSIX_ONLY = {
    "gui/pages/download_page.py",
    "gui/pages/library_page.py",
}

# 非运行时文件：开发脚本 / 本测试依赖的实现本身
_SKIP_NAMES = {"_smoke_download.py", "subprocess_no_window.py", "build.py"}


def _iter_sources():
    for base in ("framework", "gui"):
        for path in (ROOT / base).rglob("*.py"):
            if path.name in _SKIP_NAMES:
                continue
            yield path.relative_to(ROOT).as_posix(), path


def _call_is_window_safe(node: ast.Call) -> bool:
    """该 subprocess 调用是否抑制了控制台窗口。"""
    for kw in node.keywords:
        if kw.arg == "creationflags":
            return True
        # `**no_window_kwargs()` 的 arg 是 None，值是调用
        if kw.arg is None and isinstance(kw.value, ast.Call):
            fn = kw.value.func
            name = getattr(fn, "id", "") or getattr(fn, "attr", "")
            if "no_window" in name:
                return True
    return False


def _offending_lines(tree: ast.AST) -> list:
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr in _CALLS):
            continue
        owner = fn.value
        if not (isinstance(owner, ast.Name) and owner.id == "subprocess"):
            continue
        if not _call_is_window_safe(node):
            bad.append(node.lineno)
    return bad


def test_runtime_subprocess_calls_hide_console_window():
    offenders = []
    for rel, path in _iter_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for lineno in _offending_lines(tree):
            if rel in _POSIX_ONLY:
                continue
            offenders.append(f"{rel}:{lineno}")
    assert offenders == [], (
        "以下子进程调用没有抑制控制台窗口（Windows 会闪黑框，且拖慢调用点）："
        + ", ".join(offenders)
        + "；请用 framework.subprocess_no_window 的 run()/popen()"
          " 或显式传 **no_window_kwargs()"
    )


def test_scanner_detects_known_bad_pattern():
    """反向用例：能认出「裸 subprocess.run」，不误判 `**no_window_kwargs()`。"""
    sample = (
        "import subprocess\n"
        "from framework.subprocess_no_window import no_window_kwargs\n"
        "subprocess.run(['ffmpeg', '-v', 'error'])\n"
        "subprocess.run(['ok'], **no_window_kwargs())\n"
    )
    assert _offending_lines(ast.parse(sample)) == [3]
