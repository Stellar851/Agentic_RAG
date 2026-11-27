# -*- coding: utf-8 -*-
"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
智能体工具集与中台接入模块 (tools.py)

【模块定位】
本文件是系统的"感知与执行工具箱"：
1. 封装被 @tool 装饰的原子工具，供 LangChain Agent 进行 Function Calling 绑定：
   - `list_available_documents`: 宏观感知工具，获取知识库中所有可用文档名称与切片统计。
   - `search_knowledge_base`: 向量语义两阶段过滤检索工具，支持全库或单文档定向检索。
   - `read_specific_document`: 连续段落精读工具，查看指定文档的上下文脉络。
2. 承载与数据中台 (Port 8001) 的 HTTP 通信与降级逻辑，支持 X-Trace-ID 全链路透传。
3. 对外导出统一的 `AGENT_TOOLS` 注册列表，便于快速横向扩展新工具（如网络搜索、计算器等）。
================================================================================
"""

import os
import sys
import json
import functools
from typing import List, Dict, Any, Optional

# 确保所有 print 自动强制刷新
print = functools.partial(print, flush=True)

# 确保 Windows 终端输出 UTF-8 编码，防止 emoji 或特殊字符在 GBK 环境下抛出 UnicodeEncodeError
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests
from langchain_core.tools import tool

# 数据中台微服务通信配置 (Port 8001)
DATA_PLATFORM_URL = os.getenv('DATA_PLATFORM_URL', 'http://127.0.0.1:8001')

try:
    from common.logging_config import get_trace_id
except Exception:
    get_trace_id = lambda: '-'


def _get_data_platform_doc_names() -> set:
    """通过 HTTP 从数据中台获取最新合法文档名称集合，用于文档引用强校验与去噪"""
    try:
        resp = requests.get(
            f"{DATA_PLATFORM_URL}/api/v1/data/documents",
            headers={"X-Trace-ID": get_trace_id()},
            timeout=5
        )
        if resp.status_code == 200:
            docs = resp.json().get("data", {}).get("documents", [])
            return {d.get("doc_name") for d in docs if d.get("doc_name")}
    except Exception:
        pass
    return set()


def _call_data_platform_retrieve(query: str, doc_name_filter: Optional[str] = None) -> Optional[str]:
    """通过 HTTP 调用数据中台的两阶段漏斗向量检索服务"""
    try:
        resp = requests.post(
            f"{DATA_PLATFORM_URL}/api/v1/data/retrieve",
            json={
                "query": query,
                "doc_name_filter": doc_name_filter,
                "top_k": 6 if doc_name_filter else 10,
                "top_n": 4 if doc_name_filter else 3
            },
            headers={"X-Trace-ID": get_trace_id()},
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            metrics = data.get("metrics", {})
            cand_cnt = metrics.get("candidate_count", 0)
            fin_cnt = metrics.get("final_count", 0)
            top_sc = metrics.get("top1_score", 0.0)
            print(f"  ⚡ [数据中台赋能] 成功通过 HTTP 接入数据中台 (Port 8001) 初筛:{cand_cnt} | 精排:{fin_cnt} | 最高分:{top_sc}")
            return data.get("formatted_text")
    except Exception:
        pass
    return None


# ==============================================================================
# 一、 智能体原子工具库 (@tool)
# ==============================================================================

@tool
def list_available_documents() -> str:
    """
    【工具功能】获取知识库中当前已上传的所有文档列表及简要统计信息。
    【适用场景】当用户提问未指明具体文档名称、询问整体知识库资产、或需要进行多文档宏观对比时调用。
    """
    from mcp_client import call_mcp_tool_sync

    def _fallback_list_docs() -> str:
        print("\n📚 [Agent 降级触发] list_available_documents: 正在通过 HTTP 从数据中台 (Port 8001) 读取知识库文档资产...")
        try:
            resp = requests.get(
                f"{DATA_PLATFORM_URL}/api/v1/data/documents",
                headers={"X-Trace-ID": get_trace_id()},
                timeout=5
            )
            if resp.status_code == 200:
                docs = resp.json().get("data", {}).get("documents", [])
                if not docs:
                    return "当前知识库为空，尚未录入任何业务文档。"
                lines = [
                    f"- {d.get('doc_name')} ({d.get('chunk_count') or d.get('total_chunks', 0)}切片)"
                    for d in docs
                ]
                return f"知识库共 {len(docs)} 篇文档：\n" + "\n".join(lines)
        except Exception as e:
            return f"数据中台连接异常: {e}"
        return "获取文档清单失败"

    return call_mcp_tool_sync(
        tool_name="list_available_documents",
        arguments={},
        fallback_func=_fallback_list_docs,
        trace_id=get_trace_id()
    )


@tool
def search_knowledge_base(query: str, doc_name_filter: Optional[str] = None) -> str:
    """
    【工具功能】在知识库中进行语义相似度向量检索，由数据中台统一执行两阶段高信噪比漏斗过滤。
    【参数说明】
        query: 明确具体的检索关键词或查询问题，禁止使用模糊代词（如“它”、“这个系统”）。
        doc_name_filter: 可选，指定仅在某个特定文档内定向检索（例如 '系统网关改造手册.md'）。
    """
    from mcp_client import call_mcp_tool_sync

    def _fallback_search(query: str, doc_name_filter: Optional[str] = None):
        data = _call_data_platform_retrieve(query=query, doc_name_filter=doc_name_filter)
        return data or f"未能从数据中台检索到与 '{query}' 相关的片段，或数据中台当前不可用。"

    return call_mcp_tool_sync(
        tool_name="search_knowledge_base",
        arguments={"query": query, "doc_name_filter": doc_name_filter},
        fallback_func=_fallback_search,
        trace_id=get_trace_id()
    )


@tool
def read_specific_document(doc_name: str, max_chunks: int = 6) -> str:
    """
    【工具功能】连续精读指定文档的上下文片段，支持查看某文档的连续章节内容。
    【参数说明】
        doc_name: 必须是完整的文档名称（例如 '用户手册.pdf' 或 '系统架构说明.md'）。
        max_chunks: 读取的最大连续切片数量，默认 6 片。
    """
    from mcp_client import call_mcp_tool_sync

    def _fallback_read(doc_name: str, max_chunks: int = 6):
        print(f"\n📖 [Agent 降级触发] read_specific_document: 正在通过 HTTP 从数据中台精读文档《{doc_name}》...")
        try:
            resp = requests.get(
                f"{DATA_PLATFORM_URL}/api/v1/data/document-chunks",
                params={"doc_name": doc_name, "max_chunks": max_chunks},
                headers={"X-Trace-ID": get_trace_id()},
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                return data.get("formatted_text", f"未读取到文档《{doc_name}》的内容。")
        except Exception as e:
            return f"精读文档《{doc_name}》异常: {e}"
        return f"未能从数据中台获取文档《{doc_name}》的切片内容。"

    return call_mcp_tool_sync(
        tool_name="read_specific_document",
        arguments={"doc_name": doc_name, "max_chunks": max_chunks},
        fallback_func=_fallback_read,
        trace_id=get_trace_id()
    )


# ==============================================================================
# 二、 工具集统一注册导出列表 (AGENT_TOOLS)
# ==============================================================================

AGENT_TOOLS = [
    list_available_documents,
    search_knowledge_base,
    read_specific_document
]
