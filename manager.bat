@echo off
REM ============================================================
REM  StillChecker Manager launcher
REM  Runs manager.py inside WSL Ubuntu using the project's venv
REM  ( .venv/bin/python ). The Tk windows appear through WSLg.
REM  Double-click this file to start.
REM ============================================================

wsl.exe -d Ubuntu bash -lc "cd /mnt/c/Code/flame/raspberry_ninja && exec .venv/bin/python manager.py"

if errorlevel 1 (
    echo.
    echo Manager exited with an error - see the messages above.
    pause
)
