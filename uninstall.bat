@echo off
rem Removes the shortcuts and the .venv folder. Your tasks stay in Todoist;
rem the local cache/layout in %APPDATA%\TodoistWidgets and the saved login in
rem Windows Credential Manager are NOT removed (see README to remove them too).
setlocal
cd /d "%~dp0"
echo Closing Todoist Widgets is up to you: right-click the tray icon and choose Quit first.
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0shortcut.ps1" -Folder Programs -Remove
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0shortcut.ps1" -Folder Startup -Remove
if exist ".venv" (
  rmdir /s /q ".venv"
  echo Removed .venv
)
echo Done. Your tasks are untouched (they live in Todoist).
exit /b 0
