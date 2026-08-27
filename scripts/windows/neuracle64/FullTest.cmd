@echo off
powershell.exe -NoLogo -NoProfile -File "%~dp0FullTest.ps1" %*
exit /b %ERRORLEVEL%
