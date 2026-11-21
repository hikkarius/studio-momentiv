@echo off
title Studio Momentiv
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Studio is not installed yet. Run the install file first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run_studio.py
if errorlevel 1 pause
