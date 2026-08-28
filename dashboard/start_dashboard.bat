@echo off
REM Start the dashboard without leaving a terminal window open.
REM
REM Double-click this file, or make a desktop shortcut to it. It launches the
REM Streamlit server detached from any console and opens the browser, so the
REM window it came from can be closed. The server itself keeps running until
REM you stop it (see stop_dashboard.bat, or end "python.exe" in Task Manager)
REM or the machine restarts - a bookmark to http://localhost:8501 only works
REM while it is running.
REM
REM pythonw.exe rather than python.exe: it is the windowless interpreter, so no
REM console is created at all. Falls back to a minimised python.exe if the
REM venv has no pythonw (a non-standard install).

setlocal
set "ROOT=%~dp0.."
pushd "%ROOT%"

if exist "venv\Scripts\pythonw.exe" (
    start "" "venv\Scripts\pythonw.exe" -m streamlit run dashboard\app.py --server.port 8501 --server.headless true
) else (
    start "Company ATS Dashboard" /min "venv\Scripts\python.exe" -m streamlit run dashboard\app.py --server.port 8501 --server.headless true
)

REM Give the server a moment to bind the port before the browser asks for it.
REM ping rather than timeout: timeout aborts with "Input redirection is not
REM supported" whenever this script is run with its stdin redirected, which is
REM exactly what happens when it is called from another script or a tool.
ping -n 5 127.0.0.1 >nul 2>&1
start "" "http://localhost:8501"

popd
endlocal
