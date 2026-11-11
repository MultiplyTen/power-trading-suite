@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe goto run
where py >nul 2>nul
if errorlevel 1 goto venv_plain
py -3 -m venv .venv
goto venv_done
:venv_plain
python -m venv .venv
:venv_done
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
:run
.venv\Scripts\python.exe main.py disclosure
pause
exit /b 0
:fail
echo Setup failed. Install Python 3.9+ and check your network, then retry.
pause
exit /b 1
