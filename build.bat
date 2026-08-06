@echo off
rem 一键打包脚本：双击运行，构建 claw.exe 并复制资源
rem 依赖：已安装 PyInstaller（pip install pyinstaller）
cd /d "%~dp0"
python build.py
echo.
pause
