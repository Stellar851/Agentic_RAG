"""
================================================================================
Agentic RAG 企业级 Model Context Protocol (MCP) 客户端适配模块
(Agentic_RAG/mcp_client.py)

【模块定位】
1. 作为标准的 MCP Client，通过 SSE 传输协议连接数据中台 MCP Server (Port 8001/mcp/sse)；
2. 封装同步与异步工具调用接口，为 Agent Tools 提供标准 MCP 通信通道；
3. 具备工业级自动降级容错机制 (MCP -> REST 优雅降级)，确保 100% 高可用。
================================================================================
"""

import os
import sys
import json
import asyncio
from typing import Dict, Any, Optional, Callable

# 默认中台 MCP 端点地址
DATA_PLATFORM_MCP_URL = os.getenv("DATA_PLATFORM_MCP_URL", "http://127.0.0.1:8001/mcp/sse")
DATA_PLATFORM_URL = os.getenv("DATA_PLATFORM_URL", "http://127.0.0.1:8001")


async def _async_call_mcp_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    mcp_endpoint: str = DATA_PLATFORM_MCP_URL,
    timeout: float = 25.0
) -> Optional[str]:
    """通过 MCP SSE 协议异步调用数据中台暴露的标准工具"""
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(mcp_endpoint, timeout=timeout) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            # 提取文本返回
            if hasattr(result, "content") and result.content:
                text_parts = [c.text for c in result.content if getattr(c, "type", "") == "text" or hasattr(c, "text")]
                if text_parts:
                    return "\n".join(text_parts)
            # 兼容 structured_content
            if hasattr(result, "structured_content") and result.structured_content:
                sc = result.structured_content
                if isinstance(sc, dict) and "result" in sc:
                    return str(sc["result"])
                return json.dumps(sc, ensure_ascii=False)
            return str(result)


def call_mcp_tool_sync(
    tool_name: str,
    arguments: Dict[str, Any],
    fallback_func: Optional[Callable[..., Any]] = None,
    trace_id: Optional[str] = None
) -> str:
    """
    【统一工具调用入口】优先走工业级 MCP 协议；若发生网络不可达或超时，自动平滑降级至 REST 适配器
    """
    try:
        # 在独立的临时事件循环或当前线程中运行 MCP 客户端
        result = asyncio.run(_async_call_mcp_tool(tool_name, arguments))
        if result is not None:
            print(f"  🔌 [MCP 协议驱动] 成功通过 Model Context Protocol (SSE) 调用数据中台: {tool_name}")
            return result
    except Exception as mcp_err:
        print(f"  ⚠️ [MCP 协议通信提示] 暂时无法通过 MCP SSE 连接数据中台 ({mcp_err})，自动执行优雅降级...")

    # 执行降级兜底方案 (REST API)
    if fallback_func is not None:
        return fallback_func(**arguments)

    return f"【工具执行异常】未能通过 MCP 或备用通道执行工具 {tool_name}"
