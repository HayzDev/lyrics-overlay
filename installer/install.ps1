# Per-user install (no admin): app files, Start menu shortcut, Apps & features entry with Uninstall.
$ErrorActionPreference = 'Stop'
$name     = 'LyricsOverlay'
$dest     = Join-Path $env:LOCALAPPDATA "Programs\$name"
$exe      = Join-Path $dest "$name.exe"
$shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'Lyrics Overlay.lnk'
$version  = (Get-Content (Join-Path $PSScriptRoot 'version.txt') -ErrorAction SilentlyContinue) -as [string]
if (-not $version) { $version = '1.0' }
$regKey   = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$name"

Write-Host "Installing Lyrics Overlay to $dest"
Get-Process $name -ErrorAction SilentlyContinue | Stop-Process -Force   # upgrade over a running copy
Start-Sleep -Milliseconds 500
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
Copy-Item (Join-Path $PSScriptRoot 'app') $dest -Recurse
Copy-Item (Join-Path $PSScriptRoot 'uninstall.ps1') $dest

$sh = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
$sh.TargetPath = $exe
$sh.WorkingDirectory = $dest
$sh.IconLocation = "$exe,0"
$sh.Description = 'Spotify lyrics overlay for games'
$sh.Save()

$uninstall = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$dest\uninstall.ps1`""
New-Item $regKey -Force | Out-Null
$props = @{
    DisplayName = 'Lyrics Overlay'; DisplayVersion = $version.Trim(); Publisher = 'HayzDev'
    DisplayIcon = $exe; InstallLocation = $dest
    UninstallString = $uninstall; QuietUninstallString = $uninstall
}
foreach ($p in $props.GetEnumerator()) { Set-ItemProperty $regKey $p.Key $p.Value }
Set-ItemProperty $regKey NoModify 1 -Type DWord
Set-ItemProperty $regKey NoRepair 1 -Type DWord
$size = [int]((Get-ChildItem $dest -Recurse | Measure-Object Length -Sum).Sum / 1KB)
Set-ItemProperty $regKey EstimatedSize $size -Type DWord

Start-Process $exe
Write-Host 'Done. Lyrics Overlay is in the Start menu and running (tray icon). Uninstall from Settings > Apps.'
