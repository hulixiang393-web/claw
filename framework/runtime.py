"""Locate native/browser runtimes shipped beside the packaged executable."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def runtime_root() -> Path:
    return app_root() / "runtime"


def bundled_path(name: str) -> Path:
    return runtime_root() / name


def find_bundled_tool(directory: str, filename: str) -> str:
    root = bundled_path(directory)
    if not root.exists():
        return ""
    for candidate in root.rglob(filename):
        if candidate.is_file():
            return str(candidate)
    return ""


def configure_runtime_environment() -> None:
    """Prefer bundled tools while preserving normal developer-machine fallback."""
    tool_dirs = []
    for name in ("vlc", "ffmpeg", "yt-dlp"):
        root = bundled_path(name)
        if root.exists():
            tool_dirs.append(str(root))
            tool_dirs.extend(str(path.parent) for path in root.rglob("*.exe"))
    if tool_dirs:
        current = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join(dict.fromkeys(tool_dirs + [current]))

    vlc = bundled_path("vlc")
    plugins = vlc / "plugins"
    if plugins.is_dir():
        os.environ["VLC_PLUGIN_PATH"] = str(plugins)

    browsers = bundled_path("playwright")
    if browsers.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
