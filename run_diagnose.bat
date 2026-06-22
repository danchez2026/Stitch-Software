@echo off
"%~dp0python\python.exe" "%~dp0diagnose_camera.py"
if errorlevel 1 pause
