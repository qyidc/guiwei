@echo off
setlocal
title 归位 · 启动
cd /d "%~dp0"

rem 中文输出在 GBK 控制台会崩(UnicodeEncodeError),统一 UTF-8
set "PYTHONUTF8=1"

set "ROOT=%~1"
if "%ROOT%"=="" set "ROOT=%USERPROFILE%\Inbox"
set "PORT=%~2"
if "%PORT%"=="" set "PORT=8787"

rem 浏览器:默认打开;脚本/CI 里 set GUIWEI_NO_OPEN=1 可关掉
set "OPENARG=--open"
if defined GUIWEI_NO_OPEN set "OPENARG="

rem ---------- 找 python ----------
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo [x] 没找到 python,先装 Python 3.10+ 并把它加进 PATH。
  pause
  exit /b 1
)

rem ---------- 依赖:第一次跑自动建 .venv ----------
if not exist ".venv\Scripts\python.exe" (
  echo [*] 第一次运行,创建虚拟环境 .venv ...
  %PY% -m venv .venv || goto fail
  echo [*] 装依赖:pymupdf pillow openpyxl ...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install pymupdf pillow openpyxl || goto fail
)
set "PYX=.venv\Scripts\python.exe"

rem ---------- 收件夹:没有就建,没初始化就 init ----------
if not exist "%ROOT%\" mkdir "%ROOT%"
if not exist "%ROOT%\索引\规则.json" (
  echo [*] 初始化收件夹 %ROOT% ...
  "%PYX%" organize.py init --root "%ROOT%" || goto fail
)

echo.
echo 归位 · 控制台   http://127.0.0.1:%PORT%/
echo 收件夹          %ROOT%
echo 关闭窗口或 Ctrl+C 停止。
echo.
"%PYX%" ui.py --root "%ROOT%" --port %PORT% %OPENARG%
echo.
echo 控制台已退出(退出码 %errorlevel%)。
pause
exit /b 0

:fail
echo.
echo [x] 出错了,看上面的报错。
pause
exit /b 1
