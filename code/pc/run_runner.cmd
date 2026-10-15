@echo off
rem Supervisor for the live trading runner on the owner's PC (pcmove 2026-10-10).
rem Restarts box_runner.py 15s after it exits. box_runner.py holds runner.lock, so
rem a second copy can never trade at the same time. Stop: create runner.stopped.
setlocal
set "BOT=%~dp0.."
cd /d "%BOT%"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist runner-logs mkdir runner-logs
:loop
if exist "%BOT%\MIRROR_ONLY" exit /b 3
if exist "%BOT%\runner.stopped" exit /b 0
echo %date% %time% SUPERVISOR start>>runner-logs\supervisor.log
"%BOT%\.venv\Scripts\python.exe" "%BOT%\box_runner.py" >>runner-logs\supervisor.log 2>&1
echo %date% %time% SUPERVISOR runner exited rc=%errorlevel%>>runner-logs\supervisor.log
ping -n 16 127.0.0.1 >nul
goto loop
