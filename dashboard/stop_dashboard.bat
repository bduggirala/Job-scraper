@echo off
REM Stop the dashboard server started by start_dashboard.bat.
REM
REM Matches on the command line rather than on "whatever owns port 8501",
REM because a venv's pythonw.exe is a stub that re-execs the base interpreter:
REM only the child holds the socket, so killing the listener alone leaves the
REM parent stub behind as an orphan. Both carry the same command line.
REM
REM The filter requires streamlit AND dashboard\app.py, so it cannot match a
REM scraper run: `python main.py` and `python -m dashboard.runner` have
REM neither. A run already in flight is deliberately left alone - closing the
REM dashboard must never abandon a 40-minute scrape half-way. It keeps writing
REM logs\scraper.log and output\, and the dashboard reports how it ended next
REM time you open it, because the outcome is recorded on disk.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^^pythonw?\.exe$' -and $_.CommandLine -like '*streamlit*dashboard*app.py*' };" ^
  "if ($p) { $p | ForEach-Object { Write-Host ('Stopping dashboard server - PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue };" ^
  "  Write-Host 'Dashboard stopped. http://localhost:8501 will not load until you start it again.' }" ^
  "else { Write-Host 'No dashboard server is running.' }"
