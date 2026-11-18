"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
统一模型配置中心与网关工厂 (models.py)

【模块定位】
本文件是系统的"模型中枢"与"统一模型工厂"，集中管理全系统所有模型 API 及其调用凭证：
1. 大语言模型 (LLM):
   - 通用动态路由入口: get_llm(model_name=...)
   - 专职出题与裁判模型: get_judge_llm() -> 严格锁定 qwen3.8-max-0902 (阿里百炼逻辑旗舰)
   - 专职考生与问答模型: get_answer_llm() -> 严格锁定 qwen3.7-plus (阿里百炼主力推理)
   - 兼容别名: get_deepseek_llm (保持历史调用 100% 零修改)
2. 文本嵌入模型 (Embedding):
   - 统一入口: get_embeddings() / get_ollama_embeddings() -> 本地 Ollama bge-m3 (1024维初筛)
3. 语义精排重排模型 (Reranker):
   - 统一入口: get_reranker() / get_local_reranker() -> 阿里百炼云端 gte-rerank-v2
4. 多平台 API 注册中心 (阿里百炼 / DeepSeek / OpenAI / Ollama 等一键插拔热切换)
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
ALI_DEFAULT_ANSWER_MODEL: str = "qwen3.8-flash"  # 考生系统与默认作答模型
ALI_DEFAULT_JUDGE_MODEL: str = "qwen3.8-max-0902"   # 出题专家与量化裁判模型

# 2. DeepSeek 配置 (兼容独立中转/AutoDL 算力机)
DEEPSEEK_API_KEY_ENV_VAR: str = "Auto_aip_key"
DEEPSEEK_BASE_URL: str = "https://www.autodl.art/api/v1"
DEEPSEEK_DEFAULT_MODEL: str = "deepseek-v4-pro-0813"

# 3. OpenAI 官方配置 (备用)
OPENAI_API_KEY_ENV_VAR: str = "OPENAI_API_KEY"
OPENAI_BASE_URL: str = "https://api.openai.com/v1"

# 4. Ollama 本地服务配置
OLLAMA_BASE_URL: str = "http://localhost:11434"
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
    # 阿里百炼主力问答模型 (考生系统与通用对话)
    "qwen3.8-flash": {
        "id": "qwen3.8-flash",
        "name": "通义千问 3.8-flash",
        "short_name": "3.8-flash",
        "desc": "全方位主力 · 极速流利回答",
        "base_url": ALI_BASE_URL,
        "key_env": ALI_API_KEY_ENV_VAR,
        "provider": "阿里百炼"
    },
    # 阿里百炼旗舰逻辑模型 (命题专家与 Ragas 裁判)
    "qwen3.8-max-0902": {
        "id": "qwen3.8-max-0902",
        "name": "通义千问 3.8 Max",
        "short_name": "3.8 Max",
        "desc": "顶级逻辑旗舰 · 深度多跳推理",
        "base_url": ALI_BASE_URL,
        "key_env": ALI_API_KEY_ENV_VAR,
        "provider": "阿里百炼"
    },
    # DeepSeek 深度推理旗舰
    "deepseek-v4-pro-0813": {
        "id": "deepseek-v4-pro-0813",
        "name": "DeepSeek V4 Pro",
        "short_name": "DeepSeek V4 Pro",
        "desc": "高智商逻辑旗舰 · 深度推理作答",
        "base_url": ALI_BASE_URL,
        "key_env": ALI_API_KEY_ENV_VAR,
        "provider": "阿里百炼"
    },
    # DeepSeek 极速客观模型
    "deepseek-v4-flash-0731": {
        "id": "deepseek-v4-flash-0731",
        "name": "DeepSeek V4 Flash",
        "short_name": "DeepSeek V4 Flash",
        "desc": "极速客观打分 · 阿里百炼平台提供",
        "base_url": ALI_BASE_URL,
        "key_env": ALI_API_KEY_ENV_VAR,
        "provider": "阿里百炼"
    }
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
            "is_configured": bool(key_val and key_val.strip())
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

    # 规范化别名对齐 (如历史旧标识或简称映射)
    canonical_model = target_model
    if target_model.lower() in ["glm-5.3", "glm5.3", "glm-5.3-flash", "glm-4", "glm"]:
        canonical_model = "deepseek-v4-pro-0813"
    elif target_model.lower() in ["ds-pro", "deepseek-pro", "deepseek-v4-pro"]:
        canonical_model = "deepseek-v4-pro-0813"
    elif target_model.lower() in ["qwen3.7-plus", "qwen3.7", "3.7-plus", "3.7plus", "qwen-3.7-plus"]:
        # 兼容旧考生模型调用：自动无感重定向至当前激活的考生主力作答模型 (如 qwen3.8-flash)
        canonical_model = ALI_DEFAULT_ANSWER_MODEL
    else:
        for k in SUPPORTED_MODELS:
            if k.lower() == target_model.lower() or target_model.lower() in [k.lower() + "-flash", k.lower().replace("-flash", "")]:
                canonical_model = k
                break

    # 动态匹配对应的 Base URL 与 环境变量 API Key
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
    # 路由信息属于调试细节；默认不刷屏，需要排查时打开 DEBUG 日志即可。
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
    """
    【业务专职获取器：出题专家 & 独立反思裁判】
    必须且仅返回阿里百炼旗舰顶级逻辑模型 qwen3.8-max-0902，保障量化评测与原子反思的极高严谨性。
    """
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
    """
    【业务专职获取器：考生作答系统】
    必须且仅返回阿里百炼主力问答模型 qwen3.7-plus，保障批量作答的高并发与推理稳定性。
    """
    return get_llm(
        model=ALI_DEFAULT_ANSWER_MODEL,
        temperature=temperature,
        streaming=streaming,
        **kwargs
    )


# 100% 向后兼容别名：现有其他业务代码调用 get_deepseek_llm 无需任何改动
get_deepseek_llm = get_llm


# ==============================================================================
# 四、 文本嵌入向量 (Embedding) 工厂
# ==============================================================================

def get_embeddings(
    model: str = OLLAMA_EMBEDDING_MODEL,
    base_url: str = OLLAMA_BASE_URL
) -> Embeddings:
    """
    【统一文本嵌入工厂入口】
    获取 Ollama 本地部署的稠密文本嵌入模型 (默认 1024 维 Dense 向量 bge-m3)
    """
    return OllamaEmbeddings(
        model=model,
        base_url=base_url
    )


# 100% 向后兼容别名：现有代码调用 get_ollama_embeddings 无需任何改动
get_ollama_embeddings = get_embeddings


# ==============================================================================
# 五、 语义精排重排 (Reranker) 统一客户端与工厂
# ==============================================================================

class DashScopeRerankerClient:
    """
    【阿里百炼云端语义精排重排客户端】
    直连阿里百炼官方顶尖 Cross-Attention 重排模型 gte-rerank-v2。
    与通义千问大模型共享同一个 DASHSCOPE_API_KEY。
    集成公网官方端点与北京 MaaS 专区端点双重保活与容灾。
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = ALI_DEFAULT_RERANK_MODEL
    ):
        self.api_key = api_key or os.getenv(ALI_API_KEY_ENV_VAR, "")
        self.model = model
        self.endpoints = [ALI_RERANK_URL_MAIN, ALI_RERANK_URL_WORKSPACE]

    def compute_relevance_scores(self, query: str, texts: List[str]) -> List[float]:
        """
        调用阿里百炼 gte-rerank-v2 计算 Query 与候选文本的高精度 Cross-Attention 语义相关度得分 (0.0 ~ 1.0)
        """
        if not texts:
            return []
        if not self.api_key:
            print(f"⚠️ [百炼重排警告] 未检测到环境变量 {ALI_API_KEY_ENV_VAR}，无法调用云端重排！")
            return [0.0] * len(texts)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "input": {
                "query": query,
                "documents": texts
            },
            "parameters": {
                "return_documents": False,
                "top_n": len(texts)
            }
        }

        for target_url in self.endpoints:
            try:
                resp = requests.post(target_url, headers=headers, json=payload, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get("output", {}).get("results", [])
                    score_map = {item["index"]: round(float(item["relevance_score"]), 4) for item in results}
                    return [score_map.get(i, 0.0) for i in range(len(texts))]
                else:
                    err_msg = resp.text[:120]
                    if "quota" in err_msg.lower() or resp.status_code == 403:
                        print("⚠️ [百炼重排 403] gte-rerank-v2 免费额度已耗尽或鉴权失败，请检查百炼账户")
                    else:
                        print(f"⚠️ [百炼重排] 端点 {target_url} 响应状态码: {resp.status_code}")
            except Exception as e:
                print(f"⚠️ [百炼重排请求超时/异常] 端点 {target_url}: {e}")

        # 若端点不可达，返回 0.0 保障后续链路不报错崩溃
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
    集中获取具备业务双层阈值截断 (Top-10 <= 550.0 + Top-3 >= 0.40) 的重排漏斗引擎单例。
    业务模块可直接从 models.py 一站式导入使用。
    """
    from reranker import get_local_reranker
    return get_local_reranker()


# 100% 向后兼容别名
get_local_reranker = get_reranker


# ==============================================================================
# 六、 终端自检测试入口
# ==============================================================================

if __name__ == "__main__":
    print(f"\n{'='*65}")
    print("🚀 [Agentic RAG 统一模型中枢网关自检]")
    print(f" 🤖 考生默认模型: 【{ALI_DEFAULT_ANSWER_MODEL}】 (阿里百炼)")
    print(f" ⚖️ 裁判专职模型: 【{ALI_DEFAULT_JUDGE_MODEL}】 (阿里百炼顶级逻辑)")
    print(f" ☁️ 云端重排引擎: 【{RERANK_MODEL_NAME}】 (阿里百炼 Cross-Attention)")
    print(f" 🧬 本地向量模型: 【{OLLAMA_EMBEDDING_MODEL}】 (本地 Ollama 1024维)")
    print(f" 🔑 百炼环境变量: {ALI_API_KEY_ENV_VAR} -> 状态: {'已配置' if os.getenv(ALI_API_KEY_ENV_VAR) else '未配置'}")
    print(f"{'='*65}\n")

    try:
        llm = get_llm()
        resp = llm.invoke("你好，请简要说明你的模型身份与核心优势。")
        print(f"💬 模型回复:\n{resp.content}\n")
    except Exception as e:
        print(f"⚠️ 测试模型调用异常: {e}")
