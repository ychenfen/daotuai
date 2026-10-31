@echo off
rem 启动聚合网关（Windows）。可用 set PYTHON=... 指定解释器。
cd /d "%~dp0"
if "%PYTHON%"=="" set PYTHON=python
if "%AGG_DB%"=="" set AGG_DB=%~dp0data\agg.db
%PYTHON% -m uvicorn app.main:app --host 127.0.0.1 --port 8010
