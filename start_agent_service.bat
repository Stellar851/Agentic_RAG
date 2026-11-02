@echo off
chcp 65001 >nul
title [Agent Service] 业务智能体对话中枢 - 8000
cls
echo ======================================================================
echo [Agent Service] 正在启动业务智能体对话服务 (Port 8000)...
echo 访问对话界面: http://127.0.0.1:8000/
echo 接口调试文档: http://127.0.0.1:8000/docs
echo ======================================================================
cd /d "%~dp0"
".venv\Scripts\python.exe" -u -m uvicorn Agentic_RAG.app:app --host 0.0.0.0 --port 8000
pause
