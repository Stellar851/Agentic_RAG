"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
子路由模块：智能对话与流式问答 (routers/chat.py)

【模块定位】
负责多轮上下文会话管理、动态模型热切换路由、Agentic RAG ReAct 循环调度与 SSE 打字机流式输出。
严格保持卡片式、结构化控制台日志输出规范。
================================================================================
"""

import json
import traceback
from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from models import ACTIVE_LLM_MODEL
from agent import get_agent_service
from chat_history_db import get_chat_db
from schemas import ChatRequest

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

    if not user_query:
        return JSONResponse(status_code=400, content={"code": 400, "message": "提问内容不能为空"})

    db = get_chat_db()

    # 1. 确保 session_id 有效，如果未传则自动创建
    is_new_session = False
    if not session_id or not db.get_session(session_id):
        new_sess = db.create_session(title=user_query[:20] if len(user_query) > 20 else user_query)
        session_id = new_sess["session_id"]
        is_new_session = True

    try:
        # 2. 从数据库加载该会话的历史消息构建上下文
        db_messages = db.get_session_messages(session_id)

        chat_history_for_agent = []
        if explicit_history is not None:
            chat_history_for_agent = explicit_history
        else:
            for m in db_messages:
                chat_history_for_agent.append({
                    "role": m["role"],
                    "content": m["content"]
                })

        # 3. 保存用户的提问消息到 SQLite
        db.add_message(session_id=session_id, role="user", content=user_query)

        # 若是已有会话但只有默认标题 "新对话"，用第一个问题命名
        current_sess = db.get_session(session_id)
        if current_sess and current_sess.get("title") in ["新对话", ""]:
            title_text = user_query[:18] + ("..." if len(user_query) > 18 else "")
            db.update_session_title(session_id, title_text)

        agent_service = get_agent_service()
        mode_desc = "打字机流式 (SSE)" if stream_mode else "单次同步响应"
        final_model_name = target_model or ACTIVE_LLM_MODEL
        print(f"\n┌─── [步骤 1/3: 智能问答受理] " + "─" * 46)
        print(f"│ 👤 会话ID: {session_id[:8]}... | 模式: {mode_desc}")
        print(f"│ 🤖 调用主控模型: 【{final_model_name}】 (阿里百炼)")
        print(f"│ 💬 用户提问: \"{user_query}\"")
        print(f"└───" + "─" * 68)

        if stream_mode:
            def generate_stream():
                accumulated_answer = ""
                final_steps = []
                final_docs = []
                final_model = target_model or ACTIVE_LLM_MODEL

                # 发送会话初始元数据
                yield f"event: session_init\ndata: {json.dumps({'session_id': session_id, 'is_new_session': is_new_session, 'model_name': final_model}, ensure_ascii=False)}\n\n"

                for item in agent_service.ask_stream(user_query=user_query, chat_history=chat_history_for_agent, model_name=target_model):
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

                # 流式全部结束后，一次性将完整回答写入 SQLite 数据库
                if accumulated_answer:
                    db.add_message(
                        session_id=session_id,
                        role="assistant",
                        content=accumulated_answer,
                        steps=final_steps,
                        referenced_docs=final_docs
                    )
                docs_summary = "、".join([f"《{d}》" for d in final_docs]) if final_docs else "未直接引用独立文档"
                print(f"\n┌─── [步骤 3/3: 答案生成与流式交付完成] " + "─" * 38)
                print(f"│ 🤖 生成模型: 【{final_model}】 (阿里百炼)")
                print(f"│ 📊 回复总字数: {len(accumulated_answer)} 字符 | 引用源: {docs_summary}")
                print(f"│ ✅ 交付状态: 已成功推流给前端，并持久化写入 SQLite 对话库")
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
            # 传统同步 JSON 调用
            result = agent_service.ask(user_query=user_query, chat_history=chat_history_for_agent, model_name=target_model)
            answer = result.get("answer", "")
            steps = result.get("steps", [])
            referenced_docs = result.get("referenced_docs", [])
            model_used = result.get("model_name", target_model or ACTIVE_LLM_MODEL)

            db.add_message(
                session_id=session_id,
                role="assistant",
                content=answer,
                steps=steps,
                referenced_docs=referenced_docs
            )
            docs_summary = "、".join([f"《{d}》" for d in referenced_docs]) if referenced_docs else "未直接引用独立文档"
            print(f"\n┌─── [步骤 3/3: 答案生成与同步交付完成] " + "─" * 38)
            print(f"│ 🤖 生成模型: 【{model_used}】 (阿里百炼)")
            print(f"│ 📊 回复总字数: {len(answer)} 字符 | 引用源: {docs_summary}")
            print(f"│ ✅ 交付状态: 已成功写入 SQLite 对话历史")
            print(f"└───" + "─" * 68 + "\n")

            return {
                "code": 200,
                "data": {
                    "session_id": session_id,
                    "answer": answer,
                    "steps": steps,
                    "referenced_docs": referenced_docs,
                    "model_name": model_used,
                    "is_new_session": is_new_session
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

