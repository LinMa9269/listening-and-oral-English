@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 launcher.py
) else (
  python launcher.py
)
if errorlevel 1 (
  echo.
  echo Could not start Listen Lab. Install Python 3.11 or newer from python.org.
  pause
)
