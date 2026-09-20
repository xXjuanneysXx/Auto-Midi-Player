@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 设置上传中转并重新打包
echo 用法示例：
echo   设置上传中转并重新打包.bat https://xxx.workers.dev --key 你的口令
echo   设置上传中转并重新打包.bat https://xxx.workers.dev --key 你的口令 --strip-token
echo （不带参数双击 = 按提示一步步填）
echo.
python -X utf8 set_relay_url.py %*
echo.
pause