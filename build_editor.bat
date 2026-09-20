@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 打包「简谱编辑器」到独立目录 EditorPlay\AutoPlayEditor\，
rem 跟主程序 dist\AutoPlay、跟奏版 FollowPlay\AutoPlayFollow 互不干扰。
rem
rem 为什么要绕一下：exe 正在跑的时候那个目录是锁着的，PyInstaller 直接往里写
rem 会先清空再失败，旧的 exe 就变成残废了。所以先打到 EditorPlayTmp，
rem 成了再整目录镜像过去。
echo == 打包编辑器（AutoPlayEditor）==
python -m PyInstaller --noconfirm --clean --distpath EditorPlayTmp AutoPlayEditor.spec
if errorlevel 1 (
  echo [x] 打包失败，EditorPlay 里的旧版本没有被改动。
  pause
  exit /b 1
)

echo.
echo == 镜像到 EditorPlay\AutoPlayEditor ==
if not exist "EditorPlay" mkdir "EditorPlay"
robocopy "EditorPlayTmp\AutoPlayEditor" "EditorPlay\AutoPlayEditor" /MIR /NFL /NDL /NJH /NJS /NP >nul
rem robocopy 的返回码 0~7 都算成功（1 = 有文件复制过来了）
if errorlevel 8 (
  echo [x] 复制到 EditorPlay 失败，先关掉正在运行的程序再试。
  pause
  exit /b 1
)

rmdir /s /q "EditorPlayTmp" 2>nul

echo.
echo 打包完成：%~dp0EditorPlay\AutoPlayEditor\AutoPlayEditor.exe
echo 不用管理员权限，也不会往游戏里发按键。
pause