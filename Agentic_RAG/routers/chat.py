"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
子路由模块：智能对话与流式问答 (routers/chat.py)

【模块定位】
负责多轮上下文会话管理、动态模型热切换路由、Agentic RAG ReAct 循环调度与 SSE 打字机流式输出。
严格保持卡片式、结构化控制台日志输出规范。
================================================================================
"""

import os
import json
import uuid
import asyncio
import traceback
from typing import Optional, List, Dict, Any
from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from models import ACTIVE_LLM_MODEL
from agent import get_agent_service
# 已解耦：会话与消息管理 100% 委托数据中台 (Port 8001)
from schemas import ChatRequest

import requests

DATA_PLATFORM_URL = os.getenv("DATA_PLATFORM_URL", "http://127.0.0.1:8001")
try:
    from common.logging_config import get_trace_id
except Exception:
    get_trace_id = lambda: "-"


def _dp_get_session(session_id: str) -> Optional[dict]:
    try:
        resp = requests.get(
            f"{DATA_PLATFORM_URL}/api/v1/data/sessions/{session_id}",
            headers={"X-Trace-ID": get_trace_id()},
            timeout=5
        )
        if resp.status_code == 200:
            return resp.json().get("data", {}).get("session")
    except Exception:
        pass
    return None


def _dp_create_session(title: str) -> dict:
    try:
        resp = requests.post(
            f"{DATA_PLATFORM_URL}/api/v1/data/sessions",
            json={"title": title, "agent_id": "agentic_rag_core"},
            headers={"X-Trace-ID": get_trace_id()},
            timeout=5
        )
        if resp.status_code == 200:
            return resp.json().get("data", {})
    except Exception:
        pass
    return {"session_id": "fallback-session"}


def _dp_get_messages(session_id: str) -> list:
    try:
        resp = requests.get(
            f"{DATA_PLATFORM_URL}/api/v1/data/sessions/{session_id}",
            headers={"X-Trace-ID": get_trace_id()},
            timeout=5
        )
        if resp.status_code == 200:
            return resp.json().get("data", {}).get("messages", [])
    except Exception:
        pass
    return []


def _dp_update_session_title(session_id: str, title: str):
    try:
        requests.put(
            f"{DATA_PLATFORM_URL}/api/v1/data/sessions/{session_id}/title",
            json={"title": title},
            headers={"X-Trace-ID": get_trace_id()},
            timeout=5
        )
    except Exception as e:
        print(f"⚠️ [数据中台更新标题异常] {e}")


def _dp_add_message(session_id: str, role: str, content: str, steps: list = None, referenced_docs: list = None):
    try:
        requests.post(
            f"{DATA_PLATFORM_URL}/api/v1/data/sessions/{session_id}/messages",
            json={
                "role": role,
                "content": content,
                "steps": steps or [],
                "referenced_docs": referenced_docs or []
            },
            headers={"X-Trace-ID": get_trace_id()},
            timeout=5
        )
    except Exception as e:
        print(f"⚠️ [数据中台消息持久化异常] {e}")


router = APIRouter(prefix="/api", tags=["智能对话中枢"])


@router.post("/chat", summary="发起 Agentic RAG 智能问答提问")
async def chat(req: ChatRequest):
    """
    【API】Agentic RAG 对话接口 (支持持久化会话、多轮上下文记忆、模型热切换与打字机式流式生成)
    """
    user_query = req.query.strip()
    session_id = (req.session_id or "").strip()
    explicit_history = req.history
    stream_mode = req.stream if req.stream is not None else True
    target_model = (req.model_name or "").strip() or None
    should_persist = req.persist if req.persist is not None else True

    if not user_query:
        return JSONResponse(status_code=400, content={"code": 400, "message": "提问内容不能为空"})

    # 1. 确保 session_id 有效；评测等临时模式 (persist=False) 不写入数据中台历史库
    is_new_session = False
    if should_persist:
        if not session_id or not await asyncio.to_thread(_dp_get_session, session_id):
            new_sess = await asyncio.to_thread(
                _dp_create_session,
                user_query[:20] if len(user_query) > 20 else user_query
            )
            session_id = new_sess["session_id"]
            is_new_session = True
    else:
        if not session_id:
            session_id = f"ephemeral-{uuid.uuid4().hex[:12]}"
            is_new_session = True

    try:
        # 2. 构建多轮上下文；非持久化模式跳过数据中台查询
        chat_history_for_agent = []
        if explicit_history is not None:
            chat_history_for_agent = explicit_history
        elif should_persist:
            db_messages = await asyncio.to_thread(_dp_get_messages, session_id)
            for m in db_messages:
                chat_history_for_agent.append({
                    "role": m["role"],
                    "content": m["content"]
                })

        # 3. 保存用户的提问消息并更新默认标题（仅持久化模式）
        if should_persist:
            await asyncio.to_thread(_dp_add_message, session_id, "user", user_query)
            current_sess = await asyncio.to_thread(_dp_get_session, session_id)
            if current_sess and current_sess.get("title") in ["新对话", ""]:
                title_text = user_query[:18] + ("..." if len(user_query) > 18 else "")
                await asyncio.to_thread(_dp_update_session_title, session_id, title_text)

        agent_service = get_agent_service()
        mode_desc = "打字机流式 (SSE)" if stream_mode else "单次同步响应"
        final_model_name = target_model or ACTIVE_LLM_MODEL
        print(f"\n┌─── [步骤 1/3: 智能问答受理] " + "─" * 46)
        print(f"│ 👤 会话ID: {session_id[:8]}... | 模式: {mode_desc} | 持久化: {should_persist}")
        print(f"│ 🤖 调用主控模型: 【{final_model_name}】 (阿里百炼)")
        print(f"│ 💬 用户提问: \"{user_query}\"")
        print(f"└───" + "─" * 68)

        if stream_mode:
            async def generate_stream():
                accumulated_answer = ""
                final_steps = []
                final_docs = []
                final_model = target_model or ACTIVE_LLM_MODEL

                yield f"event: session_init\ndata: {json.dumps({'session_id': session_id, 'is_new_session': is_new_session, 'model_name': final_model}, ensure_ascii=False)}\n\n"

                async for item in agent_service.ask_stream_async(
                    user_query=user_query,
                    chat_history=chat_history_for_agent,
                    model_name=target_model,
                ):
                    ev = item.get("event", "message")
                    d = item.get("data", {})

                    if ev == "token":
                        accumulated_answer += d.get("delta", "")
                    elif ev == "done":
                        accumulated_answer = d.get("answer", accumulated_answer)
                        final_steps = d.get("steps", [])
                        final_docs = d.get("referenced_docs", [])
                        final_model = d.get("model_name", final_model)

                    yield f"event: {ev}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n"

                if should_persist and accumulated_answer:
                    await asyncio.to_thread(
                        _dp_add_message,
                        session_id,
                        "assistant",
                        accumulated_answer,
                        final_steps,
                        final_docs
                    )
                docs_summary = "、".join([f"《{d}》" for d in final_docs]) if final_docs else "未直接引用独立文档"
                print(f"\n┌─── [步骤 3/3: 答案生成与流式交付完成] " + "─" * 38)
                print(f"│ 🤖 生成模型: 【{final_model}】 (阿里百炼)")
                print(f"│ 📊 回复总字数: {len(accumulated_answer)} 字符 | 引用源: {docs_summary}")
                print(f"│ ✅ 交付状态: {'已成功推流给前端，并持久化写入 SQLite 对话库' if should_persist else '已成功推流（临时评测模式未入库）'}")
                print(f"└───" + "─" * 68 + "\n")

            return StreamingResponse(
                generate_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no"
                }
            )

        else:
            result = await asyncio.to_thread(
                agent_service.ask,
                user_query=user_query,
                chat_history=chat_history_for_agent,
                model_name=target_model
            )
            answer = result.get("answer", "")
            steps = result.get("steps", [])
            referenced_docs = result.get("referenced_docs", [])
            model_used = result.get("model_name", target_model or ACTIVE_LLM_MODEL)

            if should_persist:
                await asyncio.to_thread(
                    _dp_add_message,
                    session_id,
                    "assistant",
                    answer,
                    steps,
                    referenced_docs
                )
            docs_summary = "、".join([f"《{d}》" for d in referenced_docs]) if referenced_docs else "未直接引用独立文档"
            print(f"\n┌─── [步骤 3/3: 答案生成与同步交付完成] " + "─" * 38)
            print(f"│ 🤖 生成模型: 【{model_used}】 (阿里百炼)")
            print(f"│ 📊 回复总字数: {len(answer)} 字符 | 引用源: {docs_summary}")
            print(f"│ ✅ 交付状态: {'已成功写入 SQLite 对话历史' if should_persist else '同步响应完成（临时评测模式未入库）'}")
            print(f"└───" + "─" * 68 + "\n")

            return {
                "code": 200,
                "data": {
                    "session_id": session_id,
                    "answer": answer,
                    "steps": steps,
                    "referenced_docs": referenced_docs,
                    "contexts": result.get("contexts", []),
                    "model_name": model_used,
                    "is_new_session": is_new_session,
                    "success": result.get("success", True),
                    "error_type": result.get("error_type"),
                }
            }

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={
                "code": 500,
                "message": f"Agent 问答服务异常: {str(e)}"
            }
        )

