@echo off
echo ============================================
echo  MAC2000 Stage Calibration
echo ============================================
echo.
"%~dp0python\python.exe" "%~dp0calibrate_stage.py"
if errorlevel 1 pause
