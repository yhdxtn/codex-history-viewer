@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "APP_DIR=%~dp0"

if exist "%APP_DIR%dist\CodexHistoryViewer\CodexHistoryViewer.exe" (
  if not exist "%APP_DIR%dist\CodexHistoryViewer\_internal\PySide6\plugins\platforms\qwindows.dll" (
    echo 启动文件不完整：请把 Windows ZIP 完整解压后再运行，不要只复制 EXE。
    echo 需要同时保留 dist\CodexHistoryViewer\_internal 文件夹。
    pause
    exit /b 1
  )
  "%APP_DIR%dist\CodexHistoryViewer\CodexHistoryViewer.exe" > "%TEMP%\CodexHistoryViewer-startup.log" 2>&1
  set "APP_EXIT=%ERRORLEVEL%"
  if not "%APP_EXIT%"=="0" (
    echo 工具启动失败，退出代码：%APP_EXIT%
    echo 诊断日志：%TEMP%\CodexHistoryViewer-startup.log
    if exist "%TEMP%\CodexHistoryViewer-startup.log" type "%TEMP%\CodexHistoryViewer-startup.log"
    pause
  )
  exit /b %APP_EXIT%
)

where py >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python 启动器。请安装 Python 3.10 或更高版本，或使用 dist 文件夹中的 EXE。
  pause
  exit /b 1
)

if not exist "%APP_DIR%.venv\Scripts\python.exe" (
  py -3 -m venv "%APP_DIR%.venv"
  if errorlevel 1 goto setup_failed
)

"%APP_DIR%.venv\Scripts\python.exe" -c "import PySide6, zstandard" >nul 2>nul
if errorlevel 1 (
  echo 正在安装运行依赖。首次启动需要网络连接；会话数据不会上传。
  "%APP_DIR%.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%APP_DIR%requirements.txt"
  if errorlevel 1 goto setup_failed
)

start "" "%APP_DIR%.venv\Scripts\pythonw.exe" "%APP_DIR%main.py"
exit /b 0

:setup_failed
echo 环境准备失败。请检查 Python 安装和网络后重试，或查看 README.md。
pause
exit /b 1
