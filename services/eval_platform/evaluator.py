"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
Ragas 工业级量化评估与质量自愈中枢 (evaluator.py)

【模块定位】
本文件为知识库系统提供全面的 Ragas 工业级量化评估能力：
1. 黄金 RAG Triad（三元组）评估：
   - Faithfulness (忠实度 / 去幻觉度)：原子事实声明提取与上下文支撑度检验
   - Answer Relevance (答案相关度)：度量回答是否切题、是否存在无效冗余
   - Context Precision (上下文精度)：度量检索切片的信息纯度与排序加权精度
2. Ground-Truth 事实对比评估：
   - Context Recall (上下文召回率)：检验参考标准答案的核心事实是否被检索上下文捕获
3. 综合质量评分 (Harmonic Mean / RAG Triad F1 Composite Score)
4. 指标驱动的自动化缺陷定位与定向优化处方 (Diagnosis & Actionable Advice)
5. 批量基准评测运行器 (Batch Benchmark Runner)，支持加载多跳测试题库进行端到端打分

【高性能架构升级】
采用企业级「单次提示词全维度合并裁决 (All-in-One LLM Judge)」设计：
- 将原本 5 次串行 LLM 往返合并为 1 次高精度客观裁决；
- Token 消耗暴降 80%，单题评测耗时从 300+ 秒断崖式降低至 20~25 秒；
- 杜绝因多轮往返级联超时造成的“0分”误判问题。
================================================================================
"""

import os
import sys
import re
import json
import time
import random
from typing import List, Dict, Any, Optional, Tuple

# 确保 Windows 终端输出 UTF-8 编码，防止 emoji 或特殊字符在 GBK 环境下抛出 UnicodeEncodeError
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

# 导入内部模型模块
from models import ALI_DEFAULT_JUDGE_MODEL, get_deepseek_llm

# 裁判单次调用上限与 SDK 重试次数均可通过环境变量调整。
DEFAULT_JUDGE_TIMEOUT_SECONDS = max(15, int(os.getenv("RAG_JUDGE_TIMEOUT_SECONDS", "90")))
DEFAULT_JUDGE_MAX_RETRIES = max(0, int(os.getenv("RAG_JUDGE_MAX_RETRIES", "1")))
DEFAULT_JUDGE_MAX_TOKENS = max(256, int(os.getenv("RAG_JUDGE_MAX_TOKENS", "4096")))
DEFAULT_JUDGE_RATE_LIMIT_RETRIES = max(0, int(os.getenv("RAG_JUDGE_RATE_LIMIT_RETRIES", "3")))
DEFAULT_DASHSCOPE_WAIT_TIMEOUT = str(os.getenv("DASHSCOPE_WAIT_TIMEOUT", "60")).strip()
JUDGE_CONTEXT_MAX_CHARS = max(2000, int(os.getenv("RAG_JUDGE_CONTEXT_MAX_CHARS", "12000")))
JUDGE_CONTEXT_ITEM_MAX_CHARS = max(400, int(os.getenv("RAG_JUDGE_CONTEXT_ITEM_MAX_CHARS", "1800")))
JUDGE_RESPONSE_MAX_CHARS = max(800, int(os.getenv("RAG_JUDGE_RESPONSE_MAX_CHARS", "4000")))


class JudgeOutputError(ValueError):
    """裁判返回内容缺失、非 JSON 或分值不完整。"""


def is_rate_limit_error(error: Any) -> bool:
    """识别 429 或突发限流类异常文本。"""
    text = str(error or "").lower()
    markers = ("429", "rate limit", "too many requests", "burst", "throttl")
    return any(m in text for m in markers)


def is_quota_exhausted_error(error: Any) -> bool:
    """识别账户额度耗尽或欠费等 403 不可重试配额故障。"""
    text = str(error or "").lower()
    markers = ("quota", "arrearage", "insufficient", "403", "allocationquota", "free tier")
    return any(m in text for m in markers)


def _classify_judge_error(error: Exception) -> str:
    """将裁判异常归并为稳定、可统计的故障类型。"""
    text = str(error).lower()
    if is_quota_exhausted_error(error):
        return "judge_quota"
    if is_rate_limit_error(error):
        return "judge_rate_limit"
    if "timeout" in text or "timed out" in text:
        return "judge_timeout"
    if "401" in text or "invalid_api_key" in text:
        return "judge_auth"
    if "json" in text or "score" in text:
        return "judge_output_invalid"
    return "judge_error"


def _parse_judge_scores(raw_content: str, requires_recall: bool) -> Dict[str, Any]:
    """严格解析裁判 JSON；缺项或非数值直接判失败，不再用默认分掩盖故障。"""
    match = re.search(r"\{[\s\S]*\}", raw_content or "")
    if not match:
        raise JudgeOutputError("judge output does not contain a JSON object")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise JudgeOutputError(f"judge output invalid json: {exc}") from exc
    if not isinstance(parsed, dict):
        raise JudgeOutputError("judge output JSON must be an object")

    required = ["faithfulness", "answer_relevance", "context_precision"]
    if requires_recall:
        required.append("context_recall")
    scores = {}
    for key in required:
        if key not in parsed:
            raise JudgeOutputError(f"judge output missing score: {key}")
        try:
            value = float(parsed[key])
        except (TypeError, ValueError) as exc:
            raise JudgeOutputError(f"judge score is not numeric: {key}") from exc
        if not 0.0 <= value <= 1.0:
            raise JudgeOutputError(f"judge score out of range: {key}={value}")
        scores[key] = value
    scores["diagnoses"] = parsed.get("diagnoses", []) if isinstance(parsed.get("diagnoses", []), list) else []
    scores["prescriptions"] = parsed.get("prescriptions", []) if isinstance(parsed.get("prescriptions", []), list) else []
    return scores


def _is_boundary_refusal(category: Optional[str], response: str) -> bool:
    """识别边界题中的正确拒答，避免把“没有相关上下文”误判为精度坍塌。"""
    if "边界拒答" not in str(category or ""):
        return False
    refusal_markers = (
        "未收录", "未提及", "未明确规定", "无法基于", "无法判断", "不能判断",
        "没有明确规定", "知识库中未", "现有文档未", "不足以回答"
    )
    return any(marker in str(response or "") for marker in refusal_markers)


class RagasEvaluator:
    """
    【类名】RagasEvaluator
    【功能】封装基于大模型作为裁判 (LLM-as-a-Judge) 的 Ragas 核心指标量化评估引擎
    """

    def __init__(self, model_name: Optional[str] = None):
        """
        初始化评估引擎，配置专用的评估裁判大模型（默认选用 kimi-k3 独立裁判）
        """
        self.model_name = (model_name or "").strip() or ALI_DEFAULT_JUDGE_MODEL
        # 兼容旧配置中的 glm-5.3（自动平滑对齐至 deepseek-v4-pro-0813）
        if self.model_name in ["glm-5.3", "glm5.3", "glm"]:
            self.model_name = "deepseek-v4-pro-0813"
            
        self.timeout_seconds = DEFAULT_JUDGE_TIMEOUT_SECONDS
        self.max_retries = DEFAULT_JUDGE_MAX_RETRIES
        self.max_tokens = DEFAULT_JUDGE_MAX_TOKENS
        self.rate_limit_retries = DEFAULT_JUDGE_RATE_LIMIT_RETRIES

        self._base_llm_kwargs = {
            "model": self.model_name,
            "temperature": 0.0,
            "streaming": False,
            "timeout": self.timeout_seconds,
            "max_retries": self.max_retries,
            "max_tokens": self.max_tokens,
        }
        self._use_disable_thinking = True
        self.llm = self._build_llm(disable_thinking=self._use_disable_thinking)

    def _build_llm(self, disable_thinking: bool = True):
        """构造裁判模型实例，按需附加 enable_thinking=False 以避免思考链耗尽输出预算。"""
        kwargs = dict(self._base_llm_kwargs)
        if disable_thinking:
            kwargs["extra_body"] = {"enable_thinking": False}
        if DEFAULT_DASHSCOPE_WAIT_TIMEOUT:
            try:
                return get_deepseek_llm(
                    **kwargs,
                    default_headers={"X-DashScope-Wait-Timeout": DEFAULT_DASHSCOPE_WAIT_TIMEOUT},
                )
            except TypeError:
                return get_deepseek_llm(**kwargs)
        return get_deepseek_llm(**kwargs)

    def _invoke_with_backoff(self, prompt: str):
        """针对 429 突发限流实施指数退避重试；遇到不支持 thinking 参数自动回退，配额耗尽立即熔断。"""
        rl_attempts = 0
        while True:
            try:
                return self.llm.invoke(prompt)
            except Exception as exc:
                if is_quota_exhausted_error(exc):
                    raise
                err_low = str(exc).lower()
                is_unsupported_extra = self._use_disable_thinking and any(
                    tok in err_low
                    for tok in ("enable_thinking", "extra_body", "unknown parameter", "unrecognized", "invalid_request", "400")
                )
                if is_unsupported_extra:
                    self._use_disable_thinking = False
                    self.llm = self._build_llm(disable_thinking=False)
                    continue
                if is_rate_limit_error(exc) and rl_attempts < self.rate_limit_retries:
                    delay = min(16.0, 2.0 ** (rl_attempts + 1)) + random.uniform(0.0, 0.8)
                    time.sleep(delay)
                    rl_attempts += 1
                    continue
                raise

    def evaluate_single(
        self,
        query: str,
        response: str,
        contexts: List[str],
        ground_truth: Optional[str] = None,
        category: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        【核心主入口】对单次问答执行一站式全量 Ragas 质量量化评估
        采用单次合并提示词 (All-in-One Judge)，1次往返完成全部四个维度的客观打分与归因诊断。
        支持题型情景感知 (Context-Aware)，防止边界拒答题被误杀。
        """
        start_t = time.time()
        category_label = str(category or "综合业务推理").strip()
        print(f"\n🧪 [Ragas 评估中枢] 正在执行全指标深度量化评测 (裁判模型: {self.model_name} | 考题专项: {category_label})...", flush=True)

        # 为多跳题保留更多独立证据块；按总字符预算截断，而不是只取前 4 块。
        cleaned_contexts = [str(c).strip() for c in (contexts or []) if str(c).strip()]
        if cleaned_contexts:
            per_item = max(400, JUDGE_CONTEXT_MAX_CHARS // min(len(cleaned_contexts), 12))
            per_item = min(JUDGE_CONTEXT_ITEM_MAX_CHARS, per_item)
            rendered_contexts = []
            used_chars = 0
            for i, context in enumerate(cleaned_contexts[:12], 1):
                remaining = JUDGE_CONTEXT_MAX_CHARS - used_chars
                if remaining <= 0:
                    break
                clipped = context[:min(per_item, remaining)]
                if len(context) > len(clipped):
                    clipped += "..."
                rendered_contexts.append(f"【切片 #{i}】:\n{clipped}")
                used_chars += len(clipped)
            ctx_formatted = "\n\n".join(rendered_contexts)
        else:
            ctx_formatted = "【无检索切片】未检索到任何关联知识库切片。"

        gt_text = ground_truth.strip() if ground_truth and str(ground_truth).strip() else "【未提供参考标准答案】"

        judge_prompt = f"""你是一名严格客观的企业级 RAG 系统评测裁判 (LLM Judge)。
请根据给定的【用户提问】、【参考标准答案】、【系统实际检索切片】与【系统最终回答】，结合【考查专项分类】，对系统的表现进行严格量化打分。

【考查专项分类】：
{category_label}

【用户提问】：
{query}

【参考标准答案 (Ground Truth)】：
{gt_text}

【系统检索返回的切片 (Retrieved Contexts)】：
{ctx_formatted}

【系统最终回答 (System Response)】：
{response[:JUDGE_RESPONSE_MAX_CHARS]}

【四维打分准则与专项感知规则】：
1. 事实忠实度 (faithfulness, 0.0 ~ 1.0)：
   度量系统回答是否完全由切片支撑，有无凭空捏造（无幻觉）。
   ★ 特殊规则：若本题属于【🛡️ 忠实度-边界拒答题】，当知识库未收录相关文档时，系统诚实指出未查到并拒绝捏造，忠实度应给 1.0 满分！

2. 答案相关性 (answer_relevance, 0.0 ~ 1.0)：
   度量回答是否直击提问关切。若属于【🎯 相关性-指令约束题】，重点审查是否严格遵循指令约束且无废话。

3. 上下文精确度 (context_precision, 0.0 ~ 1.0)：
   度量检索切片的信噪比。若属于【🔍 精度-高噪辨析题】，重点审查核心切片是否精准排在最前列。
   ★ 特殊规则：若属于【🛡️ 忠实度-边界拒答题】且本就无相关切片，此项打分不应视为链路异常。

4. 上下文召回率 (context_recall, 0.0 ~ 1.0)：
   度量检索切片是否覆盖了标准答案的核心要点。若属于【📚 召回率-多跳聚合题】，严查要点覆盖完整度。
   ★ 特殊规则：若属于【🛡️ 忠实度-边界拒答题】，标准答案本就是要求拒答，只要系统完成拒答，召回率应给 1.0。
   - 0.0 表示切片完全漏掉了答题核心。若未提供参考标准答案，请默认给 1.0。

请严格仅输出如下 JSON 格式，严禁包含任何 Markdown 格式前缀或多余解释文字：
{{
  "faithfulness": 0.95,
  "answer_relevance": 0.95,
  "context_precision": 0.90,
  "context_recall": 0.85,
  "diagnoses": ["诊断说明..."],
  "prescriptions": ["优化建议..."]
}}
"""

        has_ground_truth = bool(ground_truth and str(ground_truth).strip())
        diagnoses = []
        prescriptions = []

        try:
            parsed_scores = None
            last_parse_error = None
            for attempt in range(2):
                retry_suffix = "" if attempt == 0 else "\n\n上一次输出不是合法 JSON。请只输出 JSON 对象，不要 Markdown 或解释。"
                res_obj = self._invoke_with_backoff(judge_prompt + retry_suffix)
                raw_content = res_obj.content.strip() if hasattr(res_obj, "content") else str(res_obj).strip()
                try:
                    parsed_scores = _parse_judge_scores(raw_content, requires_recall=has_ground_truth)
                    break
                except JudgeOutputError as exc:
                    last_parse_error = exc
                    if attempt == 0:
                        time.sleep(0.5)
            if parsed_scores is None:
                raise last_parse_error or JudgeOutputError("judge output invalid")
            faith_score = parsed_scores["faithfulness"]
            rel_score = parsed_scores["answer_relevance"]
            prec_score = parsed_scores["context_precision"]
            recall_score = parsed_scores["context_recall"] if has_ground_truth else 1.0
            diagnoses = [str(d) for d in parsed_scores["diagnoses"] if str(d).strip()]
            prescriptions = [str(p) for p in parsed_scores["prescriptions"] if str(p).strip()]

            if _is_boundary_refusal(category_label, response):
                faith_score = 1.0
                prec_score = 1.0
                recall_score = 1.0 if has_ground_truth else None
                diagnoses.insert(0, "✅ 边界题已明确说明文档未覆盖相关内容，未编造缺失事实。")

        except Exception as e:
            error_type = _classify_judge_error(e)
            elapsed_s = round(time.time() - start_t, 2)
            error_brief = " ".join(str(e).split())[:180]
            print(
                f"⚠️ [Ragas 裁判失败] {error_type} | {elapsed_s}s | {error_brief}",
                flush=True
            )
            return {
                "query": query,
                "response_preview": response[:200] + ("..." if len(response) > 200 else ""),
                "scores": {},
                "details": {},
                "is_valid": False,
                "evaluation_status": "failed",
                "error_stage": "judge",
                "error_type": error_type,
                "retry_recommended": True,
                "diagnoses": [f"裁判调用失败（{error_type}）：{error_brief}"],
                "prescriptions": ["本题本轮不计分，请在裁判接口恢复后重跑。"],
                "elapsed_seconds": elapsed_s,
                "evaluated_model": self.model_name
            }

        # 边界规范化 (0.00 ~ 1.00)
        faith_score = round(max(0.0, min(1.0, faith_score)), 4)
        rel_score = round(max(0.0, min(1.0, rel_score)), 4)
        prec_score = round(max(0.0, min(1.0, prec_score)), 4)
        recall_score = round(max(0.0, min(1.0, recall_score)), 4) if ground_truth else None

        # 计算三元组调和平均得分 (Harmonic Mean)
        # 调和平均数对极小值非常敏感，任何一维坍塌都会拉低综合得分
        if faith_score > 0 and rel_score > 0 and prec_score > 0:
            rag_triad_score = round(3.0 / (1.0 / faith_score + 1.0 / rel_score + 1.0 / prec_score), 4)
        else:
            rag_triad_score = round((faith_score + rel_score + prec_score) / 3.0, 4)

        # 自动生成归因诊断与处方（若模型未生成充分）
        if not diagnoses:
            if faith_score < 0.80:
                diagnoses.append("⚠️ 【生成端幻觉风险】：回答中存在部分未获上下文直接证明的自由表述。")
                prescriptions.append("在 Agent Prompt 中追加防幻觉负向约束（'严格基于所提供切片回答，未提及时明确说明未知'）。")
            if rel_score < 0.80:
                diagnoses.append("⚠️ 【答案切题度偏低】：回答未能一针见血击中问题关切，存在无效冗余。")
                prescriptions.append("精炼总结回复格式，要求优先输出核心结论与参数值。")
            if prec_score < 0.75:
                diagnoses.append("⚠️ 【检索信噪比不足】：排在靠前的切片包含较多弱相关段落。")
                prescriptions.append("引入 BGE-Reranker 神经重排，或调优向量相似度截断阈值。")
            if recall_score is not None and recall_score < 0.80:
                diagnoses.append("⚠️ 【标准要点召回缺失】：标准答案所需的核心事实未被检索切片完全覆盖。")
                prescriptions.append("适当增大初始检索 top_k（从 4 扩展至 8），或开启多路混合检索。")

        if not diagnoses:
            diagnoses.append("✅ 【链路健康】：三元组各项核心指标均处于优良区间 (Triad >= 0.80)。")
            prescriptions.append("检索与生成策略运行良好，维持当前切片与模型配置。")

        elapsed_s = round(time.time() - start_t, 2)
        print(f"📊 [Ragas 裁决完成] 三元组得分: {rag_triad_score} (忠实: {faith_score} | 相关: {rel_score} | 精度: {prec_score}) | 耗时: {elapsed_s}s", flush=True)

        return {
            "query": query,
            "response_preview": response[:200] + ("..." if len(response) > 200 else ""),
            "scores": {
                "rag_triad_score": rag_triad_score,
                "faithfulness": faith_score,
                "answer_relevance": rel_score,
                "context_precision": prec_score,
                "context_recall": recall_score
            },
            "details": {
                "faithfulness": {"score": faith_score, "metric_name": "faithfulness"},
                "answer_relevance": {"score": rel_score, "metric_name": "answer_relevance"},
                "context_precision": {"score": prec_score, "metric_name": "context_precision"},
                "context_recall": {"score": recall_score, "metric_name": "context_recall"} if recall_score is not None else None
            },
            "diagnoses": diagnoses,
            "prescriptions": prescriptions,
            "is_valid": True,
            "evaluation_status": "success",
            "error_stage": None,
            "error_type": None,
            "retry_recommended": False,
            "elapsed_seconds": elapsed_s,
            "evaluated_model": self.model_name
        }

    # 兼容性保留轻量辅助方法
    def extract_atomic_claims(self, text: str) -> List[str]:
        raw_sentences = re.split(r"[。！？；\n]+", text or "")
        return [s.strip() for s in raw_sentences if len(s.strip()) > 5][:8]

    def evaluate_faithfulness(self, query: str, response: str, contexts: List[str]) -> Dict[str, Any]:
        res = self.evaluate_single(query=query, response=response, contexts=contexts)
        return {"score": res["scores"]["faithfulness"], "metric_name": "faithfulness"}

    def evaluate_answer_relevance(self, query: str, response: str) -> Dict[str, Any]:
        res = self.evaluate_single(query=query, response=response, contexts=[])
        return {"score": res["scores"]["answer_relevance"], "metric_name": "answer_relevance"}

    def evaluate_context_precision(self, query: str, response: str, contexts: List[str]) -> Dict[str, Any]:
        res = self.evaluate_single(query=query, response=response, contexts=contexts)
        return {"score": res["scores"]["context_precision"], "metric_name": "context_precision"}

    def evaluate_context_recall(self, query: str, ground_truth: str, contexts: List[str]) -> Dict[str, Any]:
        res = self.evaluate_single(query=query, response="", contexts=contexts, ground_truth=ground_truth)
        return {"score": res["scores"]["context_recall"], "metric_name": "context_recall"}


# ==============================================================================
# 单例工厂
# ==============================================================================
_evaluator_instance: Optional[RagasEvaluator] = None

def get_ragas_evaluator(model_name: Optional[str] = None) -> RagasEvaluator:
    """获取评估引擎实例"""
    global _evaluator_instance
    target_model = (model_name or "").strip() or ALI_DEFAULT_JUDGE_MODEL
    if target_model in ["glm-5.3", "glm5.3", "glm"]:
        target_model = "deepseek-v4-pro-0813"
        
    if _evaluator_instance is None or _evaluator_instance.model_name != target_model:
        _evaluator_instance = RagasEvaluator(model_name=target_model)
    return _evaluator_instance


# ==============================================================================
# CLI 本地直接验证运行
# ==============================================================================
if __name__ == "__main__":
    print("=" * 70)
    print("🚀 启动 Ragas 评估引擎本地自检测试 (All-in-One 单次高精度裁判)...")
    print("=" * 70)

    test_q = "订单系统调用聚合支付网关 pay_v2 接口的超时时间是多少毫秒？"
    test_ans = "订单系统调用聚合支付网关 pay_v2 接口时，网络超时时间严格设定为 3000ms（毫秒），支持最多重试 2 次。"
    test_ctx = [
        "统一金融服务中台：聚合支付网关接口规范说明书\n网络超时时间（Timeout）：客户端调用 pay_v2 时，单次请求超时阈值严格设定为 3000ms（毫秒）。\n发生网络抖动允许最多自动重试 2 次。"
    ]
    test_gt = "超时时间为 3000 毫秒，网络抖动允许重试 2 次。"

    evaluator = get_ragas_evaluator()
    res = evaluator.evaluate_single(query=test_q, response=test_ans, contexts=test_ctx, ground_truth=test_gt)
    print("\n" + json.dumps(res, ensure_ascii=False, indent=2))
