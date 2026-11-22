# Removes everything install.ps1 created, plus saved settings.
$name = 'LyricsOverlay'
$dest = Join-Path $env:LOCALAPPDATA "Programs\$name"
Get-Process $name -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
Remove-Item (Join-Path ([Environment]::GetFolderPath('Programs')) 'Lyrics Overlay.lnk') -ErrorAction SilentlyContinue
Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$name" -Recurse -ErrorAction SilentlyContinue
Remove-Item (Join-Path $env:APPDATA $name) -Recurse -Force -ErrorAction SilentlyContinue
# this script lives in $dest, so delete the folder from a detached process once we've exited
Start-Process cmd.exe -WindowStyle Hidden -ArgumentList "/c timeout /t 2 >nul & rmdir /s /q `"$dest`""
Write-Host 'Lyrics Overlay uninstalled.'
