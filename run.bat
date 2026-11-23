@echo off
setlocal
cd /d "%~dp0"
title AP Invoice Agent
echo ==========================================
echo   AP Invoice Agent - setup and run
echo ==========================================
echo.

rem ---- find Python (PATH, py launcher, or Anaconda) ----
set "PY="
python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY if exist "%USERPROFILE%\anaconda3\python.exe" set "PY=%USERPROFILE%\anaconda3\python.exe"
if not defined PY if exist "C:\ProgramData\anaconda3\python.exe" set "PY=C:\ProgramData\anaconda3\python.exe"
if not defined PY if exist "%USERPROFILE%\miniconda3\python.exe" set "PY=%USERPROFILE%\miniconda3\python.exe"
if not defined PY (
  echo [ERROR] Python not found. Open "Anaconda Prompt", cd to this folder and run: run.bat
  pause
  exit /b 1
)
echo Using Python: %PY%

rem ---- virtual environment (first run only) ----
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  "%PY%" -m venv .venv 2>nul || %PY% -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Could not create .venv
  pause
  exit /b 1
)
set "VPY=.venv\Scripts\python.exe"

rem ---- packages ----
echo.
echo Installing packages (first run takes a few minutes)...
"%VPY%" -m pip install --upgrade pip -q
"%VPY%" -m pip install -r requirements.txt -q
if errorlevel 1 (
  echo [ERROR] Package install failed. Scroll up for the reason.
  pause
  exit /b 1
)

rem ---- regression check ----
echo.
echo ==========================================
echo   Regression check (13 test files)
echo ==========================================
"%VPY%" run_cli.py --reset
echo.
echo Note: without an API key or Tesseract, 06_ec4_scanned_clean.pdf FAILS on purpose
echo       (an unreadable scan goes to review instead of being approved).

rem ---- app ----
echo.
echo ==========================================
echo   Starting app: http://localhost:8501
echo   Close this window (or Ctrl+C) to stop.
echo ==========================================
start "" cmd /c "timeout /t 6 /nobreak >nul & start http://localhost:8501"
"%VPY%" -m streamlit run app.py
pause
