"""
================================================================================
企业级统一数据中台微服务 (Data Platform)
数据契约层 (schemas.py)
================================================================================
"""

from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional


class DataRetrieveRequest(BaseModel):
    """数据中台：统一多路检索与双层漏斗过滤请求"""
    query: str = Field(..., description="检索提问关键词或语句")
    doc_name_filter: Optional[str] = Field(None, description="限定在特定文档中检索，为空则全库检索")
    top_k: int = Field(10, description="第一层 Chroma 向量初筛候选召回数")
    top_n: int = Field(3, description="第二层 Cross-Attention 深度精排保留数")
    distance_threshold: float = Field(550.0, description="第一层向量欧氏距离硬性截断阈值")
    rerank_threshold: float = Field(0.40, description="第二层语义重排得分硬性截断阈值")


class DocumentChunksRequest(BaseModel):
    """数据中台：按文档名精确定向读取切片请求"""
    doc_name: str = Field(..., description="目标文档名称")
    max_chunks: int = Field(6, description="最大读取切片数")


class ConfirmUploadRequest(BaseModel):
    """数据中台：确认预检文档入库请求"""
    filename: str = Field(..., description="待入库的文档文件名")
    clean_mode: str = Field("rule", description="数据清洗模式：'rule' (规则清洗) 或 'llm' (大模型结构化清洗)")


class CancelUploadRequest(BaseModel):
    """数据中台：取消预检文档请求"""
    filename: str = Field(..., description="待放弃的文件名")


# ==============================================================================
# 全局多智能体会话与历史记忆模型
# ==============================================================================

class SessionCreateRequest(BaseModel):
    """数据中台：创建会话请求"""
    title: Optional[str] = Field("新对话", description="会话标题")
    agent_id: Optional[str] = Field("agentic_rag_core", description="所属智能体业务标识")


class SessionTitleUpdateRequest(BaseModel):
    """数据中台：更新会话标题请求"""
    title: str = Field(..., description="新的会话标题")


class SessionMessageAddRequest(BaseModel):
    """数据中台：追加历史消息请求"""
    role: str = Field(..., description="角色: 'user' 或 'assistant'")
    content: str = Field(..., description="消息正文内容")
    steps: Optional[List[Dict[str, Any]]] = Field(None, description="智能体多跳推理步骤轨迹")
    referenced_docs: Optional[List[str]] = Field(None, description="引用到的事实文档清单")
