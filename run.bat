@echo off
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install it first.
    pause
    exit /b 1
)
python scan.py %*