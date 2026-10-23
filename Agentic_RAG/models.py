"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
模型管理与配置模块 (models.py)

【模块定位】
本文件是系统的"配置中心"与"模型工厂"，统一管理：
1. 语言大模型 (LLM): DeepSeek 大模型的 API 地址、模型名、密钥环境变量名
2. 文本向量模型 (Embedding): 本地 Ollama bge-m3 的服务地址与模型名
================================================================================
"""

import os
from typing import Optional, Dict, Any, List
from langchain_openai import ChatOpenAI
from langchain_community.embeddings import OllamaEmbeddings
from langchain_core.embeddings import Embeddings


# ==============================================================================
# 一、 常量定义与配置参数
# ==============================================================================

# DeepSeek 配置
DEEPSEEK_API_KEY_ENV_VAR: str = "Auto_aip_key"
DEEPSEEK_BASE_URL: str = "https://www.autodl.art/api/v1"
DEEPSEEK_DEFAULT_MODEL: str = "GLM-5.3-flash"

#阿里千问
ALI_API_KEY_ENV_VAR: str = "DASHSCOPE_API_KEY"
ALI_BASE_URL: str = "https://ws-kcf6twfk4vfy5l1e.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
ALI_DEFAULT_MODEL: str = "qwen3.7-plus"

# 当前激活生效的默认主模型配置
ACTIVE_LLM_API_KEY_ENV_VAR: str = ALI_API_KEY_ENV_VAR
ACTIVE_LLM_BASE_URL: str = ALI_BASE_URL
ACTIVE_LLM_MODEL: str = ALI_DEFAULT_MODEL

# 注册支持的前端热切换模型列表（包含展示名、接口地址与密钥环境变量）
SUPPORTED_MODELS: Dict[str, Dict[str, Any]] = {
    "qwen3.7-plus": {
        "id": "qwen3.7-plus",
        "name": "通义千问 3.7 Plus",
        "short_name": "3.7 Plus",
        "desc": "全方位主力 · 极速流利回答",
        "base_url": ALI_BASE_URL,
        "key_env": ALI_API_KEY_ENV_VAR,
        "provider": "阿里百炼"
    },
    "qwen3.8-max": {
        "id": "qwen3.8-max",
        "name": "通义千问 3.8 Max",
        "short_name": "3.8 Max",
        "desc": "顶级逻辑旗舰 · 深度多跳推理",
        "base_url": ALI_BASE_URL,
        "key_env": ALI_API_KEY_ENV_VAR,
        "provider": "阿里百炼"
    },
    "glm-5.3": {
        "id": "glm-5.3",
        "name": "智谱 GLM-5.3",
        "short_name": "GLM 5.3",
        "desc": "极速推理 · 阿里云免费额度托管",
        "base_url": ALI_BASE_URL,
        "key_env": ALI_API_KEY_ENV_VAR,
        "provider": "阿里百炼"
    }
}


def get_supported_models_list() -> List[Dict[str, Any]]:
    """获取所有受支持的模型元数据列表（供前端动态下拉框与Gemini卡片渲染）"""
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


# Ollama 本地向量服务配置
OLLAMA_BASE_URL: str = "http://localhost:11434"
OLLAMA_EMBEDDING_MODEL: str = "bge-m3"


# ==============================================================================
# 二、 大语言模型 (LLM) 客户端工厂
# ==============================================================================

def get_deepseek_llm(
    model: Optional[str] = None,
    temperature: float = 0,
    streaming: bool = True,
    **kwargs
) -> ChatOpenAI:
    """
    创建并返回指定或当前默认配置的 LLM 客户端。
    支持传入 model 动态从注册表中匹配其 base_url 与 api_key，支持前端无感热切换。
    """
    target_model = (model or "").strip() or ACTIVE_LLM_MODEL
    
    # 智能别名与大小写容错映射 (例如用户缓存的旧值 GLM-5.3-flash 自动对齐到 glm-5.3)
    canonical_model = target_model
    for k in SUPPORTED_MODELS:
        if k.lower() == target_model.lower() or target_model.lower() in [k.lower() + "-flash", k.lower().replace("-flash", "")]:
            canonical_model = k
            break

    # 动态匹配注册表中的 Base URL 和 API Key
    if canonical_model in SUPPORTED_MODELS:
        cfg = SUPPORTED_MODELS[canonical_model]
        base_url = cfg["base_url"]
        key_env = cfg["key_env"]
        actual_model_name = canonical_model
    else:
        base_url = ACTIVE_LLM_BASE_URL
        key_env = ACTIVE_LLM_API_KEY_ENV_VAR
        actual_model_name = target_model

    api_key = os.getenv(key_env)
    print(f"🤖 [LLM 动态路由] 已连接模型: 【{actual_model_name}】 | Base URL: {base_url}")

    return ChatOpenAI(
        model=actual_model_name,
        api_key=api_key,
        base_url=base_url,    
        temperature=temperature, 
        streaming=streaming, 
        extra_body={}, 
        **kwargs
    )


# ==============================================================================
# 三、 本地向量模型 (Embedding) 客户端工厂
# ==============================================================================

def get_ollama_embeddings(
    model: str = OLLAMA_EMBEDDING_MODEL,
    base_url: str = OLLAMA_BASE_URL
) -> Embeddings:
    """
    【功能】获取 Ollama 本地部署的文本嵌入模型 (默认 bge-m3)

    【注意事项】
        使用前需运行 `ollama pull bge-m3` 下载模型，并确保 Ollama 服务处于运行状态。
    """
    return OllamaEmbeddings(
        model=model,
        base_url=base_url
    )


# ==============================================================================
# 四、 终端交互测试入口 (直接运行 python models.py 即可与当前模型对话)
# ==============================================================================

if __name__ == "__main__":
    print(f"\n{'='*55}")
    print(f" [模型直连测试] 正在连接当前配置的模型: 【{ACTIVE_LLM_MODEL}】")
    print(f" 接口地址 (Base URL): {ACTIVE_LLM_BASE_URL}")
    print(f" 环境变量: {ACTIVE_LLM_API_KEY_ENV_VAR}")
    print(f" 输入问题直接对话测试，输入 'q' 或 'exit' 退出")
    print(f"{'='*55}\n")

    try:
        llm = get_deepseek_llm()
        while True:
            user_input = input("👤 你: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ["q", "exit", "quit"]:
                print("👋 已退出测试。")
                break

            print(f"🤖 {ACTIVE_LLM_MODEL}: ", end="", flush=True)
            # 开启流式打字机逐字输出
            for chunk in llm.stream(user_input):
                print(chunk.content, end="", flush=True)
            print("\n")
    except Exception as e:
        print(f"\n❌ 模型调用异常: {e}")
