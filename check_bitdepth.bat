@echo off
echo Drag and drop a TIFF or PNG file onto this batch file to check its bit depth.
echo.
if "%~1"=="" (
    echo No file specified. Drag a file onto this .bat to check it.
    pause
    exit /b
)
"%~dp0python\python.exe" -c "import tifffile; import sys; img = tifffile.imread(sys.argv[1]); print(f'File: {sys.argv[1]}'); print(f'Shape: {img.shape}'); print(f'Dtype: {img.dtype}'); print(f'Pixel range: {img.min()} - {img.max()}'); bits = 16 if img.dtype == 'uint16' else 8; print(f'Bit depth: {bits}-bit')" "%~1"
echo.
pause
