@echo off
setlocal
chcp 65001 >nul 2>&1

echo ============================================
echo   BCBTranslate - Build Installer
echo ============================================
echo.

REM --- Step 1: Check prerequisites ---
echo [1/4] Checking prerequisites...

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python not found on PATH.
    pause
    exit /b 1
)

REM Check if PyInstaller is importable
python -c "import PyInstaller" >nul 2>&1
if %errorlevel% neq 0 (
    echo    PyInstaller not found. Installing...
    python -m pip install pyinstaller
    python -c "import PyInstaller" >nul 2>&1
    if %errorlevel% neq 0 (
        echo ERROR: Failed to install PyInstaller.
        pause
        exit /b 1
    )
)
echo    Python + PyInstaller OK

REM Check for Inno Setup
set "ISCC="
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
)
if exist "C:\Program Files\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
)
if "%ISCC%"=="" (
    echo    WARNING: Inno Setup 6 not found.
    echo             Download it free from: https://jrsoftware.org/isdl.php
    echo             After installing, re-run this script.
    echo.
    echo             Continuing with PyInstaller build only...
) else (
    echo    Inno Setup OK
)

REM Read version from version.py (single source of truth)
for /f %%a in ('python -c "from version import APP_VERSION; print(APP_VERSION)"') do set "VERSION=%%a"
if "%VERSION%"=="" (
    echo ERROR: Could not read APP_VERSION from version.py
    pause
    exit /b 1
)
echo    Version: %VERSION%
echo.

REM --- Step 2: Clean previous build ---
echo [2/4] Cleaning previous build...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"
if exist "installer_output" rmdir /s /q "installer_output"
echo    OK
echo.

REM --- Step 3: Build with PyInstaller ---
echo [3/4] Building application with PyInstaller...
echo          (this may take a minute or two)
echo.

python -m PyInstaller ^
    --name BCBTranslate ^
    --noconfirm ^
    --windowed ^
    --icon "gui\resources\icons\app.ico" ^
    --add-data "gui\resources\styles\dark.qss;gui\resources\styles" ^
    --add-data "gui\resources\styles\light.qss;gui\resources\styles" ^
    --add-data "gui\resources\icons\app.png;gui\resources\icons" ^
    --add-data "gui\resources\icons\app.ico;gui\resources\icons" ^
    --add-data ".env.example;." ^
    --add-data "version.py;." ^
    --hidden-import azure.cognitiveservices.speech ^
    --collect-binaries azure.cognitiveservices.speech ^
    --hidden-import sounddevice ^
    --hidden-import pynput.keyboard._win32 ^
    --hidden-import pynput._util.win32 ^
    --hidden-import aiortc ^
    --collect-submodules aiortc ^
    --hidden-import aioice ^
    --hidden-import pylibsrtp ^
    --collect-binaries pylibsrtp ^
    --hidden-import av ^
    --collect-submodules av ^
    --collect-binaries av ^
    --exclude-module tkinter ^
    --exclude-module matplotlib ^
    --exclude-module scipy ^
    --exclude-module PIL ^
    --exclude-module pytest ^
    main.py

if %errorlevel% neq 0 (
    echo.
    echo ERROR: PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo    PyInstaller build complete: dist\BCBTranslate\
echo.

REM --- Step 4: Build installer with Inno Setup ---
if "%ISCC%"=="" (
    echo [4/4] SKIPPED - Inno Setup not installed.
    echo.
    echo    You can distribute the dist\BCBTranslate\ folder as-is,
    echo    or install Inno Setup 6 and re-run this script for a
    echo    proper installer .exe.
    echo.
    pause
    exit /b 0
)

echo [4/4] Building installer with Inno Setup...
"%ISCC%" /DMyAppVersion=%VERSION% installer.iss
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Inno Setup build failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   BUILD COMPLETE
echo ============================================
echo.
echo   Installer:  installer_output\BCBTranslate_Setup_%VERSION%.exe
echo.
echo   Give this single .exe to anyone. It will:
echo     - Install the application
echo     - Create a Desktop shortcut
echo     - Upgrade over any previous version
echo     - Register an uninstaller
echo.

pause
