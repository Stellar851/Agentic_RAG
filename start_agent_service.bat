@echo off
chcp 65001 >nul
title [Agent Service] 业务智能体对话中枢 - 8000
echo ======================================================================
echo 🤖 正在启动 Agentic RAG 业务对话服务 (Agent Service)
echo 📍 访问对话: http://localhost:8000/
echo 📋 接口文档: http://localhost:8000/docs
echo ======================================================================
cd /d "%~dp0"
".venv\Scripts\python.exe" -m uvicorn Agentic_RAG.app:app --host 0.0.0.0 --port 8000
pause
