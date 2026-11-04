@echo off
rem 源码版启动入口：双击即可。打包版用 start.bat 或 VideoExtractor.exe。
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
py -3 -m app.server
goto done

:run
python -m app.server

:done
if errorlevel 1 pause
