"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
Flask Web 应用程序核心服务 (app.py)

【模块定位】
本文件是系统的 Web 服务总入口：
1. 基于 Flask 提供 RESTful API 与静态页面路由。
2. 功能端点 (API Endpoints):
   - `GET  /`                     : 前端交互主页面
   - `GET  /api/status`           : 系统健康状态自检 (DeepSeek Key / Ollama / ChromaDB)
   - `POST /api/upload`           : 上传一个或多个文档，自动存入 uploads/ 并向量化持久化到 ChromaDB
   - `GET  /api/documents`        : 查询知识库中所有已入库的文档清单与统计
   - `DELETE /api/documents/<name>`: 删除指定文档（同步清除 Chroma 向量与 uploads 物理文件）
   - `POST /api/chat`             : 发起 Agentic RAG 提问，返回综合答案、多跳思考证据链与参考文档
================================================================================
"""

import os
import sys
import traceback
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
from werkzeug.utils import secure_filename

# 确保 Windows 终端输出 UTF-8 编码，防止 emoji 或特殊字符在 GBK 环境下抛出 UnicodeEncodeError
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 导入内部系统模块
from models import ACTIVE_LLM_API_KEY_ENV_VAR, ACTIVE_LLM_MODEL, get_ollama_embeddings
from vector_store import get_vector_store, UPLOAD_DIR
from agent import get_agent_service
from chat_history_db import get_chat_db

# 初始化 Flask 实例
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_AS_ASCII"] = False  # 确保中文正确输出
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 限制单次上传最大 64MB

# 允许上传的文件格式后缀集合
ALLOWED_EXTENSIONS = {".docx", ".pdf", ".pptx", ".txt", ".md", ".json", ".csv"}


def is_allowed_file(filename: str) -> bool:
    """检查上传的文件扩展名是否在允许列表内"""
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_EXTENSIONS


# ==============================================================================
# 一、 基础页面与健康检测路由
# ==============================================================================

@app.route("/")
def index():
    """渲染前端交互主页面"""
    return render_template("index.html")


@app.route("/api/status", methods=["GET"])
def get_system_status():
    """
    【API】获取后端各组件（主大模型 API、Ollama 本地服务、ChromaDB 向量库）健康状态
    """
    try:
        # 1. 检查主大模型 API Key
        deepseek_status = {
            "connected": False,
            "message": "",
            "model_name": ACTIVE_LLM_MODEL
        }
        key = os.getenv(ACTIVE_LLM_API_KEY_ENV_VAR)
        if key and key.strip():
            masked = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "***"
            deepseek_status["connected"] = True
            deepseek_status["message"] = f"密钥读取成功 ({masked})"
        else:
            deepseek_status["message"] = f"未找到环境变量 `{ACTIVE_LLM_API_KEY_ENV_VAR}`"

        # 2. 检查 Ollama 服务
        ollama_status = {"connected": False, "message": ""}
        try:
            emb = get_ollama_embeddings()
            vec = emb.embed_query("健康检查")
            ollama_status = {"connected": True, "message": f"连接成功！向量维度: {len(vec)}"}
        except Exception as e:
            ollama_status["message"] = f"Ollama 服务异常: {str(e)}"

        # 3. 检查向量数据库与文档总数
        vs = get_vector_store()
        doc_list = vs.list_all_documents()

        return jsonify({
            "code": 200,
            "status": "success",
            "data": {
                "deepseek": deepseek_status,
                "ollama": ollama_status,
                "documents_count": len(doc_list),
                "total_chunks": sum(d.get("total_chunks", 0) for d in doc_list)
            }
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "status": "error",
            "message": f"系统自检发生异常: {str(e)}"
        }), 500


# ==============================================================================
# 二、 文档管理 API
# ==============================================================================

@app.route("/api/upload", methods=["POST"])
def upload_documents():
    """
    【API】上传一个或多个文档文件：
    1. clean_mode:
       - "auto" (默认): 自动预检文档。若检测到复杂 ASCII 架构图，返回 needs_confirmation 提示用户选择；
                        若未达图表阈值，则自动以代码普通模式 (rule) 极速秒级入库，零弹窗骚扰；
       - "llm" / "rule": 用户或前端已明确指定的清洗模式，直接执行流水线入库。
    """
    if "files" not in request.files:
        return jsonify({"code": 400, "message": "请求中未包含文件参数 (files)"}), 400

    files = request.files.getlist("files")
    if not files or files[0].filename == "":
        return jsonify({"code": 400, "message": "未选择任何有效文件"}), 400

    clean_mode = (request.form.get("clean_mode") or "auto").strip().lower()
    if clean_mode not in ["auto", "llm", "rule"]:
        clean_mode = "auto"

    vs = get_vector_store()
    saved_files_info = []
    errors = []

    # 第一阶段：保存文件并做格式白名单检查
    for file in files:
        original_name = file.filename
        if not original_name:
            continue

        if not is_allowed_file(original_name):
            errors.append(f"文件【{original_name}】格式不支持，仅支持 .pptx, .docx, .pdf, .txt, .md, .json, .csv")
            continue

        try:
            safe_name = os.path.basename(original_name).replace("/", "").replace("\\", "")
            save_path = os.path.join(UPLOAD_DIR, safe_name)
            file.save(save_path)
            file_size_kb = round(os.path.getsize(save_path) / 1024, 2)
            saved_files_info.append({
                "original_name": original_name,
                "safe_name": safe_name,
                "save_path": save_path,
                "file_size_kb": file_size_kb
            })
        except Exception as e:
            traceback.print_exc()
            errors.append(f"保存文件【{original_name}】失败: {str(e)}")

    if not saved_files_info:
        return jsonify({
            "code": 400,
            "message": "未能成功保存任何有效文件",
            "data": {"errors": errors}
        }), 400

    # 第二阶段：如果为 auto 模式，自动扫描是否触发复杂架构图阈值
    if clean_mode == "auto":
        detected_files = []
        for item in saved_files_info:
            try:
                diag_info = vs.inspect_document(item["save_path"])
                if diag_info.get("is_real_diagram"):
                    detected_files.append({
                        "filename": item["safe_name"],
                        "file_size_kb": item["file_size_kb"],
                        "corner_count": diag_info.get("corner_count", 0),
                        "arrow_count": diag_info.get("arrow_count", 0),
                        "total_box_chars": diag_info.get("total_box_chars", 0)
                    })
            except Exception as e:
                print(f"⚠️ [文档预检异常] 预检【{item['safe_name']}】失败: {e}")

        # 如果检测到复杂架构图，返回 needs_confirmation 状态，由前端弹窗让用户针对性选择
        if detected_files:
            print(f"\n🧩 [触发架构图阈值] 本次上传的 {len(saved_files_info)} 个文件中，有 {len(detected_files)} 个包含复杂字符架构框图！")
            for df in detected_files:
                print(f"   - 【{df['filename']}】(闭合拐角: {df['corner_count']}, 流程箭头: {df['arrow_count']}, 拓扑符号: {df['total_box_chars']})")
            print("   ⏳ 正在等待前端用户选择清洗模式 (大模型升维清洗 或 代码普通清洗)...")
            return jsonify({
                "code": 200,
                "status": "needs_confirmation",
                "message": f"检测到文档包含复杂字符架构图/流程图，请选择清洗方式",
                "detected_files": detected_files,
                "all_files": [item["safe_name"] for item in saved_files_info]
            })

        # 未检测到复杂架构图，自动直接以代码普通模式 (rule) 极速入库
        print(f"\nℹ️ [自动入库] 本批上传的 {len(saved_files_info)} 个文件均未达到架构图阈值，自动采用【代码普通清洗 (0 Token)】极速入库！")
        clean_mode = "rule"

    # 第三阶段：执行实际入库流程 (针对 clean_mode 为 rule 或 llm)
    uploaded_results = []
    for item in saved_files_info:
        try:
            mode_text = "大模型智能升维清洗 (Mermaid/Markdown)" if clean_mode == "llm" else "代码规则普通清洗 (正则降噪)"
            print(f"\n📥 [处理入库文件] 文件名: 【{item['safe_name']}】 | 大小: {item['file_size_kb']} KB | 清洗模式: 【{mode_text}】")
            stats = vs.process_and_add_document(item["save_path"], clean_mode=clean_mode)
            uploaded_results.append(stats)
            if stats.get("is_duplicate"):
                print(f"💡 [查重处理结果] {stats.get('message')}")
        except Exception as e:
            traceback.print_exc()
            print(f"❌ [入库处理异常] 文件【{item['safe_name']}】处理失败: {e}")
            errors.append(f"文件【{item['safe_name']}】处理失败: {str(e)}")

    return jsonify({
        "code": 200 if uploaded_results else 400,
        "status": "completed",
        "message": f"成功入库 {len(uploaded_results)} 个文档" + (f"，{len(errors)} 个处理失败" if errors else ""),
        "data": {
            "success": uploaded_results,
            "errors": errors
        }
    })


@app.route("/api/upload/confirm", methods=["POST"])
def confirm_upload():
    """
    【API】用户在弹窗确认了清洗模式后，对已保存的文档执行向量化入库
    """
    try:
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        clean_mode = (data.get("clean_mode") or "rule").strip().lower()
        if clean_mode not in ["llm", "rule"]:
            clean_mode = "rule"

        if not filenames:
            return jsonify({"code": 400, "message": "未提供待入库的文件名列表"}), 400

        vs = get_vector_store()
        uploaded_results = []
        errors = []

        mode_text = "大模型智能升维清洗 (Mermaid/Markdown)" if clean_mode == "llm" else "代码规则普通清洗 (正则降噪)"
        print(f"\n🎯 [用户确认清洗模式] 选择模式: 【{mode_text}】 | 文件数量: {len(filenames)}")

        for filename in filenames:
            safe_name = os.path.basename(filename).replace("/", "").replace("\\", "")
            save_path = os.path.join(UPLOAD_DIR, safe_name)
            if not os.path.exists(save_path):
                errors.append(f"文件【{safe_name}】在服务器缓存中未找到")
                continue

            try:
                stats = vs.process_and_add_document(save_path, clean_mode=clean_mode)
                uploaded_results.append(stats)
                if stats.get("is_duplicate"):
                    print(f"💡 [查重处理结果] {stats.get('message')}")
            except Exception as e:
                traceback.print_exc()
                errors.append(f"文件【{safe_name}】处理失败: {str(e)}")

        return jsonify({
            "code": 200 if uploaded_results else 400,
            "status": "completed",
            "message": f"成功入库 {len(uploaded_results)} 个文档" + (f"，{len(errors)} 个处理失败" if errors else ""),
            "data": {
                "success": uploaded_results,
                "errors": errors
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": 500, "message": f"确认入库处理失败: {str(e)}"}), 500


@app.route("/api/upload/cancel", methods=["POST"])
def cancel_upload():
    """
    【API】用户取消上传时，清理未入库的暂存文件
    """
    try:
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        for filename in filenames:
            safe_name = os.path.basename(filename).replace("/", "").replace("\\", "")
            save_path = os.path.join(UPLOAD_DIR, safe_name)
            if os.path.exists(save_path):
                try:
                    os.remove(save_path)
                except Exception:
                    pass
        return jsonify({"code": 200, "message": "上传已取消，暂存文件已清理"})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e)}), 500


@app.route("/api/documents", methods=["GET"])
def get_documents_list():
    """
    【API】获取知识库中全部已存储的文档清单及基本统计
    """
    try:
        vs = get_vector_store()
        docs = vs.list_all_documents()
        return jsonify({
            "code": 200,
            "data": docs
        })
    except Exception as e:
        return jsonify({"code": 500, "message": f"获取文档列表失败: {str(e)}"}), 500


@app.route("/api/documents/<path:doc_name>/chunks", methods=["GET"])
def get_document_chunks(doc_name: str):
    """
    【API】获取指定文档的所有切片文本、元数据及清洗模式，供前端可视化检查与校验
    """
    try:
        vs = get_vector_store()
        res = vs.collection.get(
            where={"doc_name": doc_name},
            include=["documents", "metadatas"]
        )
        if not res or not res.get("documents"):
            return jsonify({"code": 404, "message": f"未找到文档【{doc_name}】的切片数据"}), 404

        chunks = []
        for text, meta in zip(res["documents"], res["metadatas"]):
            chunks.append({
                "chunk_index": meta.get("chunk_index", 0),
                "total_chunks": meta.get("total_chunks", 0),
                "upload_time": meta.get("upload_time", ""),
                "clean_mode": meta.get("clean_mode", "rule"),
                "content_hash": meta.get("content_hash", ""),
                "char_count": len(text),
                "text": text
            })

        chunks.sort(key=lambda x: x["chunk_index"])
        total_chars = sum(c["char_count"] for c in chunks)
        clean_mode = chunks[0]["clean_mode"] if chunks else "rule"

        return jsonify({
            "code": 200,
            "data": {
                "doc_name": doc_name,
                "total_chunks": len(chunks),
                "total_chars": total_chars,
                "clean_mode": clean_mode,
                "content_hash": chunks[0].get("content_hash", "") if chunks else "",
                "chunks": chunks
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": 500, "message": f"获取文档切片失败: {str(e)}"}), 500


@app.route("/api/documents/<path:doc_name>", methods=["DELETE"])
def delete_document(doc_name: str):
    """
    【API】删除指定文档：
    同步移除 ChromaDB 中的所有对应切片以及 uploads 目录下的原文件
    """
    try:
        print(f"\n🗑️ [API 请求删除文档] 正在清空知识库文档: 【{doc_name}】...")
        vs = get_vector_store()
        success = vs.delete_document(doc_name=doc_name, delete_file=True)
        if success:
            return jsonify({"code": 200, "message": f"文档【{doc_name}】已成功移除"})
        else:
            return jsonify({"code": 400, "message": f"删除文档【{doc_name}】失败"}), 400
    except Exception as e:
        return jsonify({"code": 500, "message": f"删除文档发生异常: {str(e)}"}), 500


# ==============================================================================
# 三、 会话管理与历史记忆 API
# ==============================================================================

@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    """获取所有历史会话列表"""
    try:
        db = get_chat_db()
        sessions = db.list_sessions()
        return jsonify({"code": 200, "data": sessions})
    except Exception as e:
        return jsonify({"code": 500, "message": f"获取会话列表失败: {str(e)}"}), 500


@app.route("/api/sessions", methods=["POST"])
def create_session():
    """新建一个空白会话"""
    try:
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "新对话").strip() or "新对话"
        db = get_chat_db()
        session = db.create_session(title=title)
        print(f"\n💬 [API 新建会话] 会话 ID: {session['session_id'][:8]}... | 标题: '{title}'")
        return jsonify({"code": 200, "data": session})
    except Exception as e:
        return jsonify({"code": 500, "message": f"创建会话失败: {str(e)}"}), 500


@app.route("/api/sessions/<session_id>", methods=["GET"])
def get_session_details(session_id: str):
    """获取某个会话的详细信息及全部历史消息"""
    try:
        db = get_chat_db()
        session = db.get_session(session_id)
        if not session:
            return jsonify({"code": 404, "message": "未找到该会话"}), 404
        messages = db.get_session_messages(session_id)
        return jsonify({
            "code": 200,
            "data": {
                "session": session,
                "messages": messages
            }
        })
    except Exception as e:
        return jsonify({"code": 500, "message": f"获取会话详情失败: {str(e)}"}), 500


@app.route("/api/sessions/<session_id>", methods=["DELETE"])
def delete_session(session_id: str):
    """删除指定会话及所有关联消息"""
    try:
        db = get_chat_db()
        success = db.delete_session(session_id)
        if success:
            print(f"\n🗑️ [API 删除会话] 会话 ID: {session_id[:8]}... 已清理")
            return jsonify({"code": 200, "message": "会话已成功删除"})
        else:
            return jsonify({"code": 404, "message": "未找到要删除的会话"}), 404
    except Exception as e:
        return jsonify({"code": 500, "message": f"删除会话失败: {str(e)}"}), 500


# ==============================================================================
# 四、 Agentic RAG 智能问答对话 API (接入多轮上下文与历史记忆)
# ==============================================================================

@app.route("/api/chat", methods=["POST"])
def chat():
    """
    【API】Agentic RAG 对话接口 (支持持久化会话、多轮上下文记忆与打字机式流式生成)
    请求体 JSON:
    {
        "session_id": "可选，若为空自动新建",
        "query": "用户的问题",
        "history": 可选，显式传递的历史消息列表,
        "stream": 可选，默认 True 开启流式 SSE 输出
    }
    """
    import json
    data = request.get_json(silent=True) or {}
    user_query = (data.get("query") or "").strip()
    session_id = (data.get("session_id") or "").strip()
    explicit_history = data.get("history")
    stream_mode = data.get("stream", True)

    if not user_query:
        return jsonify({"code": 400, "message": "提问内容不能为空"}), 400

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
        mode_desc = "流式(SSE)" if stream_mode else "同步响应"
        print(f"\n🤖 [收到问答请求] 会话 ID: {session_id[:8]}... | 模式: {mode_desc} | 问题: '{user_query}'")

        if stream_mode:
            def generate_stream():
                accumulated_answer = ""
                final_steps = []
                final_docs = []

                # 发送会话初识元数据
                yield f"event: session_init\ndata: {json.dumps({'session_id': session_id, 'is_new_session': is_new_session}, ensure_ascii=False)}\n\n"

                for item in agent_service.ask_stream(user_query=user_query, chat_history=chat_history_for_agent):
                    ev = item.get("event", "message")
                    d = item.get("data", {})

                    if ev == "token":
                        accumulated_answer += d.get("delta", "")
                    elif ev == "done":
                        accumulated_answer = d.get("answer", accumulated_answer)
                        final_steps = d.get("steps", [])
                        final_docs = d.get("referenced_docs", [])

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
                ref_info = f"引用文档: {final_docs}" if final_docs else "未直接引用特定文档"
                print(f"✨ [问答流式输出完毕] 会话 ID: {session_id[:8]}... | 回答长度: {len(accumulated_answer)} 字符 | {ref_info}")

            return Response(
                stream_with_context(generate_stream()),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no"
                }
            )

        else:
            # 兼容传统同步 JSON 调用
            result = agent_service.ask(user_query=user_query, chat_history=chat_history_for_agent)
            answer = result.get("answer", "")
            steps = result.get("steps", [])
            referenced_docs = result.get("referenced_docs", [])

            db.add_message(
                session_id=session_id,
                role="assistant",
                content=answer,
                steps=steps,
                referenced_docs=referenced_docs
            )
            ref_info = f"引用文档: {referenced_docs}" if referenced_docs else "未直接引用特定文档"
            print(f"✨ [问答计算完毕] 会话 ID: {session_id[:8]}... | 回答长度: {len(answer)} 字符 | {ref_info}")

            return jsonify({
                "code": 200,
                "data": {
                    "session_id": session_id,
                    "answer": answer,
                    "steps": steps,
                    "referenced_docs": referenced_docs,
                    "is_new_session": is_new_session
                }
            })

    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": f"Agent 问答服务异常: {str(e)}"
        }), 500


# ==============================================================================
# 四、 服务入口
# ==============================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 70)
    #print("🚀 Agentic RAG 企业智能关联知识库系统正在启动...")
    print(f"http://127.0.0.1:{port}")
    print("=" * 70)
    app.run(host="0.0.0.0", port=port, debug=True)
