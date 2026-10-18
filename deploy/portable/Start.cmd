@echo off
rem OSCE AI Marker - start the app. Close this window to stop it.
setlocal
set "BUNDLE=%~dp0"
set "BUNDLE=%BUNDLE:~0,-1%"
set "PY=%BUNDLE%\app\.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo Not installed yet. Double-click Install.cmd first.
  pause
  exit /b 1
)

rem The bundled ffmpeg, wherever this folder now lives. Environment variables
rem win over app\.env, so moving the folder never leaves a stale path behind.
set "PATH=%BUNDLE%\tools\ffmpeg\bin;%PATH%"
set "FFMPEG_BIN=%BUNDLE%\tools\ffmpeg\bin\ffmpeg.exe"
set "FFPROBE_BIN=%BUNDLE%\tools\ffmpeg\bin\ffprobe.exe"
rem One process serves both the web page and the API, on this PC only.
set "SERVE_FRONTEND=true"
rem No NVIDIA driver on this PC: transcribe on the CPU (slower, but it works).
where nvidia-smi >nul 2>nul || set "WHISPERX_DEVICE=cpu"

cd /d "%BUNDLE%\app"
echo Starting OSCE AI Marker. The browser opens at http://127.0.0.1:8787 in a few seconds.
start "" /b cmd /c "timeout /t 12 /nobreak >nul & start http://127.0.0.1:8787/"
"%PY%" scripts\run_api.py
pause
