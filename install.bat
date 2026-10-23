@echo off
rem Todoist Widgets installer: creates .venv, installs dependencies, adds shortcuts.
rem Usage: install.bat            (asks whether to start with Windows)
rem        install.bat /quiet     (no questions, no startup shortcut)
rem        install.bat /startup   (no questions, also start with Windows)
setlocal EnableExtensions
cd /d "%~dp0"
set "TDW_DIR=%CD%"
set "MODE=%~1"
echo.
echo === Todoist Widgets installer ===
echo Folder: %TDW_DIR%
echo.

where py >nul 2>&1
if errorlevel 1 goto :nopython
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 goto :nopython

if exist ".venv\Scripts\python.exe" (
  echo Using existing virtual environment .venv
) else (
  echo Creating virtual environment .venv ...
  py -3 -m venv .venv
  if errorlevel 1 goto :fail
)

rem A .venv left behind by an interrupted install can lack pip: repair it first.
".venv\Scripts\python.exe" -m pip --version >nul 2>&1
if errorlevel 1 (
  echo The existing .venv has no pip - repairing it ...
  ".venv\Scripts\python.exe" -m ensurepip --upgrade
  if errorlevel 1 goto :fail
)

echo Installing dependencies, this can take a few minutes ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :fail

".venv\Scripts\python.exe" -c "import PySide6, requests, keyring; print('Imports OK: PySide6', PySide6.__version__, '/ requests', requests.__version__)"
if errorlevel 1 goto :fail

echo Creating Start Menu shortcut ...
set "TDW_LNK=Programs"
call :shortcut
if errorlevel 1 echo WARNING: could not create the Start Menu shortcut.

if /i "%MODE%"=="/quiet" goto :done
if /i "%MODE%"=="/startup" goto :startup
choice /C YN /N /T 30 /D N /M "Start Todoist Widgets automatically with Windows? [Y/N] "
if errorlevel 2 goto :done
:startup
echo Creating startup shortcut ...
set "TDW_LNK=Startup"
call :shortcut
if errorlevel 1 echo WARNING: could not create the startup shortcut.

:done
echo.
echo Installed. Start it from the Start Menu "Todoist Widgets" or with run.bat.
echo On first start a window explains how to connect your Todoist account.
exit /b 0

:shortcut
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%TDW_DIR%\shortcut.ps1" -Folder %TDW_LNK%
exit /b %errorlevel%

:nopython
echo Python 3.11 or newer was not found - the "py" launcher is needed.
echo Install it with:
echo     winget install Python.Python.3.12
echo then run install.bat again.
exit /b 1

:fail
echo.
echo Installation failed. See the messages above.
exit /b 1
