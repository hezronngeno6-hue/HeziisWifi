@echo off
REM ===========================================================================
REM  HEZIIS NET - run the setup script with administrator rights
REM ===========================================================================
REM  Just DOUBLE-CLICK this file.
REM
REM  Windows will show a permission prompt ("Do you want to allow this app to
REM  make changes?") - click YES. That is the prompt that gives the script the
REM  rights it needs; without it, Windows blocks every step.
REM
REM  A new window opens and STAYS OPEN so you can read everything and answer
REM  the questions it asks.
REM
REM  Why not right-click the .ps1 and choose "Run with PowerShell"?
REM  Because on Windows 10/11 that does NOT elevate. The script starts, finds
REM  it has no administrator rights, refuses to continue, and the window shuts
REM  before you can read why. It looks like a crash but it is not.
REM
REM  You can also run a different script from this folder:
REM      RUN-AS-ADMIN.cmd make-usb.ps1
REM ===========================================================================

setlocal
title HEZIIS NET - administrator setup

set "HERE=%~dp0"

if "%~1"=="" (
    set "TARGET=%HERE%make-hotspot-laptop.ps1"
) else (
    set "TARGET=%HERE%%~1"
)

echo.
echo   HEZIIS NET - requesting administrator rights
echo   ==========================================
echo.

if not exist "%TARGET%" (
    echo   ERROR: cannot find
    echo          %TARGET%
    echo.
    echo   Available scripts in this folder:
    dir /b "%HERE%*.ps1" 2>nul
    echo.
    pause
    exit /b 1
)

echo   Script: %TARGET%
echo.
echo   A Windows permission prompt will appear.
echo   Click YES to continue.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList '-NoExit','-NoProfile','-ExecutionPolicy','Bypass','-File','%TARGET%'"

if errorlevel 1 (
    echo.
    echo   The permission prompt was cancelled, so nothing was changed.
    echo.
    echo   To do it manually instead:
    echo     1. Open the Start menu and type:  Terminal
    echo     2. Right-click "Terminal" ^(or "Windows PowerShell"^) and pick
    echo        "Run as administrator"
    echo     3. In that window, paste:
    echo.
    echo        powershell -ExecutionPolicy Bypass -File "%TARGET%"
    echo.
    pause
    exit /b 1
)

echo   Launched in a separate window. You can close this one.
timeout /t 3 >nul 2>&1
exit /b 0
