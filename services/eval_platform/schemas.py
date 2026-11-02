"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
数据契约模型层 (schemas.py)

【模块定位】
集中声明系统中所有 HTTP 请求与响应的 Pydantic 数据模型，消除循环依赖，保障类型安全。
================================================================================
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


# ==============================================================================
# 一、 知识库与文档上传相关模型
# ==============================================================================

class ConfirmUploadRequest(BaseModel):
    """确认文档入库请求体"""
    filenames: List[str] = Field(..., description="待入库的文件名列表")
    clean_mode: Optional[str] = Field("rule", description="清洗模式：llm 或 rule")


class CancelUploadRequest(BaseModel):
    """取消上传并清理暂存请求体"""
    filenames: List[str] = Field(..., description="待清理的暂存文件名列表")


# ==============================================================================
# 二、 对话与会话管理相关模型
# ==============================================================================

class CreateSessionRequest(BaseModel):
    """新建会话请求体"""
    title: Optional[str] = Field("新对话", description="会话标题")


class ChatRequest(BaseModel):
    """发起 Agentic RAG 提问请求体"""
    query: str = Field(..., description="用户提问内容")
    session_id: Optional[str] = Field(None, description="会话 ID，若为空自动新建")
    history: Optional[List[Dict[str, Any]]] = Field(None, description="显式传递的历史消息")
    stream: Optional[bool] = Field(True, description="是否启用 SSE 流式输出")
    model_name: Optional[str] = Field(None, description="可选：指定本次问答使用的大模型名称（热切换）")


# ==============================================================================
# 三、 企业级五阶段评测流水线相关模型
# ==============================================================================

class BenchmarkGenerateRequest(BaseModel):
    """阶段一：知识库出题请求体"""
    mode: Optional[str] = Field("from-guide", description="模式：from-guide 或 generate")
    model_a: Optional[str] = Field("qwen3.8-max", description="命题专家模型名称（必须为阿里百炼顶级逻辑旗舰 qwen3.8-max）")
    case_count: Optional[int] = Field(10, description="需要出题/加载的题目数量，如 5, 10, 20 等")


class BenchmarkSaveCasesRequest(BaseModel):
    """阶段二：审题与题库持久化保存请求体"""
    cases: List[Dict[str, Any]] = Field(..., description="待保存持久化的用例列表")


class BenchmarkRunAnswersRequest(BaseModel):
    """阶段三：考生批量作答请求体"""
    limit: Optional[int] = Field(None, description="测试题目数量限制，例如 3 或 5，None 为全量")
    model_b: Optional[str] = Field("qwen3.7-plus", description="考生系统模型（必须为阿里百炼主力问答模型 qwen3.7-plus）")


class BenchmarkRunJudgeRequest(BaseModel):
    """阶段四：裁判深度量化评分请求体"""
    answered_cases: List[Dict[str, Any]] = Field(..., description="已生成答案的测试用例列表")
    model_c: Optional[str] = Field("qwen3.8-max", description="裁判评测模型（必须为阿里百炼顶级逻辑旗舰 qwen3.8-max）")


class SyncExportRequest(BaseModel):
    """阶段五：前端与后端即时写盘同步请求体"""
    summary: Dict[str, Any] = Field(..., description="前端当前展示的最新量化评估总结与用例记录")
