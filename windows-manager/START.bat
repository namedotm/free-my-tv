@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -STA -ExecutionPolicy RemoteSigned -File "%~dp0bootstrap.ps1"
if errorlevel 1 pause
