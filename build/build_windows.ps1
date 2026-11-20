# Build native standalone Windows executable: dist/TwitchRecorderPro/TwitchRecorderPro.exe
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "=== Building TwitchRecorderPro.exe (Windows) ===" -ForegroundColor Cyan
Get-Process TwitchRecorderPro -ErrorAction SilentlyContinue | Stop-Process -Force

.\.venv\Scripts\pyinstaller.exe --noconfirm --clean TwitchRecorderPro.spec

$exePath = Resolve-Path ".\dist\TwitchRecorderPro\TwitchRecorderPro.exe"
Write-Host "Built: $exePath" -ForegroundColor Green

# Create convenient shortcut right in project root
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut((Join-Path (Get-Location) "TwitchRecorderPro.lnk"))
$sc.TargetPath = $exePath.Path
$sc.WorkingDirectory = Split-Path $exePath.Path -Parent
$sc.IconLocation = (Resolve-Path ".\assets\app_icon.ico").Path
$sc.Description = "Twitch Recorder Pro — Studio Suite"
$sc.Save()
Write-Host "Shortcut created: TwitchRecorderPro.lnk" -ForegroundColor Green
