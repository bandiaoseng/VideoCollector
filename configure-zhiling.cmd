@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "configure_zhiling.py" --gui
  exit /b
)
python "configure_zhiling.py" --gui
if errorlevel 1 pause
