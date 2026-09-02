@echo off
REM ============================================================
REM  Build popup_launcher.exe and register xtquant-popup protocol
REM  Run this from anywhere - it cd's to project root automatically.
REM  Pure ASCII to avoid GBK/UTF-8 codepage issues in cmd.
REM ============================================================

setlocal

REM Resolve project root (parent of this script's directory).
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.."
set "ROOT=%CD%"
popd

echo.
echo [1/3] Checking Python and pyinstaller ...
where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] python not found in PATH. Install Python 3.x and retry.
  exit /b 1
)

python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
  echo Installing pyinstaller ...
  python -m pip install --upgrade pyinstaller
  if errorlevel 1 (
    echo [ERROR] pip install pyinstaller failed.
    exit /b 1
  )
)

echo.
echo [2/3] Building popup_launcher.exe ...
pushd "%ROOT%"
python tools\build_popup_launcher.py --no-register
if errorlevel 1 (
  popd
  echo [ERROR] build failed.
  exit /b 1
)

if not exist "dist\popup_launcher.exe" (
  popd
  echo [ERROR] dist\popup_launcher.exe not produced.
  exit /b 1
)

echo.
echo [3/3] Registering xtquant-popup:// protocol in HKCU ...
python tools\build_popup_launcher.py --register-only --exe-path "%ROOT%\dist\popup_launcher.exe"
if errorlevel 1 (
  popd
  echo [ERROR] register failed.
  exit /b 1
)
popd

echo.
echo ============================================================
echo  Done.
echo  Output: dist\popup_launcher.exe
echo  Registered: xtquant-popup:// in HKCU
echo  Next: copy dist\popup_launcher.exe to LAN PC B and run once
echo         to register the protocol there.
echo ============================================================
endlocal
