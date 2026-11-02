"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
子路由模块：会话历史管理 (routers/session.py)

【模块定位】
负责多会话生命周期管理：新建会话、获取会话列表、加载特定会话上下文与删除会话。
================================================================================
"""

from typing import Optional
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from chat_history_db import get_chat_db
from schemas import CreateSessionRequest

router = APIRouter(prefix="/api/sessions", tags=["会话管理"])


@router.get("", summary="获取所有历史会话列表")
async def list_sessions():
    """获取所有历史会话列表"""
    try:
        db = get_chat_db()
        sessions = db.list_sessions()
        return {"code": 200, "data": sessions}
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": f"获取会话列表失败: {str(e)}"})


@router.post("", summary="新建空白会话")
async def create_session(req: Optional[CreateSessionRequest] = None):
    """新建一个空白会话"""
    try:
        title = "新对话"
        if req and req.title:
            title = req.title.strip() or "新对话"
        db = get_chat_db()
        session = db.create_session(title=title)
        print(f"\n💬 [API 新建会话] 会话 ID: {session['session_id'][:8]}... | 标题: '{title}'")
        return {"code": 200, "data": session}
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": f"创建会话失败: {str(e)}"})


@router.get("/{session_id}", summary="获取会话详情与历史消息")
async def get_session_details(session_id: str):
    """获取某个会话的详细信息及全部历史消息"""
    try:
        db = get_chat_db()
        session = db.get_session(session_id)
        if not session:
            return JSONResponse(status_code=404, content={"code": 404, "message": "未找到该会话"})
        messages = db.get_session_messages(session_id)
        return {
            "code": 200,
            "data": {
                "session": session,
                "messages": messages
            }
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": f"获取会话详情失败: {str(e)}"})


@router.delete("/{session_id}", summary="删除指定会话")
async def delete_session(session_id: str):
    """删除指定会话及所有关联消息"""
    try:
        db = get_chat_db()
        success = db.delete_session(session_id)
        if success:
            print(f"\n🗑️ [API 删除会话] 会话 ID: {session_id[:8]}... 已清理")
            return {"code": 200, "message": "会话已成功删除"}
        else:
            return JSONResponse(status_code=404, content={"code": 404, "message": "未找到要删除的会话"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": f"删除会话失败: {str(e)}"})

