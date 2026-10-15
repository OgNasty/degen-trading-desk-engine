@echo off
rem Run one scheduled job (morning_balance_run.py / friday_rsi.py) with the bot venv.
setlocal
set "BOT=%~dp0.."
cd /d "%BOT%"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist runner-logs mkdir runner-logs
if exist "%BOT%\MIRROR_ONLY" exit /b 3
echo ===== %date% %time% start %1>>"runner-logs\%~n1.log"
"%BOT%\.venv\Scripts\python.exe" "%BOT%\%~1" >>"runner-logs\%~n1.log" 2>&1
echo ===== %date% %time% end %1 rc=%errorlevel%>>"runner-logs\%~n1.log"
