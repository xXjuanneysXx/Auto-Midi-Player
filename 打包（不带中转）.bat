@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 打包（不带上传中转）
echo 打一版「上传走内置令牌、不用中转」的安装包，结果在「发布」文件夹里。
echo 打完之后会把中转配置还回来（除非加 --permanent）。
echo.
python -X utf8 build_no_relay.py %*
echo.
pause