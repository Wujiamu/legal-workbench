@echo off
setlocal
chcp 65001 >nul
set "ROOT=%~dp0"
if exist "%ROOT%backend\.venv\Scripts\pythonw.exe" if exist "%ROOT%scripts\lwb-launch" goto :launch_w
if exist "%ROOT%backend\.venv\Scripts\python.exe" if exist "%ROOT%scripts\lwb-launch" goto :launch_c
powershell -NoProfile -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('请确认 法律工作台.bat 位于项目根目录，且已完成安装（backend\.venv 与 scripts\lwb-launch 存在）。', '无法启动法律工作台', 'OK', 'Error') | Out-Null" >nul 2>&1
if not errorlevel 1 exit /b 1
echo Unable to start Legal Workbench: please complete setup per README (backend\.venv and scripts\lwb-launch required).
pause
exit /b 1
:launch_w
start "" /D "%ROOT%" "%ROOT%backend\.venv\Scripts\pythonw.exe" "%ROOT%scripts\lwb-launch"
exit /b 0
:launch_c
start "" /B /D "%ROOT%" "%ROOT%backend\.venv\Scripts\python.exe" "%ROOT%scripts\lwb-launch"
exit /b 0
