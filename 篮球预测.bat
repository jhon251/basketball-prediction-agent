@echo off
title 篮球预测
cd /d C:\lanqiu

netstat -ano | findstr ":8765" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 goto running

echo [篮球预测] 正在启动服务（窗口"篮球预测服务"是服务本体，关闭它即停止服务）...
start "篮球预测服务" /D "C:\lanqiu" "C:\lanqiu\.venv\Scripts\python.exe" -m basketball_agent.cli.main serve --open

set /a tries=0
:waitloop
ping -n 2 127.0.0.1 >nul
curl -s -m 2 http://127.0.0.1:8765/api/health >nul 2>&1
if %errorlevel%==0 goto ready
set /a tries+=1
if %tries% lss 10 goto waitloop
echo [篮球预测] 服务未能在约 15 秒内就绪，请查看"篮球预测服务"窗口里的报错。
pause
exit /b 1

:running
echo [篮球预测] 服务已在运行，打开页面...
start "" http://127.0.0.1:8765
ping -n 3 127.0.0.1 >nul
exit /b 0

:ready
echo [篮球预测] 服务已就绪：http://127.0.0.1:8765 （浏览器应已自动弹出）
ping -n 4 127.0.0.1 >nul
exit /b 0
