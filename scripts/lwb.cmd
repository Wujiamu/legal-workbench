@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "ROOT=%~dp0.."

if exist "%ROOT%\backend\.venv\Scripts\python.exe" (
    "%ROOT%\backend\.venv\Scripts\python.exe" "%ROOT%\scripts\lwb" %*
    exit /b %ERRORLEVEL%
)

where uv >nul 2>&1
if %ERRORLEVEL%==0 (
    uv run --project "%ROOT%\backend" python "%ROOT%\scripts\lwb" %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 "%ROOT%\scripts\lwb" %*
    exit /b %ERRORLEVEL%
)

python "%ROOT%\scripts\lwb" %*
exit /b %ERRORLEVEL%
