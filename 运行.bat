@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Subscription Aggregator

echo.
echo  ============================================================
echo    Subscription Aggregator  -  multi-source merge + filter
echo  ============================================================
echo.

REM 优先用 Windows 自带的 py 启动器，其次用 PATH 里的 python
set "PYRUN="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PYRUN=py -3"
if not defined PYRUN (
    python --version >nul 2>&1
    if not errorlevel 1 set "PYRUN=python"
)

if not defined PYRUN (
    echo  [!] Python not found.
    echo      Install Python 3.8+ from https://www.python.org/downloads/
    echo      and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

echo  Using: %PYRUN%
echo.

%PYRUN% aggregate.py %*

echo.
if errorlevel 1 (
    echo  [!] Failed. See the error above.
) else (
    echo  [OK] Finished. Output is in the "订阅" folder.
)
echo.
pause
