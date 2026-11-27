"""
================================================================================
企业级工业评测中台微服务 (Evaluation Platform)
数据契约模型层 (services/eval_platform/schemas.py)
================================================================================
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


class BenchmarkGenerateRequest(BaseModel):
    """阶段一：知识库出题请求体"""
    mode: Optional[str] = Field("from-guide", description="模式：from-guide 或 from-docs/generate")
    model_a: Optional[str] = Field("glm-5.2", description="命题专家模型名称（默认阿里百炼 glm-5.2）")
    case_count: Optional[int] = Field(10, description="需要出题/加载的题目数量，如 5, 10, 20, 100 等")
    append_existing: bool = Field(False, description="是否保留已有题库并增量补齐至目标题数")


class BenchmarkSaveCasesRequest(BaseModel):
    """阶段二：审题与题库持久化保存请求体"""
    cases: List[Dict[str, Any]] = Field(..., description="待保存持久化的用例列表")


class BenchmarkRunAnswersRequest(BaseModel):
    """阶段三：考生批量作答请求体"""
    limit: Optional[int] = Field(None, description="测试题目数量限制，例如 3 或 5，None 或 0 为全量")
    model_b: Optional[str] = Field("qwen3.8-27b", description="考生系统模型（默认阿里百炼主力问答模型 qwen3.8-27b）")
    concurrency: Optional[int] = Field(None, description="作答并发数，默认 8")
    resume: bool = Field(False, description="是否从本地作答 Checkpoint 断点续跑")


class BenchmarkRunJudgeRequest(BaseModel):
    """阶段四：裁判深度量化评分请求体"""
    answered_cases: List[Dict[str, Any]] = Field(..., description="已生成答案的测试用例列表")
    model_c: Optional[str] = Field("kimi-k3", description="裁判评测模型（默认阿里百炼深度反思模型 kimi-k3）")
    concurrency: Optional[int] = Field(None, description="裁判并发数，默认 10")
    resume: bool = Field(False, description="是否从本地裁判 Checkpoint 断点续跑")


class BenchmarkRunPipelineRequest(BaseModel):
    """一键双模型流水线并发评测请求体"""
    limit: Optional[int] = Field(None, description="测试题目数量限制，None 或 0 为全量")
    model_b: Optional[str] = Field(None, description="考生作答模型")
    model_c: Optional[str] = Field(None, description="独立裁判模型")
    answer_concurrency: Optional[int] = Field(None, description="作答并发数，默认 8")
    judge_concurrency: Optional[int] = Field(None, description="裁判并发数，默认 10")
    resume: bool = Field(False, description="是否启用断点续跑")
    reuse_answers: bool = Field(False, description="是否复用已有作答仅重跑裁判")


class SyncExportRequest(BaseModel):
    """阶段五：前端与后端即时写盘同步请求体"""
    summary: Dict[str, Any] = Field(..., description="前端当前展示的最新量化评估总结与用例记录")
