"""打包后外部运行时目录定位与环境变量配置测试。"""
from __future__ import annotations

import os

from framework import runtime


def test_find_bundled_tool_recurses_into_runtime(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    tool = root / "ffmpeg" / "bin" / "ffmpeg.exe"
    tool.parent.mkdir(parents=True)
    tool.write_bytes(b"test")
    monkeypatch.setattr(runtime, "runtime_root", lambda: root)

    assert runtime.find_bundled_tool("ffmpeg", "ffmpeg.exe") == str(tool)


def test_configure_runtime_environment_prefers_bundled_tools(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    for name in ("vlc", "ffmpeg", "yt-dlp"):
        (root / name).mkdir(parents=True)
    (root / "vlc" / "plugins").mkdir()
    (root / "playwright" / "chromium-1").mkdir(parents=True)
    monkeypatch.setattr(runtime, "runtime_root", lambda: root)
    monkeypatch.setenv("PATH", "system-path")
    monkeypatch.delenv("VLC_PLUGIN_PATH", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    runtime.configure_runtime_environment()

    path_parts = os.environ["PATH"].split(os.pathsep)
    assert str(root / "vlc") in path_parts
    assert str(root / "ffmpeg") in path_parts
    assert path_parts[-1] == "system-path"
    assert os.environ["VLC_PLUGIN_PATH"] == str(root / "vlc" / "plugins")
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(root / "playwright")
