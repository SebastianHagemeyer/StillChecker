@echo off
REM ============================================================
REM  LAN Cam Manager launcher (native Windows, no WSL, no terminal)
REM  Runs manager2.py with pythonw so no console window appears.
REM  This is the self-hosted phone-camera path (no VDO.Ninja):
REM  it starts lancam_host.py + the readnew2.py monitor and shows
REM  the link to open in Safari on your phone.
REM  Double-click this file to start.
REM ============================================================

set "PYW=C:\Python313\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"

start "" "%PYW%" "%~dp0manager2.py"
