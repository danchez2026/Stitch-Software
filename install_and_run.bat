@echo off
echo ============================================
echo  MAC2000 Tile Scan - Launch
echo ============================================
echo.
echo Using bundled Python from: %~dp0python\
echo.
"%~dp0python\python.exe" "%~dp0scan_gui.py"
if errorlevel 1 pause
