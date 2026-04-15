@echo off
echo ============================================================
echo   MediaGrab — Build Desktop App
echo ============================================================
echo.

REM Activate virtual environment if it exists
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

echo [1/2] Installing dependencies...
pip install -r requirements.txt
echo.

echo [2/2] Building MediaGrab.exe with PyInstaller...
pyinstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name "MediaGrab" ^
    --icon NONE ^
    --add-data "downloads;downloads" ^
    --hidden-import "customtkinter" ^
    --hidden-import "PIL" ^
    --hidden-import "yt_dlp" ^
    --hidden-import "instaloader" ^
    --collect-all "customtkinter" ^
    desktop_app.py

echo.
echo ============================================================
if exist "dist\MediaGrab.exe" (
    echo   BUILD SUCCESSFUL!
    echo   Executable: dist\MediaGrab.exe
) else (
    echo   BUILD FAILED — check the output above for errors.
)
echo ============================================================
pause
