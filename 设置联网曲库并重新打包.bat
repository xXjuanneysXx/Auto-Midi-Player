@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  设置联网曲库地址，然后重新打包两个安装程序
echo ============================================
echo.
echo 把曲库索引文件的地址贴进来，例如：
echo   https://cdn.jsdelivr.net/gh/你的用户名/你的仓库@main/library.json
echo （国内推荐 jsDelivr 这个镜像；官方地址是
echo   https://raw.githubusercontent.com/你的用户名/你的仓库/main/library.json ）
echo.
set /p URL=地址：
if "%URL%"=="" (
  echo 没输入地址，什么都没做。
  pause
  exit /b 1
)
python -u -X utf8 set_library_url.py "%URL%"
echo.
pause