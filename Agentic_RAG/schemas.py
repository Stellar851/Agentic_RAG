# -*- coding: utf-8 -*-
"""
Agentic RAG 业务对话服务数据契约层 (schemas.py)
精简纯净化版本：专注于会话管理与智能体流式问答
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    """新建会话请求体"""
    title: Optional[str] = Field("新对话", description="会话标题")


class ChatRequest(BaseModel):
    """发起 Agentic RAG 提问请求体"""
    query: str = Field(..., description="用户提问内容")
    session_id: Optional[str] = Field(None, description="会话 ID，若为空自动新建")
    history: Optional[List[Dict[str, Any]]] = Field(None, description="显式传递的历史消息")
    stream: Optional[bool] = Field(True, description="是否启用 SSE 流式输出")
    model_name: Optional[str] = Field(None, description="可选：指定本次问答使用的大模型名称（热切换）")
