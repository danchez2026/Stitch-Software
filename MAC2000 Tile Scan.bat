@echo off
:: Auto-detect which drive the Microscopes share is on
if exist "Z:\Microscopes\WILD\MAC2000\python\python.exe" (
    set BASEDIR=Z:\Microscopes\WILD\MAC2000
) else if exist "P:\Microscopes\WILD\MAC2000\python\python.exe" (
    set BASEDIR=P:\Microscopes\WILD\MAC2000
) else (
    echo ERROR: Cannot find MAC2000 folder on Z: or P: drive
    pause
    exit /b 1
)
"%BASEDIR%\python\python.exe" "%BASEDIR%\scan_gui.py"
if errorlevel 1 pause
