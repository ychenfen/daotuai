@echo off
rem 启动工作流服务（Windows）。需要聚合层已在 :8010 运行。
cd /d "%~dp0"
if "%PYTHON%"=="" set PYTHON=python
if "%WORKFLOW_DB%"=="" set WORKFLOW_DB=%~dp0data\workflow.db
%PYTHON% -m uvicorn wf.main:app --host 127.0.0.1 --port 8011
