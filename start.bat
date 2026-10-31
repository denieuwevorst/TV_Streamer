
@echo off
title TV Streamer v2

echo =====================================
echo          TV STREAMER V2
echo =====================================
echo.

cd /d "%~dp0"

echo Controleren van Python...
python --version

if errorlevel 1 (
    echo.
    echo FOUT: Python is niet gevonden.
    echo Installeer Python en voeg het toe aan PATH.
    pause
    exit /b 1
)

echo.
echo Controleren van FFmpeg...
ffmpeg -version >nul 2>&1

if errorlevel 1 (
    echo.
    echo FOUT: FFmpeg is niet gevonden.
    echo Installeer FFmpeg en voeg het toe aan PATH.
    pause
    exit /b 1
)

echo.
echo Starten van TV Streamer...
echo.

python tv_streamer.py

echo.
echo TV Streamer is gestopt.
pause
