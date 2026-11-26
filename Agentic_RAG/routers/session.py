# -*- coding: utf-8 -*-
"""
Agentic RAG 会话路由代理模块 (routers/session.py)
【纯无状态模式】：所有会话生命周期与消息历史 100% 委托数据中台 (Port 8001) 统一管理与持久化
"""

import os
from typing import Optional
from fastapi import APIRouter
from fastapi.responses import JSONResponse
import requests

from schemas import CreateSessionRequest

try:
    from common.logging_config import get_trace_id
except Exception:
    get_trace_id = lambda: "-"

router = APIRouter(prefix="/api/sessions", tags=["会话代理"])

DATA_PLATFORM_URL = os.getenv("DATA_PLATFORM_URL", "http://127.0.0.1:8001")


@router.get("", summary="获取当前智能体的历史会话列表")
def list_sessions():
    try:
        resp = requests.get(
            f"{DATA_PLATFORM_URL}/api/v1/data/sessions",
            params={"agent_id": "agentic_rag_core"},
            headers={"X-Trace-ID": get_trace_id()},
            timeout=5
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": f"连接数据中台失败: {str(e)}"})


@router.post("", summary="向数据中台新建空白会话")
def create_session(req: Optional[CreateSessionRequest] = None):
    try:
        title = "新对话"
        if req and req.title:
            title = req.title.strip() or "新对话"
        resp = requests.post(
            f"{DATA_PLATFORM_URL}/api/v1/data/sessions",
            json={"title": title, "agent_id": "agentic_rag_core"},
            headers={"X-Trace-ID": get_trace_id()},
            timeout=5
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": f"数据中台创建会话失败: {str(e)}"})


@router.get("/{session_id}", summary="从数据中台获取会话详情与历史消息")
def get_session_details(session_id: str):
    try:
        resp = requests.get(
            f"{DATA_PLATFORM_URL}/api/v1/data/sessions/{session_id}",
            headers={"X-Trace-ID": get_trace_id()},
            timeout=5
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": f"从数据中台获取会话失败: {str(e)}"})


@router.delete("/{session_id}", summary="从数据中台级联删除指定会话")
def delete_session(session_id: str):
    try:
        resp = requests.delete(
            f"{DATA_PLATFORM_URL}/api/v1/data/sessions/{session_id}",
            headers={"X-Trace-ID": get_trace_id()},
            timeout=5
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": f"数据中台删除会话失败: {str(e)}"})
