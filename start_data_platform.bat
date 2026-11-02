@echo off
chcp 65001 >nul
title [Data Platform] 企业级数据中台微服务 - 8001
echo ======================================================================
echo 🚀 正在启动 企业级知识与数据中台微服务 (Data Platform)
echo 📍 监听端口: http://localhost:8001
echo 📋 接口文档: http://localhost:8001/docs
echo ======================================================================
cd /d "%~dp0services\data_platform"
"..\..\.venv\Scripts\python.exe" -m uvicorn app:app --host 0.0.0.0 --port 8001
pause
