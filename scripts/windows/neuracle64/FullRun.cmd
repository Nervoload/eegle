@echo off
powershell.exe -NoLogo -NoProfile -File "%~dp0FullRun.ps1" %*
exit /b %ERRORLEVEL%
