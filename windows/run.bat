@echo off
chcp 65001 >nul
title HiddenAudio
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [X] Python peida nashod. Az python.org nasb kon va "Add to PATH" ro bezan.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [*] Dar hale sakhte mohit...
    python -m venv .venv
    if errorlevel 1 ( echo [X] Sakhte venv shekast khord. & pause & exit /b 1 )
)

if not exist ".venv\.ok" (
    echo [*] Dar hale nasb-e vabastegiha...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 ( echo [X] Nasb shekast khord. Internet/VPN ro check kon. & pause & exit /b 1 )
    echo ok> ".venv\.ok"
)

".venv\Scripts\python.exe" main.py
if errorlevel 1 (
    echo.
    echo [X] Barname ba khata baste shod.
    pause
)
