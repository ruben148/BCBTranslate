@echo off
setlocal
chcp 65001 >nul 2>&1

echo ============================================
echo   BCBTranslate - Build ^& Release
echo ============================================
echo.

:: ── Find gh.exe ──────────────────────────────────────────────────────
set "GH=gh"
where gh >nul 2>&1
if %errorlevel% equ 0 goto :gh_found

if exist "C:\Program Files\GitHub CLI\gh.exe" (
    set "GH=C:\Program Files\GitHub CLI\gh.exe"
    goto :gh_found
)

set "GH_X86=C:\Program Files (x86)\GitHub CLI\gh.exe"
if exist "%GH_X86%" (
    set "GH=%GH_X86%"
    goto :gh_found
)

echo ERROR: GitHub CLI [gh] not found.
echo        Install it from: https://cli.github.com/
pause
exit /b 1

:gh_found
echo    gh: %GH%

:: ── Check auth ───────────────────────────────────────────────────────
"%GH%" auth status >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Not logged in to GitHub CLI.
    echo        Run: gh auth login
    pause
    exit /b 1
)

:: ── Read version ─────────────────────────────────────────────────────
for /f %%a in ('python -c "from version import APP_VERSION; print(APP_VERSION)"') do set "VERSION=%%a"
if "%VERSION%"=="" (
    echo ERROR: Could not read APP_VERSION from version.py
    pause
    exit /b 1
)

set "TAG=v%VERSION%"
set "INSTALLER=installer_output\BCBTranslate_Setup_%VERSION%.exe"

echo    Version:   %VERSION%
echo    Tag:       %TAG%
echo    Installer: %INSTALLER%
echo.

:: ── Check tag doesn't already exist ──────────────────────────────────
"%GH%" release view %TAG% >nul 2>&1
if %errorlevel% equ 0 (
    echo ERROR: Release %TAG% already exists on GitHub.
    echo        Bump APP_VERSION in version.py before releasing.
    pause
    exit /b 1
)

:: ── Build ────────────────────────────────────────────────────────────
echo Building...
echo.
call build_installer.bat
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Build failed. Release aborted.
    pause
    exit /b 1
)

if not exist "%INSTALLER%" (
    echo ERROR: Expected installer not found: %INSTALLER%
    pause
    exit /b 1
)

:: ── Create GitHub Release ────────────────────────────────────────────
echo.
echo ============================================
echo   Creating GitHub Release %TAG%
echo ============================================
echo.

:: Prompt for release notes
set "NOTES_FILE=%TEMP%\bcb_release_notes.txt"
echo Enter release notes below. Save and close the editor when done.
echo.

echo Release %TAG% > "%NOTES_FILE%"
echo. >> "%NOTES_FILE%"
echo - >> "%NOTES_FILE%"
notepad "%NOTES_FILE%"

"%GH%" release create %TAG% "%INSTALLER%" --title "%TAG%" --notes-file "%NOTES_FILE%"
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to create GitHub release.
    del "%NOTES_FILE%" 2>nul
    pause
    exit /b 1
)

del "%NOTES_FILE%" 2>nul

echo.
echo ============================================
echo   RELEASE COMPLETE
echo ============================================
echo.
echo   Tag:       %TAG%
echo   Installer: %INSTALLER%
echo.
echo   Users running BCBTranslate will be prompted
echo   to update on next launch.
echo.

pause
