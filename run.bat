@echo off
rem Starts Todoist Widgets without a console window (pythonw).
rem Uses .venv if install.bat was run, otherwise a system Python that has the packages
rem (py -m pip install -r requirements.txt).
setlocal
cd /d "%~dp0"
set "APP=%~dp0todoist_widgets.py"

rem Use .venv only if it really has the packages: an interrupted install.bat can leave an
rem empty .venv behind, and pythonw would then exit silently.
if exist ".venv\Scripts\pythonw.exe" (
  ".venv\Scripts\python.exe" -c "import requests, PySide6, keyring" >nul 2>&1
  if not errorlevel 1 (
    start "" ".venv\Scripts\pythonw.exe" "%APP%" %*
    exit /b 0
  )
  echo Note: .venv is incomplete ^(packages missing^) - using the system Python instead.
)

where py >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import requests, PySide6, keyring" >nul 2>&1
  if not errorlevel 1 (
    start "" pyw -3 "%APP%" %*
    exit /b 0
  )
)

where pythonw >nul 2>&1
if not errorlevel 1 (
  python -c "import requests, PySide6, keyring" >nul 2>&1
  if not errorlevel 1 (
    start "" pythonw "%APP%" %*
    exit /b 0
  )
)

echo Todoist Widgets: the required Python packages were not found.
echo Run install.bat, or install them into your system Python with:
echo     py -m pip install -r "%~dp0requirements.txt"
pause
exit /b 1
