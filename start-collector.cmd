@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0desktop_app.py"
  exit /b 0
)
echo Local Python environment missing. The source archive does not include Python.
pause
exit /b 1
