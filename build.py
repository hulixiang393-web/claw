"""打包脚本：PyInstaller 构建 claw.exe + 复制运行资源。

用法：在项目根目录运行  python build.py
产物：dist/claw/claw.exe（自动携带 sources/data/docs/app_config.json）
需求：已安装 PyInstaller（pip install pyinstaller）
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "claw"
# 需随 exe 携带的资源（目录或文件）
RESOURCES = ["sources", "data", "docs", "app_config.json"]


def run_pyinstaller() -> None:
    """调用 PyInstaller 构建单目录包。"""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",  # 全量重建，避免旧产物残留
        "--windowed",  # GUI 程序（无控制台窗口）
        "--name", "claw",
        "--paths", ".",  # 项目根在导入路径
        "gui/app.py",
    ]
    print(">>> PyInstaller:", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def copy_resources() -> None:
    """把源配置/数据/文档复制到 exe 旁（运行时从 exe 目录读资源）。"""
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
    if not (ROOT / "gui" / "app.py").exists():
        print("错误：请在项目根目录 D:/code/claw 运行本脚本")
        sys.exit(1)
    if not (ROOT / "gui" / "app.py"):
        return
    run_pyinstaller()
    copy_resources()
    exe = DIST / "claw.exe"
    if exe.exists():
        size = exe.stat().st_size / 1024 / 1024
        print(f"\n✅ 打包完成: {exe}  ({size:.1f} MB)")
        print("   运行 dist/claw/claw.exe；VLC / yt-dlp / ffmpeg 需另行安装")
    else:
        print("\n⚠️ 未找到 claw.exe，请检查上方 PyInstaller 输出")


if __name__ == "__main__":
    main()
