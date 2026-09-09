@echo off
cd /d "%~dp0"
where docker >nul 2>nul
if errorlevel 1 (
  echo Docker ne nayden. Ustanovite Docker Desktop.
  pause
  exit /b 1
)
if not exist .env copy .env.example .env >nul
docker compose up --build
pause
