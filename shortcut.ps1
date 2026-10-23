# Creates or removes the "Todoist Widgets" shortcut. Used by install.bat / uninstall.bat.
#   powershell -File shortcut.ps1 -Folder Programs|Startup [-Remove]
param(
    [Parameter(Mandatory = $true)][ValidateSet('Programs', 'Startup')][string]$Folder,
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$appDir = $PSScriptRoot
$lnk = Join-Path ([Environment]::GetFolderPath($Folder)) 'Todoist Widgets.lnk'
if ($Remove) {
    if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk; Write-Host "Removed $lnk" }
    exit 0
}
# Target: pythonw.exe of .venv if it has the packages, otherwise of the py-launcher Python
# that has them (pythonw directly, no .bat, so no console window flashes).
$check = 'import requests, PySide6, keyring'
$target = $null
$venvW = Join-Path $appDir '.venv\Scripts\pythonw.exe'
$ErrorActionPreference = 'Continue'
if (Test-Path -LiteralPath $venvW) {
    & (Join-Path $appDir '.venv\Scripts\python.exe') -c $check 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $target = $venvW }
}
if (-not $target -and (Get-Command py -ErrorAction SilentlyContinue)) {
    $exe = & py -3 -c "import sys; $check; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $exe) {
        $w = Join-Path (Split-Path -Parent $exe.Trim()) 'pythonw.exe'
        if (Test-Path -LiteralPath $w) { $target = $w }
    }
}
$ErrorActionPreference = 'Stop'
if (-not $target) { $target = $venvW }
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $target
$s.Arguments = '"' + (Join-Path $appDir 'todoist_widgets.py') + '"'
$s.WorkingDirectory = $appDir
$s.IconLocation = Join-Path $appDir 'icon.ico'
$s.Description = 'Todoist Widgets - Todoist projects on your desktop'
$s.Save()
Write-Host "Created $lnk -> $target"
