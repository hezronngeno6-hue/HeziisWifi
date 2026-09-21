@echo off
REM ===========================================================================
REM  HEZIIS NET - run the setup with administrator rights    *** DOUBLE-CLICK ***
REM ===========================================================================
REM  Just double-click this file.
REM
REM  Windows shows a permission prompt ("Do you want to allow this app to make
REM  changes?") - click YES. That prompt is what grants the rights; without it
REM  Windows blocks every step.
REM
REM  It then runs UNATTENDED - no questions to answer, no way to mistype
REM  anything. A new window opens, does the work, and stays open so you can
REM  read the result.
REM
REM  A full log is written to:  %USERPROFILE%\heziis-run.log
REM
REM  WHY NOT right-click the .ps1 -> "Run with PowerShell"?
REM  Because on Windows 10/11 that does NOT elevate. The script starts, finds
REM  it has no administrator rights, refuses, and the window shuts before you
REM  can read why. It looks like a crash but it is not.
REM
REM  To run a different script in this folder:
REM      RUN-AS-ADMIN.cmd make-usb.ps1
REM ===========================================================================

setlocal
title HEZIIS NET - administrator setup

set "HERE=%~dp0"
set "LOG=%USERPROFILE%\heziis-run.log"

if "%~1"=="" (
    set "TARGET=%HERE%make-hotspot-laptop.ps1"
    set "ARGS=-Force -LogFile ""%LOG%"""
) else (
    set "TARGET=%HERE%%~1"
    set "ARGS=-Force"
)

echo.
echo   HEZIIS NET - requesting administrator rights
echo   ==========================================
echo.

if not exist "%TARGET%" (
    echo   ERROR: cannot find
    echo          %TARGET%
    echo.
    echo   Scripts available in this folder:
    dir /b "%HERE%*.ps1" 2>nul
    echo.
    pause
    exit /b 1
)

echo   Script: %TARGET%
echo   Log:    %LOG%
echo.
echo   A Windows permission prompt will appear.
echo   Click YES. It then runs on its own - nothing to type.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList '-NoExit','-NoProfile','-ExecutionPolicy','Bypass','-File','%TARGET%',%ARGS%"

if errorlevel 1 (
    echo.
    echo   The permission prompt was cancelled, so nothing was changed.
    echo.
    echo   To do it manually instead:
    echo     1. Open the Start menu and type:  Terminal
    echo     2. Right-click it and choose "Run as administrator"
    echo     3. In that window, paste:
    echo.
    echo        powershell -ExecutionPolicy Bypass -File "%TARGET%" %ARGS%
    echo.
    pause
    exit /b 1
)

echo.
echo   Launched in a separate window. You can close this one.
echo   When it finishes, the result is in:  %LOG%
echo.
timeout /t 4 >nul 2>&1
exit /b 0
