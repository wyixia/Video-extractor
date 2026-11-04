@echo off
rem 源码版启动入口：双击即可，窗口会最小化到任务栏。
rem 想在前台看日志，就在命令行里跑 python -m app.server。
rem 打包版用 start.bat 或 VideoExtractor.exe（无窗口）。
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if not errorlevel 1 goto run
where py >nul 2>nul
if not errorlevel 1 goto runpy

echo [ERROR] Python not found in PATH.
echo Install Python 3.9+ from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
pause
exit /b 1

:runpy
start "VideoExtractor" /min py -3 -m app.server
exit /b 0

:run
start "VideoExtractor" /min python -m app.server
exit /b 0
