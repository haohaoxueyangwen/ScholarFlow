@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\scholarflow-agent.ps1" %*
exit /b %ERRORLEVEL%
