@echo off
cd /d "%~dp0"
title Zayavka 24/7 - Sales AI Bot and Dashboard

echo ===================================================
echo     ZAYAVKA 24/7 - SALES AI BOT and DASHBOARD
echo ===================================================
echo.

REM 1. Check .env
if not exist ".env" (
    echo [ERROR] .env file not found! Copying from .env.example...
    copy .env.example .env
    echo Please fill your API keys in .env and run start.bat again.
    pause
    exit /b 1
)

REM 2. Check Python virtual environment
if not exist ".venv\Scripts\python.exe" (
    echo [*] Creating virtual environment .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Python not found. Please install Python 3.10+
        pause
        exit /b 1
    )
    echo [*] Installing requirements...
    .\.venv\Scripts\pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
)

echo [*] Dashboard URL: http://127.0.0.1:8080/
echo [*] Starting services...
echo.

REM 3. Open browser after short delay
start "" powershell -NoProfile -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8080/'"

REM 4. Run main application
.\.venv\Scripts\python.exe main.py

if errorlevel 1 (
    echo.
    echo [ERROR] Application terminated with error.
)

pause
