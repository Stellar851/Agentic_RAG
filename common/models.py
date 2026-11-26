"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
全局统一模型配置中心与网关工厂 (common/models.py)

【模块定位】
作为三大微服务的单一模型事实源，集中管理所有模型 API、注册表与工厂入口：
1. 大语言模型 (LLM):
   - 通用动态路由入口: get_llm(model_name=...)
   - 专职出题与裁判模型: get_judge_llm() -> 严格锁定 qwen3.8-max-0902 (阿里百炼逻辑旗舰)
   - 专职考生与问答模型: get_answer_llm() -> 严格锁定 deepseek-v4.1-flash (兼容旧别名 deepseek-v4-flash-0731 / qwen3.7-plus)
   - 兼容别名: get_deepseek_llm
2. 文本嵌入模型 (Embedding):
   - 统一入口: get_embeddings() / get_ollama_embeddings() -> 本地 Ollama bge-m3 (1024 维初筛)
3. 语义精排重排模型 (Reranker):
   - 统一入口: get_dashscope_reranker() -> 阿里百炼云端 gte-rerank-v2
   - 双层漏斗单例入口: get_reranker() / get_local_reranker()
================================================================================
"""

import os
import sys
import logging
import warnings
warnings.filterwarnings("ignore")
import requests
from typing import Optional, Dict, Any, List

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from langchain_openai import ChatOpenAI
from langchain_community.embeddings import OllamaEmbeddings
from langchain_core.embeddings import Embeddings

logger = logging.getLogger(__name__)


# ==============================================================================
# 一、 多供应商 API 基础配置与凭证常量
# ==============================================================================

# 1. 阿里百炼 (DashScope) 官方配置
ALI_API_KEY_ENV_VAR: str = "DASHSCOPE_API_KEY"
ALI_BASE_URL: str = "https://ws-kcf6twfk4vfy5l1e.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
ALI_OFFICIAL_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
ALI_DEFAULT_ANSWER_MODEL: str = "deepseek-v4.1-flash"  # 考生系统与默认作答模型
ALI_DEFAULT_JUDGE_MODEL: str = "qwen3.8-max-0902"  # 出题专家与量化裁判模型

# 2. DeepSeek 配置 (兼容独立中转/AutoDL 算力机)
DEEPSEEK_API_KEY_ENV_VAR: str = "Auto_aip_key"
DEEPSEEK_BASE_URL: str = "https://www.autodl.art/api/v1"
DEEPSEEK_DEFAULT_MODEL: str = "deepseek-v4-pro-0813"

# 3. OpenAI 官方配置 (备用)
OPENAI_API_KEY_ENV_VAR: str = "OPENAI_API_KEY"
OPENAI_BASE_URL: str = "https://api.openai.com/v1"

# 4. Ollama 本地服务配置
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_EMBEDDING_MODEL: str = "bge-m3"

# 5. 当前全局激活的默认主模型配置
ACTIVE_LLM_API_KEY_ENV_VAR: str = ALI_API_KEY_ENV_VAR
ACTIVE_LLM_BASE_URL: str = ALI_BASE_URL
ACTIVE_LLM_MODEL: str = ALI_DEFAULT_ANSWER_MODEL

# 6. 云端语义精排重排模型配置 (阿里百炼 DashScope 官方服务)
ALI_RERANK_URL_MAIN: str = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
ALI_RERANK_URL_WORKSPACE: str = "https://ws-kcf6twfk4vfy5l1e.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
ALI_DEFAULT_RERANK_MODEL: str = "gte-rerank-v2"
RERANK_MODEL_NAME: str = ALI_DEFAULT_RERANK_MODEL
DEFAULT_RERANK_MODEL: str = ALI_DEFAULT_RERANK_MODEL


# ==============================================================================
# 二、 全局模型注册表 (多模型热切换中心)
# ==============================================================================

SUPPORTED_MODELS: Dict[str, Dict[str, Any]] = {
    "deepseek-v4.1-flash": {
        "id": "deepseek-v4.1-flash",
        "name": "DeepSeek V4.1 Flash",
        "short_name": "DeepSeek V4.1 Flash",
        "desc": "极速推理主力 · 阿里百炼平台提供",
        "base_url": ALI_BASE_URL,
        "key_env": ALI_API_KEY_ENV_VAR,
        "provider": "阿里百炼",
    },
    "qwen3.8-flash": {
        "id": "qwen3.8-flash",
        "name": "通义千问 3.8-flash",
        "short_name": "3.8-flash",
        "desc": "全方位主力 · 极速流利回答",
        "base_url": ALI_BASE_URL,
        "key_env": ALI_API_KEY_ENV_VAR,
        "provider": "阿里百炼",
    },
    "qwen3.8-max-0902": {
        "id": "qwen3.8-max-0902",
        "name": "通义千问 3.8 Max",
        "short_name": "3.8 Max",
        "desc": "顶级逻辑旗舰 · 深度多跳推理",
        "base_url": ALI_BASE_URL,
        "key_env": ALI_API_KEY_ENV_VAR,
        "provider": "阿里百炼",
    },
    "deepseek-v4-pro-0813": {
        "id": "deepseek-v4-pro-0813",
        "name": "DeepSeek V4 Pro",
        "short_name": "DeepSeek V4 Pro",
        "desc": "高智商逻辑旗舰 · 深度推理作答",
        "base_url": ALI_BASE_URL,
        "key_env": ALI_API_KEY_ENV_VAR,
        "provider": "阿里百炼",
    },
}


def get_supported_models_list() -> List[Dict[str, Any]]:
    """获取所有受支持的模型元数据列表（供前端动态下拉框与状态卡片渲染）"""
    res = []
    for k, v in SUPPORTED_MODELS.items():
        key_val = os.getenv(v.get("key_env", ""), "")
        res.append({
            "id": v.get("id", k),
            "model_name": k,
            "name": v.get("name", k),
            "short_name": v.get("short_name", k),
            "desc": v.get("desc", ""),
            "provider": v.get("provider", ""),
            "key_env": v.get("key_env", ""),
            "is_configured": bool(key_val and key_val.strip()),
        })
    return res


# ==============================================================================
# 三、 大语言模型 (LLM) 统一工厂
# ==============================================================================

def get_llm(
    model: Optional[str] = None,
    temperature: float = 0,
    streaming: bool = True,
    **kwargs
) -> ChatOpenAI:
    """
    【统一大语言模型工厂入口】
    动态根据请求的 model 名称从注册表匹配 base_url 和 api_key，支持前端无感热切换与多供应商适配。
    """
    target_model = (model or "").strip() or ACTIVE_LLM_MODEL

    canonical_model = target_model
    if target_model.lower() in ["glm-5.3", "glm5.3", "glm-5.3-flash", "glm-4", "glm"]:
        canonical_model = "deepseek-v4-pro-0813"
    elif target_model.lower() in ["ds-pro", "deepseek-pro", "deepseek-v4-pro"]:
        canonical_model = "deepseek-v4-pro-0813"
    elif target_model.lower() in ["deepseek-v4-flash-0731", "deepseek-v4-flash", "ds-flash", "deepseek-flash"]:
        canonical_model = "deepseek-v4.1-flash"
    elif target_model.lower() in ["qwen3.7-plus", "qwen3.7", "3.7-plus", "3.7plus", "qwen-3.7-plus"]:
        canonical_model = ALI_DEFAULT_ANSWER_MODEL
    else:
        for k in SUPPORTED_MODELS:
            if k.lower() == target_model.lower() or target_model.lower() in [k.lower() + "-flash", k.lower().replace("-flash", "")]:
                canonical_model = k
                break

    if canonical_model in SUPPORTED_MODELS:
        cfg = SUPPORTED_MODELS[canonical_model]
        base_url = cfg["base_url"]
        key_env = cfg["key_env"]
        actual_model_name = canonical_model
    else:
        base_url = ACTIVE_LLM_BASE_URL
        key_env = ACTIVE_LLM_API_KEY_ENV_VAR
        actual_model_name = target_model

    api_key = os.getenv(key_env, "")
    logger.debug("LLM 路由就绪: model=%s base_url=%s", actual_model_name, base_url)

    return ChatOpenAI(
        model=actual_model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        streaming=streaming,
        extra_body={},
        **kwargs
    )


def get_judge_llm(
    temperature: float = 0,
    streaming: bool = False,
    **kwargs
) -> ChatOpenAI:
    """出题专家 & 独立反思裁判模型获取器（锁定 qwen3.8-max-0902）。"""
    return get_llm(
        model=ALI_DEFAULT_JUDGE_MODEL,
        temperature=temperature,
        streaming=streaming,
        **kwargs
    )


def get_answer_llm(
    temperature: float = 0,
    streaming: bool = True,
    **kwargs
) -> ChatOpenAI:
    """考生作答模型获取器（锁定当前激活作答模型 deepseek-v4.1-flash）。"""
    return get_llm(
        model=ALI_DEFAULT_ANSWER_MODEL,
        temperature=temperature,
        streaming=streaming,
        **kwargs
    )


# 100% 向后兼容别名
get_deepseek_llm = get_llm


# ==============================================================================
# 四、 文本嵌入向量 (Embedding) 工厂
# ==============================================================================

def get_embeddings(
    model: str = OLLAMA_EMBEDDING_MODEL,
    base_url: str = OLLAMA_BASE_URL
) -> Embeddings:
    """获取 Ollama 本地部署的稠密文本嵌入模型 (默认 1024 维 Dense 向量 bge-m3)。"""
    return OllamaEmbeddings(
        model=model,
        base_url=base_url
    )


get_ollama_embeddings = get_embeddings


# ==============================================================================
# 五、 语义精排重排 (Reranker) 统一客户端与工厂
# ==============================================================================

class DashScopeRerankerClient:
    """阿里百炼云端语义精排重排客户端 (gte-rerank-v2)，集成公网与北京 MaaS 端点容灾。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = ALI_DEFAULT_RERANK_MODEL
    ):
        self.api_key = api_key or os.getenv(ALI_API_KEY_ENV_VAR, "")
        self.model = model
        self.endpoints = [ALI_RERANK_URL_MAIN, ALI_RERANK_URL_WORKSPACE]

    def compute_relevance_scores(self, query: str, texts: List[str]) -> List[float]:
        """调用阿里百炼 gte-rerank-v2 计算 Query 与候选文本的语义相关度得分 (0.0 - 1.0)。"""
        if not texts:
            return []
        api_key = self.api_key or os.getenv(ALI_API_KEY_ENV_VAR, "")
        if not api_key:
            print(f"⚠️ [百炼重排警告] 未检测到环境变量 {ALI_API_KEY_ENV_VAR}，无法调用云端重排！")
            return [0.0] * len(texts)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": {
                "query": query,
                "documents": texts,
            },
            "parameters": {
                "return_documents": False,
                "top_n": len(texts),
            },
        }

        for target_url in self.endpoints:
            try:
                resp = requests.post(target_url, headers=headers, json=payload, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get("output", {}).get("results", [])
                    score_map = {item["index"]: round(float(item["relevance_score"]), 4) for item in results}
                    return [score_map.get(i, 0.0) for i in range(len(texts))]
                err_msg = resp.text[:120]
                if "quota" in err_msg.lower() or resp.status_code == 403:
                    print("⚠️ [百炼重排 403] gte-rerank-v2 免费额度已耗尽或鉴权失败，请检查百炼账户")
                else:
                    print(f"⚠️ [百炼重排] 端点 {target_url} 响应状态码: {resp.status_code}")
            except Exception as e:
                print(f"⚠️ [百炼重排请求超时/异常] 端点 {target_url}: {e}")

        return [0.0] * len(texts)


_global_dashscope_reranker: Optional[DashScopeRerankerClient] = None


def get_dashscope_reranker(
    api_key: Optional[str] = None,
    model: str = ALI_DEFAULT_RERANK_MODEL
) -> DashScopeRerankerClient:
    """获取阿里百炼 gte-rerank-v2 云端重排客户端单例"""
    global _global_dashscope_reranker
    if _global_dashscope_reranker is None:
        _global_dashscope_reranker = DashScopeRerankerClient(api_key=api_key, model=model)
    return _global_dashscope_reranker


def get_reranker():
    """
    【统一语义重排工厂入口】
    获取具备双层阈值截断 (Top-10 <= 550.0 + Top-3 >= 0.40) 的重排漏斗引擎单例。
    兼容数据中台内部直导与跨目录导入。
    """
    try:
        from reranker import get_local_reranker as _get_engine
    except ImportError:
        from services.data_platform.reranker import get_local_reranker as _get_engine
    return _get_engine()


get_local_reranker = get_reranker
