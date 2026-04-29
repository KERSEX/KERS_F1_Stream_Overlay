@echo off
title MRL Race Telemetry Overlay
cd /d "%~dp0"
pip install -r requirements.txt --quiet
python main.py
pause
