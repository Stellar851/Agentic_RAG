@echo off
chcp 65001 >nul
title [Evaluation Platform] 工业级评测中台微服务 - 8002
cls
echo ======================================================================
echo [Eval Platform] 正在启动工业级评测中台 (Port 8002)...
echo 访问评测看板: http://127.0.0.1:8002/
echo 接口调试文档: http://127.0.0.1:8002/docs
echo ======================================================================
cd /d "%~dp0services\eval_platform"
"..\..\.venv\Scripts\python.exe" -u -m uvicorn app:app --host 0.0.0.0 --port 8002
pause
