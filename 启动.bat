@echo off
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
set PYTHONPATH=%cd%
set "PYTHON_EXE="
set "PYTHON_ARGS="

if defined CLAW_PYTHON if exist "%CLAW_PYTHON%" (
    "%CLAW_PYTHON%" -c "import PySide6" >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=%CLAW_PYTHON%"
)

if not defined PYTHON_EXE if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -c "import PySide6" >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
)

if not defined PYTHON_EXE if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" (
    "%CONDA_PREFIX%\python.exe" -c "import PySide6" >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=%CONDA_PREFIX%\python.exe"
)

if not defined PYTHON_EXE (
    py -3.10 -c "import PySide6" >nul 2>&1
    if not errorlevel 1 (set "PYTHON_EXE=py" & set "PYTHON_ARGS=-3.10")
)
if not defined PYTHON_EXE (
    py -3.12 -c "import PySide6" >nul 2>&1
    if not errorlevel 1 (set "PYTHON_EXE=py" & set "PYTHON_ARGS=-3.12")
)
if not defined PYTHON_EXE (
    py -3.13 -c "import PySide6" >nul 2>&1
    if not errorlevel 1 (set "PYTHON_EXE=py" & set "PYTHON_ARGS=-3.13")
)
if not defined PYTHON_EXE (
    python -c "import PySide6" >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=python"
)

if not defined PYTHON_EXE (
    echo.
    echo [FAIL] No Python environment with PySide6 was found.
    echo Install it into Python 3.10 with:
    echo   py -3.10 -m pip install PySide6
    pause
    exit /b 1
)

if "%CLAW_CHECK%"=="1" (
    "%PYTHON_EXE%" %PYTHON_ARGS% -c "import sys, PySide6; print(sys.executable); print(PySide6.__file__)"
    exit /b %errorlevel%
)

rem Launch without a console window. Set CLAW_CONSOLE=1 to keep it.
if not "%CLAW_CONSOLE%"=="1" (
    if /i "%PYTHON_EXE%"=="py" (
        pyw %PYTHON_ARGS% -c "import PySide6" >nul 2>&1
        if not errorlevel 1 (
            start "" pyw %PYTHON_ARGS% gui/app.py %*
            exit /b 0
        )
    )
    if not "%PYTHON_EXE%"=="py" (
        for %%I in ("%PYTHON_EXE%") do (
            if exist "%%~dpIpythonw.exe" (
                start "" "%%~dpIpythonw.exe" %PYTHON_ARGS% gui/app.py %*
                exit /b 0
            )
        )
    )
    where pythonw.exe >nul 2>&1
    if not errorlevel 1 (
        pythonw.exe -c "import PySide6" >nul 2>&1
        if not errorlevel 1 (
            start "" pythonw.exe %PYTHON_ARGS% gui/app.py %*
            exit /b 0
        )
    )
)

"%PYTHON_EXE%" %PYTHON_ARGS% gui/app.py %*
if errorlevel 1 (
    echo.
    echo [FAIL] The selected Python environment could not start claw.
    echo Set CLAW_PYTHON to a Python executable with PySide6, or install it with:
    echo   "%PYTHON_EXE%" %PYTHON_ARGS% -m pip install PySide6
    echo.
    echo Run with CLAW_CONSOLE=1 to see the full traceback.
    pause
)
