@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 优先用最新的暂存目录：FollowPlayTmp 是临时打包出来的，其次才是 FollowPlayNew
set "SRC="
if exist "%~dp0FollowPlayTmp\AutoPlayFollow\AutoPlayFollow.exe" set "SRC=%~dp0FollowPlayTmp\AutoPlayFollow"
if not defined SRC if exist "%~dp0FollowPlayNew\AutoPlayFollow\AutoPlayFollow.exe" set "SRC=%~dp0FollowPlayNew\AutoPlayFollow"
set "DST=%~dp0FollowPlay\AutoPlayFollow"

if not defined SRC (
  echo [x] 没找到新版本：FollowPlayTmp / FollowPlayNew 里都没有 AutoPlayFollow.exe
  echo     先用 build_follow.bat 打包出 FollowPlayNew，再运行本脚本。
  pause
  exit /b 1
)

echo 跟奏版更新：把新版本覆盖到 FollowPlay
echo.

:wait
tasklist /nh /fi "imagename eq AutoPlayFollow.exe" 2>nul | find /i "AutoPlayFollow.exe" >nul
if not errorlevel 1 (
  echo [ ] 跟奏版程序还在运行，请在托盘图标上右键退出后继续...
  timeout /t 3 /nobreak >nul
  goto wait
)

robocopy "%SRC%" "%DST%" /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
set "RC=%ERRORLEVEL%"
if %RC% GEQ 8 (
  echo [x] 更新失败（robocopy 代码 %RC%），确认程序完全退出后重试。
  pause
  exit /b 1
)

echo [√] 更新完成：%DST%\AutoPlayFollow.exe
rd /s /q "%~dp0FollowPlayTmp" 2>nul
rd /s /q "%~dp0FollowPlayNew" 2>nul
if not exist "%~dp0FollowPlayTmp" if not exist "%~dp0FollowPlayNew" echo [√] 已清理临时目录
pause