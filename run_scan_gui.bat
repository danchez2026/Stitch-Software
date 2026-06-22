@echo off
echo ============================================
echo  MAC2000 Tile Scan GUI
echo ============================================
echo.
"%~dp0python\python.exe" "%~dp0scan_gui.py"
if errorlevel 1 pause
