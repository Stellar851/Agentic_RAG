@echo off
chcp 65001 >nul
title [Evaluation Platform] 工业级评测中台微服务 - 8002
echo ======================================================================
echo 🧪 正在启动 工业级评测中台微服务 (Evaluation Platform)
echo 📍 访问看板: http://localhost:8002/
echo 📋 接口文档: http://localhost:8002/docs
echo ======================================================================
cd /d "%~dp0services\eval_platform"
"..\..\.venv\Scripts\python.exe" -m uvicorn app:app --host 0.0.0.0 --port 8002
pause
