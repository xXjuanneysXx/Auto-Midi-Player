@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 一键出安装包：画图标 -> 打主程序 -> 打安装程序本体 -> 压 payload -> 合成单文件安装包
rem 产物在 发布\AutoPlay 安装程序 v<版本>.exe
rem 那个 exe 拷到任何一台 Windows 上双击就能装，目标机器不需要装 Python。
echo == 生成安装包（要几分钟，主程序有 400 MB 要打包）==
python make_installer.py all
if errorlevel 1 (
  echo [x] 打包失败，看上面的报错。
  pause
  exit /b 1
)
pause