@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 空座宝 - 校园自习室空位地图

echo ============================================
echo   空座宝 正在启动...
echo ============================================
echo.
echo   本机访问:  http://127.0.0.1:5000
echo   手机访问:  见下面列出的局域网地址（手机需连同一个 WiFi）
echo.

for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4"') do (
  for /f "tokens=1" %%b in ("%%a") do echo   http://%%b:5000
)

echo.
echo   关闭这个窗口就会停止服务
echo ============================================
echo.

python app.py

pause
