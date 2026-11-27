"""
================================================================================
企业级统一数据中台微服务 (Data Platform)
主服务接入与路由网关 (app.py)

【微服务定位】
端口: 8001
职责:
1. 知识库统一管理：多源文档解析（PDF/Word/PPT/MD/TXT）、语义分块、向量化持久存储；
2. 工业级双层检索漏斗：
   - 第一层：ChromaDB 向量初筛 (bge-m3 1024维，距离 <= 550.0 截断)
   - 第二层：阿里百炼 gte-rerank-v2 Cross-Attention 深度精排 (得分 >= 0.40 截断)
   - 诚实拒答凭据：全自动过滤无关噪音，杜绝事实幻觉；
3. 对外暴露标准化 OpenAPI 契约，供各种业务 Agent (HR/IT/财务等) 跨进程通过 HTTP 远程调用。
================================================================================
"""

import os
import sys
import shutil
import traceback
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

# 确保导入同级微服务模块
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from vector_store import get_vector_store, UPLOAD_DIR, CHROMA_PERSIST_DIR
from reranker import get_local_reranker, DEFAULT_DISTANCE_THRESHOLD, DEFAULT_RERANK_THRESHOLD
from chat_history_db import get_data_chat_db
from common.logging_config import configure_logging, get_logger, install_trace_middleware
from schemas import (
    SessionCreateRequest,
    SessionTitleUpdateRequest,
    SessionMessageAddRequest,
    DataRetrieveRequest,
)

configure_logging("data-platform")
logger = get_logger("data_platform.app")

app = FastAPI(
    title="企业级统一数据中台微服务 (Data Platform)",
    description="提供知识库资产管理、多路向量检索、云端语义重排与双层漏斗过滤服务",
    version="1.0.0"
)

# 配置 CORS 允许任意业务 Agent 跨域/跨进程调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

install_trace_middleware(
    app,
    "data-platform",
    silence_prefixes=["/api/v1/data/health", "/api/v1/data/documents"],
)

# 挂载静态资源与大屏控制台模板
INDEX_HTML_PATH = os.path.join(CURRENT_DIR, "templates", "index.html")
if os.path.isdir(os.path.join(CURRENT_DIR, "static")):
    app.mount("/static", StaticFiles(directory=os.path.join(CURRENT_DIR, "static")), name="static")

# 挂载 Model Context Protocol (MCP) 工业标准服务端 (基于 SSE 传输协议)
from mcp_server import mcp_server, perform_two_stage_retrieval
app.mount("/mcp", mcp_server.sse_app())



@app.get("/", response_class=HTMLResponse, summary="企业级数据中台专属资产管理大屏")
def root():
    """渲染数据中台专属控制台大屏界面"""
    if os.path.exists(INDEX_HTML_PATH):
        with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return RedirectResponse(url="/docs")


@app.get("/api/v1/data/health", summary="数据中台健康检查与资产摘要")
def health_check():
    """获取数据中台当前存活状态与知识库切片物理统计"""
    try:
        vs = get_vector_store()
        all_docs = vs.list_all_documents()
        total_chunks = vs.collection.count()
        chat_db = get_data_chat_db()
        mem_stats = chat_db.get_stats()
        return {
            "code": 200,
            "status": "healthy",
            "service": "Data Platform Microservice",
            "port": 8001,
            "data": {
                "total_documents": len(all_docs),
                "total_chunks": total_chunks,
                "total_sessions": mem_stats["total_sessions"],
                "total_messages": mem_stats["total_messages"],
                "upload_dir": UPLOAD_DIR,
                "chroma_dir": CHROMA_PERSIST_DIR,
                "documents": [d.get("doc_name") for d in all_docs],
                "mcp_protocol": {
                    "enabled": True,
                    "version": "2.x",
                    "sse_endpoint": "http://127.0.0.1:8001/mcp/sse",
                    "tools": ["list_available_documents", "search_knowledge_base", "read_specific_document"],
                    "resources": ["kb://documents"]
                }
            }
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "error": str(e)})


@app.post("/api/v1/data/retrieve", summary="统一双层漏斗检索与精排接口 (供 Agent 作为 Tool 调用)")
def retrieve_knowledge(req: DataRetrieveRequest):
    """
    【核心中台 API】输入用户 Query，执行两阶段高信噪比漏斗过滤：
    1. 第一阶段初筛：ChromaDB 向量检索 (bge-m3 1024 维)，施加距离截断 (<= distance_threshold)；
    2. 第二阶段精排：阿里百炼 gte-rerank-v2 跨注意力打分，施加得分截断 (>= rerank_threshold)；
    3. 若无合格切片，输出格式化拒答凭据，严防幻觉。
    """
    query = req.query.strip()
    doc_filter = req.doc_name_filter.strip() if req.doc_name_filter else None
    if not query:
        raise HTTPException(status_code=400, detail="检索内容 query 不能为空")

    try:
        res = perform_two_stage_retrieval(
            query=query,
            doc_name_filter=doc_filter,
            top_k=req.top_k,
            top_n=req.top_n,
            distance_threshold=req.distance_threshold,
            rerank_threshold=req.rerank_threshold
        )
        data_payload: dict = {
            "final_chunks": res["final_chunks"],
            "referenced_docs": res["referenced_docs"],
            "is_all_refused": res["is_all_refused"],
            "formatted_text": res["formatted_text"],
            "metrics": res["metrics"],
        }
        if res.get("refusal_reason"):
            data_payload["refusal_reason"] = res["refusal_reason"]

        return {"code": 200, "data": data_payload}

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"code": 500, "message": f"数据中台检索异常: {str(e)}"})


@app.get("/api/v1/data/document-chunks", summary="按文档名定向读取切片内容 (供 Agent 阅读特定文档)")
def read_document_chunks(doc_name: str, max_chunks: int = 6):
    """供 Agent 调用以完整阅读某篇文档的核心章节"""
    doc_name = os.path.basename(doc_name.strip())
    if not doc_name:
        raise HTTPException(status_code=400, detail="文档名不能为空")

    try:
        vs = get_vector_store()
        chunks = vs.get_chunks_by_doc_name(doc_name=doc_name, max_chunks=max_chunks)
        if not chunks:
            return {
                "code": 404,
                "message": f"知识库中未找到文档《{doc_name}》",
                "data": {"chunks": [], "formatted_text": f"未找到名为《{doc_name}》的文档。"}
            }

        snippets = []
        for c in chunks:
            snippets.append(
                f"【切片 {c['chunk_index'] + 1}/{c['total_chunks']}】\n"
                f"{c['text']}\n"
                f"----------------------------------------"
            )
        formatted_text = f"=== 正在精读文档《{doc_name}》前 {len(chunks)} 个章节切片 ===\n\n" + "\n\n".join(snippets)

        return {
            "code": 200,
            "data": {
                "doc_name": doc_name,
                "total_returned": len(chunks),
                "chunks": chunks,
                "formatted_text": formatted_text
            }
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@app.get("/api/v1/data/documents/{doc_name:path}/chunks", summary="获取指定文档的切片数据详情")
def get_document_chunks_rest(doc_name: str):
    """供前端大屏弹窗查看文档切片"""
    safe_name = os.path.basename(doc_name.strip())
    try:
        vs = get_vector_store()
        chunks = vs.get_chunks_by_doc_name(doc_name=safe_name)
        return {
            "code": 200,
            "data": {
                "doc_name": safe_name,
                "total_chunks": len(chunks),
                "chunks": chunks
            }
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@app.get("/api/v1/data/documents", summary="获取知识库中全部入库文档与元数据统计")
def list_documents():
    """获取数据中台所有文档资产列表"""
    try:
        vs = get_vector_store()
        docs = vs.list_all_documents()
        docs = sorted(
            docs,
            key=lambda d: (
                str(d.get("upload_time") or d.get("created_at") or ""),
                str(d.get("doc_name") or d.get("name") or ""),
            ),
            reverse=True,
        )
        return {
            "code": 200,
            "data": {
                "total": len(docs),
                "documents": docs
            }
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@app.post("/api/v1/data/upload", summary="上传新文档并执行解析预检与切片入库")
def upload_document(
    file: UploadFile = File(...),
    clean_mode: str = Form("rule")
):
    """上传文件至数据中台，执行自动化清洗与 Chroma 向量索引构建"""
    allowed_exts = {".docx", ".pdf", ".pptx", ".txt", ".md", ".json", ".csv"}
    raw_filename = (file.filename or "").strip()
    if not raw_filename or ".." in raw_filename:
        raise HTTPException(status_code=400, detail="无效的文件名")
    filename = os.path.basename(raw_filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail=f"不支持的文件格式: {ext}")

    normalized_clean_mode = "llm" if clean_mode == "llm" else "rule"
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    temp_path = os.path.join(UPLOAD_DIR, filename)

    try:
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        vs = get_vector_store()
        res = vs.add_document_file(file_path=temp_path, clean_mode=normalized_clean_mode)
        return {
            "code": 200,
            "message": "文档上传并处理完成",
            "data": res
        }
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@app.delete("/api/v1/data/documents/{doc_name:path}", summary="物理与向量同步下架指定文档")
def delete_document(doc_name: str):
    """物理删除原始文档并清空 ChromaDB 中的对应向量切片"""
    raw_name = (doc_name or "").strip()
    if not raw_name or ".." in raw_name:
        raise HTTPException(status_code=400, detail="无效的文档名称")
    safe_name = os.path.basename(raw_name)
    try:
        vs = get_vector_store()
        success = vs.delete_document(doc_name=safe_name)
        if success:
            return {"code": 200, "message": f"文档《{safe_name}》及其向量切片已彻底清除"}
        else:
            return JSONResponse(status_code=404, content={"code": 404, "message": f"未找到文档《{safe_name}》"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})




# ==============================================================================
# 全局多智能体会话与多轮上下文记忆中枢 (Centralized Memory APIs)
# ==============================================================================

@app.get("/api/v1/data/sessions", summary="获取智能体会话清单 (支持按 agent_id 过滤)")
def list_agent_sessions(agent_id: Optional[str] = None):
    try:
        db = get_data_chat_db()
        sessions = db.list_sessions(agent_id=agent_id)
        return {"code": 200, "data": sessions}
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@app.post("/api/v1/data/sessions", summary="创建新会话")
def create_agent_session(req: Optional[SessionCreateRequest] = None):
    try:
        title = "新对话"
        agent_id = "agentic_rag_core"
        if req:
            if req.title:
                title = req.title.strip() or "新对话"
            if req.agent_id:
                agent_id = req.agent_id.strip() or "agentic_rag_core"

        db = get_data_chat_db()
        sess = db.create_session(title=title, agent_id=agent_id)
        return {"code": 200, "data": sess}
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@app.get("/api/v1/data/sessions/{session_id}", summary="获取会话元数据与全部历史消息")
def get_agent_session(session_id: str):
    try:
        db = get_data_chat_db()
        sess = db.get_session(session_id)
        if not sess:
            return JSONResponse(status_code=404, content={"code": 404, "message": "会话不存在"})
        messages = db.get_session_messages(session_id)
        return {
            "code": 200,
            "data": {
                "session": sess,
                "messages": messages
            }
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@app.put("/api/v1/data/sessions/{session_id}/title", summary="动态更新会话标题")
def update_agent_session_title(session_id: str, req: SessionTitleUpdateRequest):
    try:
        db = get_data_chat_db()
        ok = db.update_session_title(session_id, req.title.strip())
        if ok:
            return {"code": 200, "message": "会话标题更新成功"}
        return JSONResponse(status_code=404, content={"code": 404, "message": "会话未找到"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@app.post("/api/v1/data/sessions/{session_id}/messages", summary="持久化追加会话消息与证据链")
def add_agent_session_message(session_id: str, req: SessionMessageAddRequest):
    try:
        db = get_data_chat_db()
        # 若会话不存在，以指定的 session_id 自动补全会话，避免外键约束异常
        if not db.get_session(session_id):
            title = req.content.strip()[:20] if req.content else "新对话"
            db.create_session(title=title, session_id=session_id)

        msg_id = db.add_message(
            session_id=session_id,
            role=req.role,
            content=req.content,
            steps=req.steps,
            referenced_docs=req.referenced_docs
        )
        return {"code": 200, "data": {"message_id": msg_id}}
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@app.delete("/api/v1/data/sessions/{session_id}", summary="级联物理删除会话及所有消息")
def delete_agent_session(session_id: str):
    try:
        db = get_data_chat_db()
        ok = db.delete_session(session_id)
        if ok:
            return {"code": 200, "message": "会话及其全部消息已彻底删除"}
        return JSONResponse(status_code=404, content={"code": 404, "message": "会话未找到"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 70)
    print("🚀 [数据中台微服务] 正在启动...")
    print("📍 监听地址: http://0.0.0.0:8001")
    print("📚 文档存储: " + UPLOAD_DIR)
    print("🗄️  向量存储: " + CHROMA_PERSIST_DIR)
    print("💡 提示: 当前仅启动了[数据中台]。如需一键启动全套集群(包括8000业务Agent)，请在根目录下执行: python app.py")
    print("=" * 70 + "\n")
    uvicorn.run("app:app", host="0.0.0.0", port=8001, reload=False)
