@echo off
title AI Video Moments Analyzer Studio
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

:: Set your API key below or put it into a .env file
:: set GEMINI_API_KEY=YOUR_GEMINI_API_KEY_HERE

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run_studio.py
) else if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" run_studio.py
) else (
    python run_studio.py
)
pause
