"""
================================================================================
评测中台微服务模型重导出适配层 (services/eval_platform/models.py)
统一委托至公共模型配置中心 common/models.py，保持 100% 向下兼容。
================================================================================
"""

import os
import sys

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_CURRENT_DIR))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.models import *  # noqa: F401, F403
from common.models import (  # noqa: F401
    ALI_API_KEY_ENV_VAR,
    ALI_BASE_URL,
    ALI_OFFICIAL_BASE_URL,
    ALI_DEFAULT_ANSWER_MODEL,
    ALI_DEFAULT_JUDGE_MODEL,
    DEEPSEEK_API_KEY_ENV_VAR,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
    OPENAI_API_KEY_ENV_VAR,
    OPENAI_BASE_URL,
    OLLAMA_BASE_URL,
    OLLAMA_EMBEDDING_MODEL,
    ACTIVE_LLM_API_KEY_ENV_VAR,
    ACTIVE_LLM_BASE_URL,
    ACTIVE_LLM_MODEL,
    ALI_RERANK_URL_MAIN,
    ALI_RERANK_URL_WORKSPACE,
    ALI_DEFAULT_RERANK_MODEL,
    RERANK_MODEL_NAME,
    DEFAULT_RERANK_MODEL,
    SUPPORTED_MODELS,
    DashScopeRerankerClient,
    get_supported_models_list,
    get_llm,
    get_judge_llm,
    get_answer_llm,
    get_deepseek_llm,
    get_embeddings,
    get_ollama_embeddings,
    get_dashscope_reranker,
    get_reranker,
    get_local_reranker,
)
