@echo off
setlocal
cd /d "%~dp0"

rem ── locate Python ────────────────────────────────────────────────
set "PYTHON="
where py >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON (
    where python >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
    echo Python 3.11+ was not found. Please install it from https://www.python.org.
    pause
    exit /b 1
)

rem ── create the venv (first start only) ───────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment ...
    %PYTHON% -m venv .venv
    if errorlevel 1 (
        echo Could not create a virtual environment.
        pause
        exit /b 1
    )
)

rem ── start (run.py installs missing core dependencies itself) ─────
".venv\Scripts\python.exe" run.py %*
if errorlevel 1 pause
