@echo off
setlocal
cd /d "%~dp0"
call "%~dp0ScholarFlow-Agent.bat" web
if errorlevel 1 pause
