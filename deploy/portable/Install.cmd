@echo off
rem OSCE AI Marker - one-click install. No administrator rights needed:
rem Python and every package go into this folder, nothing into Program Files,
rem the registry or the system PATH. Safe to run again (it repairs the install).
setlocal
set "BUNDLE=%~dp0"
set "BUNDLE=%BUNDLE:~0,-1%"
set "UV=%BUNDLE%\tools\uv\uv.exe"
set "UV_PYTHON_INSTALL_DIR=%BUNDLE%\python"
set "UV_PYTHON_PREFERENCE=only-managed"

if not exist "%UV%" (
  echo Missing tools\uv\uv.exe - this folder is not a complete bundle.
  goto :failed
)

if exist "%BUNDLE%\uv-cache\" (
  set "UV_CACHE_DIR=%BUNDLE%\uv-cache"
  set "UV_OFFLINE=1"
  set "UV_PYTHON_DOWNLOADS=never"
  echo Installing from the files in this folder. No internet connection is needed.
) else (
  echo Downloading Python and the packages. This needs internet and several GB.
)

"%UV%" sync --project "%BUNDLE%\app" --frozen --no-dev __SYNC_GROUP_ARGS__
if errorlevel 1 goto :failed

if not exist "%BUNDLE%\app\.env" copy "%BUNDLE%\app\.env.example" "%BUNDLE%\app\.env" >nul

echo.
echo Installed. Next:
echo   1. Notepad is opening app\.env - fill in WHISPERX_HF_TOKEN and NVIDIA_API_KEY, then save.
echo   2. Double-click Start.cmd.
echo   3. Sign in as admin / change-me and change the password on the Account page.
start "" notepad "%BUNDLE%\app\.env"
pause
exit /b 0

:failed
echo.
echo Install failed. Scroll up for the error.
pause
exit /b 1
