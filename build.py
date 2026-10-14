"""Build the portable Windows distribution.

The build downloads the native runtime files used by video and JS-rendered
sources, so the recipient only needs to unpack ``dist/claw`` and run the exe.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from zipfile import ZipFile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "claw"
RUNTIME = DIST / "runtime"
RUNTIME_DEPS = {
    # Official VLC portable archive. Keep this pinned so builds are repeatable.
    "vlc": os.environ.get(
        "CLAW_VLC_URL",
        "https://get.videolan.org/vlc/3.0.21/win64/vlc-3.0.21-win64.zip",
    ),
    # Gyan.dev publishes the Windows essentials build without an installer.
    "ffmpeg": os.environ.get(
        "CLAW_FFMPEG_URL",
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    ),
    "yt-dlp": os.environ.get(
        "CLAW_YTDLP_URL",
        "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
    ),
}
# 这些是构建机上的 Python 依赖；PyInstaller 会把它们打进最终目录。
BUILD_PYTHON_DEPS = {
    "PySide6": "PySide6",
    "PyInstaller": "pyinstaller",
    "playwright": "playwright",
    "vlc": "python-vlc",
    "requests": "requests",
    "lxml": "lxml",
    "cryptography": "cryptography",
    "numpy": "numpy",
    "PIL": "Pillow",
    "fontTools": "fonttools",
    "ebooklib": "EbookLib",
    "zhconv": "zhconv",
}
# 需随 exe 携带的资源（目录或文件）
RESOURCES = ["sources", "data", "PROJECT_GUIDE.md", "app_config.json"]


def ensure_build_python_dependencies() -> None:
    """Install missing Python packages needed to produce a complete bundle."""
    missing = [
        package
        for module, package in BUILD_PYTHON_DEPS.items()
        if importlib.util.find_spec(module) is None
    ]
    if not missing:
        return
    print(">>> 安装构建所需 Python 依赖:", ", ".join(missing))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            *missing,
        ],
        cwd=ROOT,
        check=True,
    )


def _download(url: str, destination: Path) -> None:
    """Download to a temporary file, then atomically move it into place."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size > 0:
        print(f"  [exists] {destination.relative_to(DIST)}")
        return
    print(f"  [download] {url}")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "claw-build/1.0"},
    )
    with tempfile.NamedTemporaryFile(
        prefix="claw-download-", suffix=".tmp", delete=False, dir=destination.parent
    ) as tmp:
        temporary = Path(tmp.name)
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        if temporary.stat().st_size == 0:
            raise RuntimeError(f"下载结果为空：{url}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _download_zip_tool(name: str, required_name: str) -> None:
    """Download an archive and copy its tool directory into runtime/{name}."""
    target = RUNTIME / name
    if target.exists() and any(target.rglob(required_name)):
        print(f"  [exists] runtime/{name}/{required_name}")
        return
    archive = RUNTIME / f".{name}.zip"
    _download(RUNTIME_DEPS[name], archive)
    extract_dir = Path(tempfile.mkdtemp(prefix=f"claw-{name}-", dir=RUNTIME))
    try:
        with ZipFile(archive) as bundle:
            bundle.extractall(extract_dir)
        matches = list(extract_dir.rglob(required_name))
        if not matches:
            raise RuntimeError(f"压缩包中没有找到 {required_name}：{RUNTIME_DEPS[name]}")
        tool_dir = matches[0].parent
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tool_dir, target, dirs_exist_ok=True)
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        archive.unlink(missing_ok=True)


def _download_yt_dlp() -> None:
    _download(RUNTIME_DEPS["yt-dlp"], RUNTIME / "yt-dlp" / "yt-dlp.exe")


def _install_playwright() -> None:
    """Install Chromium into the distribution instead of the user's profile."""
    browser_dir = RUNTIME / "playwright"
    browser_dir.mkdir(parents=True, exist_ok=True)
    if any(browser_dir.glob("chromium-*")):
        print("  [exists] runtime/playwright/chromium")
        return
    try:
        import playwright  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "当前构建 Python 缺少 playwright，请先运行：python -m pip install playwright"
        ) from exc
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_dir)
    print("  [install] Playwright Chromium")
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        cwd=ROOT,
        env=env,
        check=True,
    )


def prepare_runtime_dependencies(skip: bool = False) -> None:
    """Fetch all native/browser dependencies used by the packaged app."""
    if skip:
        print(">>> 跳过运行时依赖下载（仅适合离线调试）")
        return
    if os.name != "nt":
        raise RuntimeError("当前构建脚本只支持 Windows 运行时依赖包")
    print(">>> 准备 VLC / ffmpeg / yt-dlp / Playwright 运行时")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    _download_zip_tool("vlc", "vlc.exe")
    _download_zip_tool("ffmpeg", "ffmpeg.exe")
    _download_yt_dlp()
    _install_playwright()


def run_pyinstaller() -> None:
    """调用 PyInstaller 构建单目录包。"""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",  # 全量重建，避免旧产物残留
        "--windowed",  # GUI 程序（无控制台窗口）
        "--name", "claw",
        "--paths", ".",  # 项目根在导入路径
        "--hidden-import", "vlc",
        "--hidden-import", "framework.epub_builder",
        "--hidden-import", "framework.fanqie_font",
        "--hidden-import", "ebooklib",
        "--hidden-import", "fontTools",
        "--collect-all", "playwright",
        "gui/app.py",
    ]
    print(">>> PyInstaller:", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def copy_resources() -> None:
    """把源配置/数据/项目说明复制到 exe 旁。"""
    print(">>> 复制资源到", DIST)
    DIST.mkdir(parents=True, exist_ok=True)
    for name in RESOURCES:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, DIST / name, dirs_exist_ok=True)
        elif src.is_file():
            shutil.copy2(src, DIST / name)
        else:
            print("  [跳过] 不存在:", name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-runtime-deps",
        action="store_true",
        help="跳过 VLC/ffmpeg/yt-dlp/Chromium 下载，仅用于离线调试",
    )
    args = parser.parse_args()
    if not (ROOT / "gui" / "app.py").exists():
        print("错误：请在项目根目录 D:/code/claw 运行本脚本")
        sys.exit(1)
    ensure_build_python_dependencies()
    run_pyinstaller()
    # PyInstaller 可能重建 dist/claw，必须在它完成后再放入外部运行时。
    prepare_runtime_dependencies(skip=args.skip_runtime_deps)
    copy_resources()
    exe = DIST / "claw.exe"
    if exe.exists():
        size = exe.stat().st_size / 1024 / 1024
        print(f"\n✅ 打包完成: {exe}  ({size:.1f} MB)")
        print("   运行 dist/claw/claw.exe；运行时依赖已放入 dist/claw/runtime")
    else:
        print("\n⚠️ 未找到 claw.exe，请检查上方 PyInstaller 输出")


if __name__ == "__main__":
    main()
