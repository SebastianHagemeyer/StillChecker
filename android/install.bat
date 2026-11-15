@echo off
REM Build + install FlameCam on the USB-connected Android phone.
cd /d "%~dp0"
python install.py %*
pause
