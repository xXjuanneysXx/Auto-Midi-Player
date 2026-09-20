@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 打包「含跟奏版本」，先打到暂存目录 FollowPlayNew，再覆盖到 FollowPlay。
rem 为什么要绕一下：程序正在运行时 AutoPlayFollow.exe 是被锁住的，
rem 直接打到 FollowPlay 会让 PyInstaller 清空目录后写不进去 —— 旧版本当场变成残废。
rem exe 名字里带 follow，所以跟奏窗口会出现；dist\AutoPlay\AutoPlay.exe 保持老样子。
python -m PyInstaller --noconfirm --clean --distpath FollowPlayNew AutoPlayFollow.spec
if errorlevel 1 (
  echo [x] 打包失败，FollowPlay 里的旧版本没有被动过。
  pause
  exit /b 1
)

echo.
echo 打包完成，接着覆盖到 FollowPlay（会等程序退出）
call "%~dp0应用跟奏版更新.bat"
