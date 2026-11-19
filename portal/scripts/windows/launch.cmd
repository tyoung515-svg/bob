@echo off
setlocal
powershell.exe -NoProfile -File "%~dp0launch.ps1"
exit /b %errorlevel%
