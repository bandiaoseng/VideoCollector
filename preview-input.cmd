@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0offline_preview.py"
  exit /b 0
)
echo Local Python environment missing. Please ask the developer to install requirements.
pause
exit /b 1
