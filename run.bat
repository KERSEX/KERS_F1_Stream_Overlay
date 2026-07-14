@echo off
title Race Telemetry Overlay

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Erstelle virtuelle Umgebung...
    python -m venv venv
)

call venv\Scripts\activate

:: Prüfe, ob die venv aktiv ist
if not defined VIRTUAL_ENV (
    echo [FEHLER] Virtuelle Umgebung konnte nicht aktiviert werden!
    pause
    exit /b 1
)

echo [INFO] Virtuelle Umgebung ist aktiv: %VIRTUAL_ENV%
pip install -r requirements.txt
python main.py

pause