"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
业务重排漏斗引擎与双层阈值截断器 (reranker.py)

【模块定位】
本文件是 RAG 检索链路中第二阶段的"业务漏斗与精细过滤控制器"：
1. 云端高精 Cross-Attention 语义打分：
   - 彻底由 models.py 的统一百炼客户端【DashScopeRerankerClient (gte-rerank-v2)】驱动。
   - 共享全局 DASHSCOPE_API_KEY，免除本地大显存与模型下载负担。
2. 彻底删除本地 ONNX 未部署依赖：
   - 纯粹面向生产环境云端高精推理，精简无用冗余逻辑。
3. 双层阈值截断漏斗 (Two-Stage Threshold Funnel) 核心算法：
   - 第一层 (Top-K 初筛距离截断): 过滤掉欧氏距离 > distance_threshold (默认 550.0) 的绝对噪音，超纲题直接拦截清零；
   - 第二层 (Top-N 精排得分截断): 过滤掉百炼 Cross-Attention 得分 < rerank_score_threshold (默认 0.40) 的弱相关切片；
   - 诚实拒答机制: 若全部被过滤，标记 is_all_refused = True，大模型诚实回答"知识库中未检索到相关内容"。
================================================================================
"""

import os
import sys
import math
import functools
from typing import List, Dict, Any, Optional

# 确保所有 print 自动强制刷新
print = functools.partial(print, flush=True)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 从 models.py 统一导入百炼重排客户端与默认配置
from models import (
    get_dashscope_reranker,
    DEFAULT_RERANK_MODEL,
    get_ollama_embeddings
)

# 默认全局截断阈值配置
DEFAULT_TOP_K: int = 10                  # 初筛召回候选上限
DEFAULT_DISTANCE_THRESHOLD: float = 550.0 # 初筛距离截断阈值 (Chroma 欧氏距离 > 550 视为弱相关噪音，直接丢弃)

DEFAULT_TOP_N: int = 3                   # 重排送入大模型的最终切片数
DEFAULT_RERANK_THRESHOLD: float = 0.40   # 重排匹配得分截断阈值 (0.0 ~ 1.0，得分 < 0.40 彻底剔除)


class RerankerEngine:
    """
    【类名】RerankerEngine (原 LocalReranker)
    【功能】双层阈值截断漏斗控制器：由百炼 gte-rerank-v2 提供 Cross-Attention 打分
    """
    _instance: Optional["RerankerEngine"] = None

    def __init__(self):
        self.dashscope_client = get_dashscope_reranker()

    def _compute_ollama_semantic_scores(self, query: str, texts: List[str]) -> List[float]:
        """网络异常时的备用方案：利用本地 Ollama bge-m3 嵌入与关键词覆盖率综合打分"""
        try:
            embeddings = get_ollama_embeddings()
            q_vec = embeddings.embed_query(query)
            doc_vecs = embeddings.embed_documents(texts)

            scores = []
            q_words = set(query.lower().split())

            for doc_text, d_vec in zip(texts, doc_vecs):
                dot = sum(a * b for a, b in zip(q_vec, d_vec))
                norm_q = math.sqrt(sum(a * a for a in q_vec)) or 1e-9
                norm_d = math.sqrt(sum(b * b for b in d_vec)) or 1e-9
                cos_sim = max(0.0, dot / (norm_q * norm_d))

                doc_lower = doc_text.lower()
                keyword_hits = sum(1 for w in q_words if len(w) > 1 and w in doc_lower)
                keyword_ratio = keyword_hits / max(1, len(q_words))

                final_score = round(cos_sim * 0.70 + keyword_ratio * 0.30, 4)
                scores.append(min(1.0, max(0.0, final_score)))

            return scores
        except Exception as e:
            print(f"⚠️ 本地 Ollama 打分异常: {e}，返回保底分 0.5")
            return [0.5] * len(texts)

    def compute_rerank_scores(self, query: str, texts: List[str]) -> List[float]:
        """
        对传入的候选文本列表计算与 query 的相关性匹配得分 (0.0 ~ 1.0)
        核心由阿里百炼 gte-rerank-v2 提供深度 Cross-Attention 打分
        """
        if not texts:
            return []

        # 核心：调用 models.py 中的阿里百炼 gte-rerank-v2 高精云端 API
        scores = self.dashscope_client.compute_relevance_scores(query, texts)
        if any(s > 0 for s in scores):
            return scores

        # 仅在网络不通或鉴权失败时，由本地已有的 Ollama bge-m3 提供降级支持
        print("🔄 [备用链路] 启用本地 Ollama bge-m3 语义特征打分...")
        return self._compute_ollama_semantic_scores(query, texts)

    def rerank_and_filter(
        self,
        query: str,
        candidate_chunks: List[Dict[str, Any]],
        top_k: int = DEFAULT_TOP_K,
        distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD,
        top_n: int = DEFAULT_TOP_N,
        rerank_threshold: float = DEFAULT_RERANK_THRESHOLD
    ) -> Dict[str, Any]:
        """
        【双层阈值截断漏斗核心执行函数】
        
        【入参】:
            - query: 用户提问
            - candidate_chunks: 向量库初筛召回的原始切片列表 (带有 distance 字段)
            - top_k: 初筛截断数量限制 (默认 10)
            - distance_threshold: 第一层初筛距离截断阈值 (默认 550.0，> 阈值直接丢弃)
            - top_n: 第二层重排最终保留切片数 (默认 3)
            - rerank_threshold: 第二层重排得分截断阈值 (默认 0.40，< 阈值彻底剔除)

        【返回值】:
            Dict:
                - final_chunks: 最终通过两道截断把关的高纯度切片列表
                - total_candidate_count: 原始初筛切片数
                - passed_stage1_count: 通过第一层初筛距离截断的切片数
                - passed_stage2_count: 通过第二层重排得分截断的切片数
                - is_all_refused: 是否所有切片均被截断 (若为 True，直接触发诚实拒答)
        """
        total_candidate_count = len(candidate_chunks)

        # ----------------------------------------------------------------------
        # 第一层截断：Top-K 初筛距离截断 (Distance Threshold Cutoff)
        # ----------------------------------------------------------------------
        stage1_passed = []
        for c in candidate_chunks[:top_k]:
            dist = c.get("distance", 999.0)
            if dist <= distance_threshold:
                stage1_passed.append(c)

        print(f"\n🛡️ [第一层截断: Top-{top_k} 初筛] 候选 {total_candidate_count} 个 ➔ 距离 <= {distance_threshold} 通过: {len(stage1_passed)} 个")

        if not stage1_passed:
            print(f"🚫 [初筛全部截断] 所有切片距离均超过阈值 {distance_threshold}，判定为与知识库无关提问，直接截断！")
            return {
                "final_chunks": [],
                "total_candidate_count": total_candidate_count,
                "passed_stage1_count": 0,
                "passed_stage2_count": 0,
                "is_all_refused": True
            }

        # ----------------------------------------------------------------------
        # 第二层截断：Top-N 精细重排得分截断 (gte-rerank-v2 Score Cutoff)
        # ----------------------------------------------------------------------
        texts_to_score = [c.get("text", "") for c in stage1_passed]
        rerank_scores = self.compute_rerank_scores(query, texts_to_score)

        # 回填得分并排序
        scored_chunks = []
        for chunk, score in zip(stage1_passed, rerank_scores):
            chunk_copy = dict(chunk)
            chunk_copy["rerank_score"] = score
            scored_chunks.append(chunk_copy)

        # 按百炼重排得分从高到低倒序排列
        scored_chunks.sort(key=lambda x: x["rerank_score"], reverse=True)

        # 严格截断低于 rerank_threshold 的边缘切片
        stage2_passed = [c for c in scored_chunks if c["rerank_score"] >= rerank_threshold]
        final_chunks = stage2_passed[:top_n]
        return {
            "final_chunks": final_chunks,
            "total_candidate_count": total_candidate_count,
            "passed_stage1_count": len(stage1_passed),
            "passed_stage2_count": len(stage2_passed),
            "is_all_refused": len(final_chunks) == 0
        }


# 向后兼容别名与单例工厂
LocalReranker = RerankerEngine

def get_local_reranker() -> RerankerEngine:
    """获取单例重排引擎"""
    if RerankerEngine._instance is None:
        RerankerEngine._instance = RerankerEngine()
    return RerankerEngine._instance
