@echo off
chcp 65001 >nul
title [Orchestrator] 三大微服务一键协同总控
cls
echo ======================================================================
echo [Orchestrator] 正在并发拉起企业级三大解耦微服务独立控制台:
echo   1. [Port 8001] 数据中台 (Data Platform)
echo   2. [Port 8002] 评测中台 (Evaluation Platform)
echo   3. [Port 8000] 业务智能体 (Agent Core Service)
echo ======================================================================

start "Data Platform (Port 8001)" cmd /k "chcp 65001 >nul & \"%~dp0start_data_platform.bat\""
ping 127.0.0.1 -n 3 >nul

start "Evaluation Platform (Port 8002)" cmd /k "chcp 65001 >nul & \"%~dp0start_eval_platform.bat\""
ping 127.0.0.1 -n 3 >nul

start "Agent Service (Port 8000)" cmd /k "chcp 65001 >nul & \"%~dp0start_agent_service.bat\""

echo.
echo ======================================================================
echo [OK] 三大独立控制台窗口已全部拉起，正在持续输出实时运行日志！
echo [1] 业务智能体纯对话界面: http://127.0.0.1:8000/
echo [2] 数据中台文档切片管理: http://127.0.0.1:8001/
echo [3] 评测中台五阶段评估看板: http://127.0.0.1:8002/
echo ======================================================================
pause
