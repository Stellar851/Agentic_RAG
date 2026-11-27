"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
FastAPI Web 核心服务总装配入口 (app.py)

【模块定位】
本文件是系统的 Web 总入口，采用 FastAPI 工业级微架构组织：
1. 负责 Uvicorn ASGI 服务初始化、CORS 跨域与静态目录挂载；
2. 负责前端主交互页面 (/) 与评测工作台 (/benchmark) 模板渲染；
3. 负责全局健康自检 (/api/status) 与支持模型列表 (/api/models)；
4. 挂载子路由模块：
   - routers/knowledge.py : 知识库文档上传、清洗预检、切片与删除
   - routers/session.py   : 会话历史生命周期管理
   - routers/chat.py      : Agentic RAG SSE 流式打字机与模型热切换
   - routers/benchmark.py : 工业级五阶段全流程评测流水线
================================================================================
"""

import os
import sys
import logging
import functools

# 确保所有 print 自动强制刷新，彻底消除 Windows 下控制台日志缓冲延迟
print = functools.partial(print, flush=True)

# 确保 Windows 终端输出 UTF-8 编码，防止 emoji 或特殊字符在 GBK 环境下抛出 UnicodeEncodeError
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 将当前 Agentic_RAG 目录置入 sys.path 首位，消除跨目录导入报红
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# 导入业务模型与子路由
from models import (
    ACTIVE_LLM_API_KEY_ENV_VAR,
    ACTIVE_LLM_MODEL,
    get_ollama_embeddings,
    get_supported_models_list,
    SUPPORTED_MODELS
)
from common.logging_config import configure_logging, get_logger, install_trace_middleware
# 已解耦：移除本地 vector_store 引用
from routers import session, chat

# 配置静默过滤器：过滤掉前端高频轮询的静默接口，避免控制台被无意义刷屏
class EndpointSilenceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        silence_endpoints = [
            "/api/status",
            "/api/sessions",
            "/api/documents",
            "/api/models",
            "/api/benchmark/",
            "/api/eval/"
        ]
        return not any(endpoint in msg for endpoint in silence_endpoints)

logging.getLogger("uvicorn.access").addFilter(EndpointSilenceFilter())

# ==============================================================================
# 一、 FastAPI 实例初始化与中间件配置
# ==============================================================================

configure_logging("agent-service")
logger = get_logger("agent.app")

app = FastAPI(
    title="Agentic RAG 企业智能关联知识库系统",
    description="基于 FastAPI + LangChain Tool-Calling + ChromaDB 构建的高性能企业级智能检索与多跳推理中枢",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# 允许跨域请求 (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

install_trace_middleware(
    app,
    "agent-service",
    silence_prefixes=["/api/status", "/api/sessions", "/api/documents", "/api/models"],
)

DATA_PLATFORM_URL = os.getenv("DATA_PLATFORM_URL", "http://127.0.0.1:8001")
EVAL_PLATFORM_URL = os.getenv("EVAL_PLATFORM_URL", "http://localhost:8002")

# 挂载静态资源目录
STATIC_DIR = os.path.join(CURRENT_DIR, "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

TEMPLATES_DIR = os.path.join(CURRENT_DIR, "templates")
INDEX_HTML_PATH = os.path.join(TEMPLATES_DIR, "index.html")
FRONTEND_DIST_DIR = os.path.join(PROJECT_ROOT, "frontend", "dist")
FRONTEND_ASSETS_DIR = os.path.join(FRONTEND_DIST_DIR, "assets")
FRONTEND_INDEX_HTML = os.path.join(FRONTEND_DIST_DIR, "index.html")

if os.path.isdir(FRONTEND_ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=FRONTEND_ASSETS_DIR), name="frontend_assets")


def _serve_frontend_entry():
    target_path = FRONTEND_INDEX_HTML if os.path.exists(FRONTEND_INDEX_HTML) else INDEX_HTML_PATH
    if not os.path.exists(target_path):
        raise HTTPException(status_code=404, detail="前端页面入口文件未找到")
    with open(target_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)


# ==============================================================================
# 二、 基础页面与系统状态自检路由
# ==============================================================================

@app.get("/", response_class=HTMLResponse, summary="渲染前端交互主页面")
@app.get("/chat", response_class=HTMLResponse, summary="渲染统一前端智能问答视图")
@app.get("/knowledge", response_class=HTMLResponse, summary="渲染统一前端知识中台视图")
@app.get("/eval", response_class=HTMLResponse, summary="渲染统一前端评测中枢视图")
def index():
    """优先返回 Vue 3 构建产物，未构建时自动回退至原版模板"""
    return _serve_frontend_entry()


@app.get("/legacy", response_class=HTMLResponse, summary="渲染原版前端页面")
def legacy_index():
    """保留原版原生页面访问入口"""
    if not os.path.exists(INDEX_HTML_PATH):
        raise HTTPException(status_code=404, detail="前端模板 index.html 未找到")
    with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)


@app.get("/benchmark", summary="跳转至独立工业级评测中台微服务大屏")
def benchmark_page():
    """将请求优雅重定向至独立的评测中台服务大屏 (Port 8002)"""
    return RedirectResponse(url=f"{EVAL_PLATFORM_URL.rstrip('/')}/", status_code=302)


@app.get("/api/models", summary="获取系统支持的热切换模型列表")
def get_models():
    """
    【API】获取当前系统已配置支持的全部大模型列表及其配置状态
    供前端顶栏下拉菜单动态渲染与热切换
    """
    models_list = get_supported_models_list()
    return {
        "code": 200,
        "status": "success",
        "data": {
            "default_model": ACTIVE_LLM_MODEL,
            "models": models_list
        }
    }


@app.get("/api/status", summary="获取后端各组件健康自检状态")
def get_system_status():
    """
    【API】获取后端各组件（主大模型 API、Ollama 本地服务、数据中台）健康状态
    """
    try:
        # 1. 检查当前主大模型 API 状态
        current_cfg = SUPPORTED_MODELS.get(ACTIVE_LLM_MODEL, {})
        env_var = current_cfg.get("key_env", ACTIVE_LLM_API_KEY_ENV_VAR)
        key = os.getenv(env_var, "")

        llm_status = {
            "connected": False,
            "message": "",
            "model_name": ACTIVE_LLM_MODEL,
            "provider": current_cfg.get("provider", "通义千问")
        }
        if key and key.strip():
            masked = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "***"
            llm_status["connected"] = True
            llm_status["message"] = f"密钥读取成功 ({masked})"
        else:
            llm_status["message"] = f"未找到环境变量 `{env_var}`"

        # 2. 检查 Ollama 服务与嵌入向量模型
        ollama_status = {"connected": False, "message": ""}
        try:
            emb = get_ollama_embeddings()
            vec = emb.embed_query("健康检查")
            ollama_status = {"connected": True, "message": f"连接成功！向量维度: {len(vec)}"}
        except Exception as e:
            ollama_status["message"] = f"Ollama 服务异常: {str(e)}"

        # 3. 通过 HTTP 探查数据中台
        data_platform_status = {"connected": False, "documents_count": 0, "total_chunks": 0}
        try:
            import urllib.request, json
            req = urllib.request.Request(
                f"{DATA_PLATFORM_URL.rstrip('/')}/api/v1/data/health",
                headers={"User-Agent": "AgentApp"}
            )
            with urllib.request.urlopen(req, timeout=2) as res:
                if res.status == 200:
                    dp_data = json.loads(res.read()).get("data", {})
                    data_platform_status["connected"] = True
                    data_platform_status["documents_count"] = dp_data.get("total_documents", 0)
                    data_platform_status["total_chunks"] = dp_data.get("total_chunks", 0)
        except Exception as e:
            data_platform_status["message"] = str(e)

        return {
            "code": 200,
            "status": "success",
            "data": {
                "deepseek": llm_status,
                "ollama": ollama_status,
                "data_platform": data_platform_status,
                "documents_count": data_platform_status["documents_count"],
                "total_chunks": data_platform_status["total_chunks"]
            }
        }
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "code": 500,
                "status": "error",
                "message": f"系统自检发生异常: {str(e)}"
            }
        )

# ==============================================================================
# 三、 挂载各业务解耦子路由
# ==============================================================================

# 已解耦：文档上传管理收敛至数据中台 (Port 8001)
app.include_router(session.router)
app.include_router(chat.router)

# ==============================================================================
# 四、 服务入口 (Uvicorn ASGI 驱动)
# ==============================================================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print("=" * 70)
    print("🚀 FastAPI Agentic RAG 高性能异步智能知识库系统正在启动...")
    print(f"🌐 Web 页面访问地址: http://127.0.0.1:{port}")
    print(f"📚 OpenAPI 交互文档: http://127.0.0.1:{port}/docs")
    print(f"📑 Redoc API 说明档: http://127.0.0.1:{port}/redoc")
    print("=" * 70)
    uvicorn.run(app, host="0.0.0.0", port=port)
