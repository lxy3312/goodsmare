@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
rem 没装 Python 时，python 会指向微软商店的空壳，--version 也会失败
%PY% --version >nul 2>nul || goto nopython
%PY% -m goodsmare %*
pause
exit /b

:nopython
echo 没找到 Python 3。
echo 马上帮你打开下载页：装 3.9 或更新的版本，安装时勾选 "Add python.exe to PATH"，装好后再双击 start.bat。
start "" "https://www.python.org/downloads/"
pause
