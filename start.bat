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
echo Starting DL Terrain Slicer at http://localhost:8765 ...
start "" http://localhost:8765
".venv\Scripts\python.exe" -m uvicorn app.main:app --port 8765
pause
