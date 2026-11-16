@echo off
rem hwp-mcp installer: double-click to install for Claude Code
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
pause
