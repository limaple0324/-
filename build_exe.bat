@echo off
REM build_exe.bat - Windows CMD 版打包腳本
REM 需先安裝 Python 並加入 PATH

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install it from https://python.org and add to PATH.
    pause
    exit /b 1
)

echo Ensuring PyInstaller...
python -m pip install --upgrade --quiet pyinstaller

if not exist scan.py (
    echo [ERROR] scan.py not found in current directory.
    pause
    exit /b 1
)

if exist scan_icon.ico (
    set ICON=--icon scan_icon.ico
) else (
    set ICON=
)

python -m PyInstaller --onefile --noconsole %ICON% scan.py
echo Build finished. EXE located in dist\scan.exe
pause