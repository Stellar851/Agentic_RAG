"""
================================================================================
企业级数据中台标准化 Model Context Protocol (MCP) 服务端
(services/data_platform/mcp_server.py)

【微服务定位与标准】
1. 遵循 Anthropic 官方 MCP 2.x 标准规范；
2. 暴露标准化知识库两阶段漏斗检索工具与文档精读工具；
3. 暴露只读知识库资产资源 (kb://documents)；
4. 同时支持作为 Starlette/FastAPI SSE 子应用挂载，以及标准 stdio 本地通信。
================================================================================
"""

import os
import sys
import json
from typing import Optional, List, Dict, Any

from mcp.server.mcpserver import MCPServer

# 确保中台根目录在 sys.path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from vector_store import get_vector_store
from reranker import get_local_reranker

# 初始化全局 MCP Server 实例
mcp_server = MCPServer(
    name="EnterpriseDataPlatformMCP",
    description="企业级数据中台标准知识库与两阶段检索 MCP 服务"
)


# ==============================================================================
# 核心公共业务函数 (与现有 REST 接口完全共享底层算法)
# ==============================================================================

def perform_two_stage_retrieval(
    query: str,
    doc_name_filter: Optional[str] = None,
    top_k: int = 15,
    top_n: int = 5,
    distance_threshold: float = 550.0,
    rerank_threshold: float = 0.40
) -> Dict[str, Any]:
    """
    【核心底座算法】两阶段高信噪比漏斗过滤：
    1. 第一阶段初筛：ChromaDB 向量距离初筛 (<= 550.0)；
    2. 第二阶段精排：阿里百炼 gte-rerank 语义交叉精排 (>= 0.40)；
    3. 诚实拒答机制：无合格切片时严厉拒答。
    """
    query = (query or "").strip()
    doc_filter = doc_name_filter.strip() if doc_name_filter else None
    if not query:
        return {
            "formatted_text": "【检索错误】检索关键词 query 不能为空。",
            "final_chunks": [],
            "referenced_docs": [],
            "is_all_refused": True
        }

    vs = get_vector_store()
    candidate_chunks = vs.query_similar_chunks(query=query, top_k=top_k, doc_name_filter=doc_filter)
    if not candidate_chunks:
        target_desc = f"在文档【{doc_filter}】中" if doc_filter else "在全库中"
        return {
            "formatted_text": f"{target_desc}未能初筛到与 '{query}' 相关的片段，请尝试更换检索关键词或扩大检索范围。",
            "final_chunks": [],
            "referenced_docs": [],
            "is_all_refused": True
        }

    reranker = get_local_reranker()
    rerank_res = reranker.rerank_and_filter(
        query=query,
        candidate_chunks=candidate_chunks,
        top_k=top_k,
        distance_threshold=distance_threshold,
        top_n=top_n,
        rerank_threshold=rerank_threshold
    )

    final_chunks = rerank_res.get("final_chunks", [])
    is_refused = rerank_res.get("is_all_refused", False) or (len(final_chunks) == 0)
    referenced_docs = sorted(list({c["doc_name"] for c in final_chunks if "doc_name" in c}))

    if is_refused:
        target_desc = f"文档【{doc_filter}】" if doc_filter else "全量知识库"
        formatted_text = (
            f"【权威核验结果】经数据中台两阶段高信噪比阈值过滤，{target_desc}中未检索到任何符合业务相关度标准的有效条款或段落。\n"
            f"（所有候选切片距离均超过阈值 {distance_threshold} 或语义重排得分低于 {rerank_threshold}）。\n"
            f"请直接向用户诚实明确指出：当前知识库中未收录与 '{query}' 相关的内容或该业务未有明确规定，严禁主观凭空捏造答案。"
        )
    else:
        snippets = []
        for i, r in enumerate(final_chunks, 1):
            score_info = f" | 重排得分: {r.get('rerank_score', '--')}" if "rerank_score" in r else ""
            snippets.append(
                f"【来源 {i}】文档: 《{r['doc_name']}》 (切片 {r['chunk_index'] + 1}/{r['total_chunks']})\n"
                f"相关度距离: {r['distance']}{score_info}\n"
                f"内容片段:\n{r['text']}\n"
                f"----------------------------------------"
            )
        formatted_text = "\n\n".join(snippets)

    return {
        "formatted_text": formatted_text,
        "final_chunks": final_chunks,
        "referenced_docs": referenced_docs,
        "is_all_refused": is_refused
    }


def perform_read_document_chunks(doc_name: str, max_chunks: int = 6) -> Dict[str, Any]:
    """定向读取特定文档的切片"""
    doc_name = (doc_name or "").strip()
    if not doc_name:
        return {"formatted_text": "【错误】未指定要阅读的文档名 doc_name。"}

    vs = get_vector_store()
    chunks = vs.get_chunks_by_doc_name(doc_name=doc_name, max_chunks=max_chunks)
    if not chunks:
        return {"formatted_text": f"知识库中未找到名为《{doc_name}》的文档。"}

    snippets = []
    for c in chunks:
        snippets.append(
            f"【切片 {c['chunk_index'] + 1}/{c['total_chunks']}】\n"
            f"{c['text']}\n"
            f"----------------------------------------"
        )
    formatted_text = f"=== 正在精读文档《{doc_name}》前 {len(chunks)} 个章节切片 ===\n\n" + "\n\n".join(snippets)
    return {
        "formatted_text": formatted_text,
        "chunks": chunks,
        "doc_name": doc_name
    }


# ==============================================================================
# MCP 标准 Tools 注册
# ==============================================================================

@mcp_server.tool(
    name="search_knowledge_base",
    description=(
        "【企业级数据中台两阶段精准检索工具】\n"
        "当用户询问系统架构规范、安全合规条款、超时重试配置、容灾标准或任何具体业务规则时必须调用此工具。\n"
        "底层集成第一层向量距离初筛(<=550.0)与第二层百炼gte-rerank语义交叉精排(>=0.40)，输出高可信证据切片。\n"
        "若知识库无相关规定，将明确触发诚实拒答凭据。"
    )
)
def search_knowledge_base(query: str, doc_name_filter: Optional[str] = None) -> str:
    """
    执行两阶段知识库语义检索与重排。
    :param query: 检索提问关键词或完整语句
    :param doc_name_filter: 可选，限定检索的特定文档名称 (例如 01_分布式微服务与金融级容灾架构规范_V3.md)
    :return: 经过双层阈值过滤的高信噪比格式化切片文本
    """
    res = perform_two_stage_retrieval(query=query, doc_name_filter=doc_name_filter)
    return res["formatted_text"]


@mcp_server.tool(
    name="read_specific_document",
    description=(
        "【企业级数据中台文档全篇精读工具】\n"
        "当用户明确指定某篇具体文档需要深入研读，或初筛后需要顺序阅读该文档上下文时调用。\n"
        "输入完整文档名称，按章节索引顺序返回前 max_chunks 个切片正文。"
    )
)
def read_specific_document(doc_name: str, max_chunks: int = 6) -> str:
    """
    定向顺序读取指定文档的正文切片。
    :param doc_name: 完整的文档文件名 (必须包含后缀，如 .md 或 .txt)
    :param max_chunks: 最多读取的切片数量，默认 6
    :return: 顺序拼接的文档章节切片正文
    """
    res = perform_read_document_chunks(doc_name=doc_name, max_chunks=max_chunks)
    return res["formatted_text"]


# ==============================================================================
# MCP 标准 Resources 注册
# ==============================================================================

@mcp_server.resource("kb://documents")
def list_knowledge_documents() -> str:
    """列出企业知识库中当前收录的全部受控文档清单与切片数量"""
    vs = get_vector_store()
    docs = vs.list_all_documents()
    payload = {
        "total_documents": len(docs),
        "total_chunks": vs.collection.count(),
        "documents": docs
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import anyio
    print("=" * 60)
    print("🚀 [MCP Server] 正在以 stdio 模式启动企业数据中台 MCP 服务...")
    print("支持接入: Cursor / Claude Desktop / VS Code MCP 客户端")
    print("=" * 60)
    anyio.run(mcp_server.run_stdio_async)
