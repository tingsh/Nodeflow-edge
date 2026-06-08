@echo off
REM Nodeflow Edge Windows Mock OTA Upgrade Script
REM Usage: upgrade.bat C:\path\to\firmware.tar.gz 1.2.0

set PAYLOAD_TAR=%1
set VERSION=%2

echo === Windows Mock OTA Upgrade Started (Version: %VERSION%) ===
echo Extracting payload to mock staging directory...
timeout /t 1 >nul

echo Updating package version...
echo """Nodeflow Edge - Single source of truth for the package version.""" > nodeflow_edge\__version__.py
echo. >> nodeflow_edge\__version__.py
echo __version__ = "%VERSION%" >> nodeflow_edge\__version__.py

echo Pre-installing dependencies...
timeout /t 1 >nul

echo Simulating atomic swap...
timeout /t 1 >nul

echo Simulating restart of system daemon...
timeout /t 1 >nul

echo === Mock OTA Upgrade Successful (Version: %VERSION%) ===
