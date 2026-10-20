@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "APP_DIR=%~dp0"

if not exist "%APP_DIR%.venv\Scripts\python.exe" (
  py -3 -m venv "%APP_DIR%.venv"
  if errorlevel 1 goto build_failed
)

"%APP_DIR%.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%APP_DIR%requirements-build.txt"
if errorlevel 1 goto build_failed

pushd "%APP_DIR%"
"%APP_DIR%.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --runtime-hook pyside_bootstrap.py --name CodexHistoryViewer --collect-all zstandard main.py
set "BUILD_EXIT=%ERRORLEVEL%"
popd
if not "%BUILD_EXIT%"=="0" goto build_failed
echo 构建完成：%APP_DIR%dist\CodexHistoryViewer\CodexHistoryViewer.exe
pause
exit /b 0

:build_failed
echo EXE 构建失败。请查看上方错误信息。
pause
exit /b 1
