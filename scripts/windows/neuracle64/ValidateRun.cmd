@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0ValidateRun.ps1" %*
exit /b %ERRORLEVEL%
