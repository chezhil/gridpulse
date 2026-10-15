@echo off
cd /d "%~dp0"
set "GRIDPULSE_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
echo GridPulse preview: http://127.0.0.1:8766/login.html
echo Keep this window open while using the preview.
if exist "%GRIDPULSE_PYTHON%" (
  "%GRIDPULSE_PYTHON%" -u -m http.server 8766 --bind 127.0.0.1
) else (
  py -m http.server 8766 --bind 127.0.0.1
)
pause
