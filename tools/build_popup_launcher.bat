@echo off
REM ============================================================
REM  构建 popup_launcher.exe 并在 Windows 上注册 xtquant-popup 协议
REM  适用：A 机器（项目根目录），需要已 pip install pyinstaller
REM  产物：dist\popup_launcher.exe
REM ============================================================

setlocal
cd /d "%~dp0\.."

echo.
echo [1/3] 检查 Python 与 pyinstaller ...
where python >nul 2>&1
if errorlevel 1 (
  echo [!] 未找到 python，请先安装 Python 并加入 PATH
  exit /b 1
)

python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
  echo [*] 未安装 pyinstaller，正在安装 ...
  python -m pip install --upgrade pyinstaller
  if errorlevel 1 (
    echo [!] pyinstaller 安装失败
    exit /b 1
  )
)

echo.
echo [2/3] 打包 popup_launcher.exe ...
python tools\build_popup_launcher.py --no-register
if errorlevel 1 (
  echo [!] 打包失败
  exit /b 1
)

echo.
echo [3/3] 注册 xtquant-popup:// 协议到 HKCU ...
python tools\build_popup_launcher.py --register-only --exe-path "%CD%\dist\popup_launcher.exe"
if errorlevel 1 (
  echo [!] 注册失败
  exit /b 1
)

echo.
echo ============================================================
echo  [完成] 产物: dist\popup_launcher.exe
echo         已注册协议: xtquant-popup://
echo  下一步：把 dist\popup_launcher.exe 拷贝到 B 机器，B 机器双击一次即可。
echo ============================================================
endlocal
