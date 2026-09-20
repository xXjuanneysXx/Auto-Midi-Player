@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ========================================
echo   打包 main.py -> dist\AutoPlay\AutoPlay.exe
echo   （不带黑框：--noconsole）
echo ========================================

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [1/2] 安装 PyInstaller...
    python -m pip install pyinstaller
) else (
    echo [1/2] PyInstaller 已安装
)

echo [2/2] 开始打包（配置在 AutoPlay.spec 里）...
python -m PyInstaller --noconfirm --clean AutoPlay.spec

if errorlevel 1 (
    echo.
    echo 打包失败！
    pause
    exit /b 1
)

echo.
echo ========================================
echo   打包完成
echo   可执行文件: dist\AutoPlay\AutoPlay.exe
echo ========================================
pause
exit /b 0
