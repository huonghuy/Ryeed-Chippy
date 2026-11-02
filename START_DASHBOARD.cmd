@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 app.py
  goto finished
)
where python >nul 2>nul
if not errorlevel 1 (
  python app.py
  goto finished
)
echo Python 3.10 or newer is needed. Install Python, then run this file again.
:finished
pause
