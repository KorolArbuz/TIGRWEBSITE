@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"

echo ==========================================
echo HOCO Catalog - local start, NO DOCKER
echo ==========================================
echo.

set "PY_CMD="
where py >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -3"

if not defined PY_CMD (
    where python >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD goto no_python

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python virtual environment .venv ...
    %PY_CMD% -m venv ".venv"
    if errorlevel 1 goto failed
)

echo Installing/updating Python dependencies ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --require-hashes -r "requirements.txt"
if errorlevel 1 goto failed

if not exist ".env" (
    ".venv\Scripts\python.exe" "scripts\setup_security.py" --development
    if errorlevel 1 goto failed
)
set "APP_ENV=development"
set "SESSION_COOKIE_SECURE=false"
set "ALLOWED_HOSTS=localhost,127.0.0.1"

echo.
echo ==========================================
echo Store:  http://localhost:8000
echo Admin:  http://localhost:8000/admin
echo Stop server: Ctrl+C
 echo ==========================================
echo.

".venv\Scripts\python.exe" "app.py"
if errorlevel 1 goto failed

goto end

:no_python
echo.
echo ERROR: Python was not found.
echo Install Python 3.12 or newer from python.org.
echo During setup enable: Add Python to PATH
echo Then run start-local.bat again.
echo.
pause
exit /b 1

:failed
echo.
echo ERROR: Local server setup/start failed.
echo Copy the last error lines from this window and send them to me.
echo.
pause
exit /b 1

:end
endlocal
