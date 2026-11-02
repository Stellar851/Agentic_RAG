@echo off
chcp 65001 >nul
title [Orchestrator] 三大微服务一键协同总控
echo ======================================================================
echo 🌟 正在并发启动企业级三大解耦微服务：
echo   1. [Port 8001] 数据中台 (Data Platform)
echo   2. [Port 8002] 评测中台 (Evaluation Platform)
echo   3. [Port 8000] 业务智能体 (Agent Core Service)
echo ======================================================================
start "Data Platform (8001)" cmd /k "%~dp0start_data_platform.bat"
timeout /t 2 >nul
start "Evaluation Platform (8002)" cmd /k "%~dp0start_eval_platform.bat"
timeout /t 2 >nul
start "Agent Service (8000)" cmd /k "%~dp0start_agent_service.bat"
echo.
echo ✅ 三大微服务窗口已全部拉起！
echo 👉 业务智能体对话: http://localhost:8000/
echo 👉 评测中台中枢看板: http://localhost:8002/
echo 👉 数据中台接口文档: http://localhost:8001/docs
echo ======================================================================
