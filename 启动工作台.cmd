@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动短剧工作台（聚合层 8010 + 工作流 8011）...
py -3.12 run.py %*
if errorlevel 1 pause
