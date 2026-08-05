@echo off
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
set PYTHONPATH=%cd%
python gui/app.py %*
if errorlevel 1 (
    echo.
    echo [FAIL] Check Python and PySide6:
    echo   pip install PySide6
    pause
)
