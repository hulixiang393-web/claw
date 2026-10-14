@echo off
rem 一键打包脚本：双击运行，构建 claw.exe 并复制资源
rem build.py 会自动安装缺失的 Python 构建依赖，并下载 VLC/ffmpeg/yt-dlp/Chromium
cd /d "%~dp0"
python build.py
echo.
pause
