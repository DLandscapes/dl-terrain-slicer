@echo off
title DL Terrain Slicer - server (close this window to stop)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The Python environment .venv is missing.
  echo Please run:  python -m venv .venv
  echo followed by: .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)
rem launcher.py picks a free port and opens the browser itself. The packaged
rem downloads run the very same file, so there is only one startup path.
".venv\Scripts\python.exe" launcher.py
pause
