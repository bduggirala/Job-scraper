@echo off
REM Put a "Company ATS Dashboard" shortcut on the Desktop.
REM
REM Run this ONCE. It exists because a browser bookmark cannot start a program:
REM bookmarking http://localhost:8501 only opens a page, and if the server is
REM not already running the browser just reports that it cannot connect. A
REM shortcut to start_dashboard.bat does both - starts the server, then opens
REM the page - so it replaces the bookmark rather than complementing it.
REM
REM Nothing is installed and nothing is registered to run at login; this only
REM creates one .lnk file on your Desktop, which you can delete at any time.

setlocal
set "TARGET=%~dp0start_dashboard.bat"
set "WORKDIR=%~dp0.."

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Company ATS Dashboard.lnk'));" ^
  "$s.TargetPath = '%TARGET%';" ^
  "$s.WorkingDirectory = (Resolve-Path '%WORKDIR%').Path;" ^
  "$s.WindowStyle = 7;" ^
  "$s.Description = 'Start the local job-scraper dashboard and open it in the browser';" ^
  "$s.IconLocation = 'shell32.dll,13';" ^
  "$s.Save();" ^
  "Write-Host ('Created: ' + (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Company ATS Dashboard.lnk'))"

echo.
echo Double-click that shortcut to start the dashboard and open it in your browser.
echo To pin it: right-click the shortcut, then "Pin to Start" or "Pin to taskbar".
endlocal
