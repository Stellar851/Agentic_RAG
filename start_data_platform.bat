@echo off
chcp 65001 >nul
title [Data Platform] 企业级数据中台微服务 - 8001
cls
echo ======================================================================
echo [Data Platform] 正在启动企业级数据中台 (Port 8001)...
echo 访问管理大屏: http://127.0.0.1:8001/
echo 接口调试文档: http://127.0.0.1:8001/docs
echo ======================================================================
cd /d "%~dp0services\data_platform"
"..\..\.venv\Scripts\python.exe" -u -m uvicorn app:app --host 0.0.0.0 --port 8001
pause
