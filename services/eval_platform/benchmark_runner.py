"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
自动化基准评测运行器 (benchmark_runner.py)

【三模型评测流水线核心执行模块】
- 模型 A (qwen3.8-max-0902)       : [前序阶段] 派生高质量多维考题与标准答案
- 人工审查 (Excel 审核)      : [把关阶段] 人工过滤并确认黄金基准
- 模型 B (qwen3.8-flash)     : [考生系统] 多跳 Agentic 检索与推理作答
- 模型 C (qwen3.8-max-0902)       : [独立裁判] 严格客观无偏见 Ragas 指标量化打分

【高性能工程特性】
1. 多线程并发评测：ThreadPoolExecutor (默认 4 并发)，总耗时压缩至 30~45 秒。
2. 极速模式 (--quick)：仅测 3 题，8 秒出结果，专为日常微调代码设计。
3. 错题重跑机制 (--retry-failed)：只重新评测上一次未达标的题目，秒级反馈。
4. Excel 与 JSON 双报告生成：包含宏观指标看板、逐题证据链与自动化诊断处方。
================================================================================
"""

import os
import sys
import io
import time
import json
import argparse
import warnings
import threading
from datetime import datetime
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional

# 彻底屏蔽第三方库控制台警告干扰
warnings.filterwarnings("ignore")

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_console_lock = threading.Lock()

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICES_DIR = os.path.dirname(CURRENT_DIR)
PROJECT_ROOT = os.path.dirname(SERVICES_DIR)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import openpyxl
from models import ALI_DEFAULT_ANSWER_MODEL, ALI_DEFAULT_JUDGE_MODEL
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

AGENT_STOP_MARKERS = ['agent stopped due to iteration limit or time limit', 'iteration limit', 'time limit', 'could not parse llm output', 'agent stopped']

def is_agent_stop_output(text):
    normalized = ' '.join(str(text or '').lower().split())
    return any(marker in normalized for marker in AGENT_STOP_MARKERS)

def build_agent_failure_answer(contexts=None):
    if contexts:
        evidence = ' '.join(str(c) for c in contexts[:2]).strip()
        evidence = evidence[:360] + ('...' if len(evidence) > 360 else '')
        return '系统未能在限定时间内完成充分推理，当前无法可靠回答。\n\n' + evidence
    return '系统未能在限定时间内检索到有效业务依据，当前无法可靠回答。'

class RemoteAgentClient:
    def __init__(self, agent_url=None):
        self.agent_url = agent_url or os.getenv('AGENT_SERVICE_URL', 'http://localhost:8000/api/chat')

    def ask(self, user_query, model_name=None, verbose=False):
        import requests
        try:
            resp = requests.post(
                self.agent_url,
                json={'query': user_query, 'model_name': model_name, 'stream': False, 'persist': False},
                timeout=120
            )
            if resp.status_code == 200:
                d = resp.json().get('data', {})
                contexts = d.get('contexts') or []
                steps = d.get('steps') or []
                if not contexts and steps:
                    for s in steps:
                        obs = s.get('observation') or s.get('observation_preview') or ''
                        if obs and ('【切片' in obs or '【来源' in obs or '=== 正在精读' in obs):
                            contexts.append(obs)
                return {
                    'answer': d.get('answer', ''),
                    'contexts': contexts,
                    'steps': steps,
                    'success': d.get('success', True),
                    'error_type': d.get('error_type')
                }
            return {'answer': f'Agent HTTP {resp.status_code}', 'contexts': [], 'success': False, 'error_type': f'http_{resp.status_code}'}
        except Exception as e:
            return {'answer': f'Agent error: {e}', 'contexts': [], 'success': False, 'error_type': 'agent_exception'}

def get_agent_service():
    shared = os.path.join(PROJECT_ROOT, 'Agentic_RAG')
    if os.path.exists(shared) and shared not in sys.path:
        sys.path.insert(0, shared)
    try:
        from agent import get_agent_service as local_get
        return local_get()
    except Exception:
        return RemoteAgentClient()
from evaluator import get_ragas_evaluator

BENCHMARK_DIR = os.path.join(CURRENT_DIR, "benchmarks")
os.makedirs(BENCHMARK_DIR, exist_ok=True)
DEFAULT_BENCHMARK_EXCEL = os.path.join(BENCHMARK_DIR, "待审核基准测试集.xlsx")
LATEST_REPORT_JSON = os.path.join(BENCHMARK_DIR, "最新评测报告.json")


# ==============================================================================
# 一、 加载人工审核后的 Excel 题库
# ==============================================================================

def load_reviewed_cases(excel_path: str = DEFAULT_BENCHMARK_EXCEL) -> List[Dict[str, Any]]:
    """
    【功能】读取人工审核后的 Excel 测试集，过滤剔除项，采用修正答案
    """
    if not os.path.exists(excel_path):
        print(f"⚠️ [错误] 未找到基准测试集表格: {excel_path}")
        return []

    wb = openpyxl.load_workbook(excel_path, data_only=True)
    ws = wb.active

    cases = []
    # 从第 2 行开始读取
    for row in range(2, ws.max_row + 1):
        case_id = ws.cell(row=row, column=1).value
        category = ws.cell(row=row, column=2).value or "综合业务推理"
        query = ws.cell(row=row, column=3).value
        raw_gt = ws.cell(row=row, column=4).value or ""
        source_chunks = ws.cell(row=row, column=5).value or ""
        source_doc = ws.cell(row=row, column=6).value or ""
        review_status = str(ws.cell(row=row, column=7).value or "").strip()
        reviewed_gt = ws.cell(row=row, column=8).value or ""

        # 跳过空行或被人工标记为“剔除”、“删除”、“不通过”的题目
        if not query or not str(query).strip():
            continue
        if any(reject in review_status for reject in ["剔除", "删除", "废弃", "不通过"]):
            print(f"⏩ [人工过滤] 跳过第 {case_id} 题: 标记为【{review_status}】")
            continue

        # 审核备注
        review_comment = str(ws.cell(row=row, column=9).value or "").strip()

        # 优先使用人工修正后的答案，若无修正则使用原始黄金答案
        final_gt = str(reviewed_gt).strip() if str(reviewed_gt).strip() else str(raw_gt).strip()

        cases.append({
            "id": int(case_id) if case_id else len(cases) + 1,
            "category": str(category).strip(),
            "query": str(query).strip(),
            "raw_ground_truth": str(raw_gt).strip(),
            "ground_truth": final_gt,
            "source_chunks": str(source_chunks).strip(),
            "source_doc": str(source_doc).strip(),
            "review_status": review_status or "通过",
            "reviewed_gt": str(reviewed_gt).strip(),
            "reviewed_ground_truth": str(reviewed_gt).strip(),
            "review_comment": review_comment,
            "review_notes": review_comment,
            "excel_row": row
        })

    return cases


# ==============================================================================
# 二、 单题评测执行单元 (模型 B 回答 + 模型 C 裁决)
# ==============================================================================

def _build_case_result(
    case: Dict[str, Any],
    *,
    answer: str,
    contexts: List[str],
    qa_latency: float,
    judge_latency: float,
    total_latency: float,
    scores: Dict[str, Any],
    is_valid: bool,
    is_passed: bool,
    evaluation_status: str,
    error_stage: Optional[str],
    error_type: Optional[str],
    retry_recommended: bool,
    diagnoses: List[str],
    prescriptions: List[str],
    model_b: str
) -> Dict[str, Any]:
    """构造统一单题结果，确保有效性与故障阶段可被报表和前端识别。"""
    return {
        "id": case["id"],
        "category": case["category"],
        "query": case["query"],
        "raw_ground_truth": case.get("raw_ground_truth", ""),
        "ground_truth": case.get("ground_truth", ""),
        "source_chunks": case.get("source_chunks", ""),
        "source_doc": case.get("source_doc", ""),
        "review_status": case.get("review_status", "通过"),
        "reviewed_gt": case.get("reviewed_gt", ""),
        "review_comment": case.get("review_comment", ""),
        "system_answer": answer,
        "contexts_count": len(contexts),
        "contexts_preview": ("\n---\n".join(contexts[:3]))[:800] if contexts else "未检索到切片",
        "scores": scores,
        "is_valid": is_valid,
        "is_passed": bool(is_valid and is_passed),
        "execution_status": "failed" if error_stage == "agent" else "success",
        "evaluation_status": evaluation_status,
        "error_stage": error_stage,
        "error_type": error_type,
        "retry_recommended": retry_recommended,
        "diagnoses": diagnoses,
        "prescriptions": prescriptions,
        "qa_latency": qa_latency,
        "judge_latency": judge_latency,
        "total_latency": total_latency,
        "model_b": model_b
    }


def evaluate_single_case(
    case: Dict[str, Any],
    model_b: str,
    model_c: str
) -> Dict[str, Any]:
    """
    执行单个测试用例：
    1. 调用模型 B (Agentic RAG) 检索并作答；
    2. 仅在作答成功时调用模型 C (独立裁判) 量化打分；
    3. Agent 中断或裁判失败时标记为无效样本，不伪造 0 分。
    """
    agent_service = get_agent_service()
    evaluator = get_ragas_evaluator(model_name=model_c)

    q = case["query"]
    gt = case.get("ground_truth", "")
    start_t = time.time()

    with redirect_stdout(io.StringIO()):
        try:
            qa_res = agent_service.ask(user_query=q, model_name=model_b, verbose=False)
            ans = str(qa_res.get("answer", ""))
            ctxs = qa_res.get("contexts", []) or []
            stop_detected = is_agent_stop_output(ans)
            qa_success = bool(qa_res.get("success", True)) and not stop_detected
            qa_error_type = (
                qa_res.get("error_type") or ("agent_max_iterations" if stop_detected else "agent_error")
            ) if not qa_success else None
        except Exception as e:
            ans = build_agent_failure_answer([])
            ctxs = []
            qa_success = False
            qa_error_type = "agent_exception"
        qa_time = round(time.time() - start_t, 2)

        if not qa_success:
            total_time = round(time.time() - start_t, 2)
            return _build_case_result(
                case,
                answer=ans,
                contexts=ctxs,
                qa_latency=qa_time,
                judge_latency=0.0,
                total_latency=total_time,
                scores={},
                is_valid=False,
                is_passed=False,
                evaluation_status="skipped",
                error_stage="agent",
                error_type=qa_error_type or "agent_max_iterations",
                retry_recommended=True,
                diagnoses=["Agent 未能在限定步骤内完成可靠作答，本题不计入质量通过率。"],
                prescriptions=["缩小问题范围或提高 RAG_AGENT_MAX_ITERATIONS 后重跑。"],
                model_b=model_b
            )

        eval_start = time.time()
        try:
            eval_res = evaluator.evaluate_single(
                query=q,
                response=ans,
                contexts=ctxs,
                ground_truth=gt,
                category=case.get("category", "")
            )
        except Exception as e:
            eval_res = {
                "scores": {},
                "is_valid": False,
                "evaluation_status": "failed",
                "error_stage": "judge",
                "error_type": "judge_error",
                "retry_recommended": True,
                "diagnoses": [f"裁判调用异常：{str(e)[:180]}"],
                "prescriptions": ["本题本轮不计分，请在裁判接口恢复后重跑。"]
            }
        judge_time = round(time.time() - eval_start, 2)
        total_time = round(time.time() - start_t, 2)

        eval_valid = bool(
            eval_res.get("is_valid", eval_res.get("evaluation_status") == "success")
        )
        if not eval_valid:
            return _build_case_result(
                case,
                answer=ans,
                contexts=ctxs,
                qa_latency=qa_time,
                judge_latency=judge_time,
                total_latency=total_time,
                scores={},
                is_valid=False,
                is_passed=False,
                evaluation_status="failed",
                error_stage=eval_res.get("error_stage", "judge"),
                error_type=eval_res.get("error_type", "judge_error"),
                retry_recommended=True,
                diagnoses=eval_res.get("diagnoses", ["裁判未返回有效评分。"]),
                prescriptions=eval_res.get("prescriptions", ["本题本轮不计分，请重跑。"]),
                model_b=model_b
            )

        scores = eval_res.get("scores", {})
        triad_score = float(scores.get("rag_triad_score", 0.0) or 0.0)
        is_passed = triad_score >= 0.80
        return _build_case_result(
            case,
            answer=ans,
            contexts=ctxs,
            qa_latency=qa_time,
            judge_latency=judge_time,
            total_latency=total_time,
            scores=scores,
            is_valid=True,
            is_passed=is_passed,
            evaluation_status="success",
            error_stage=None,
            error_type=None,
            retry_recommended=False,
            diagnoses=eval_res.get("diagnoses", []),
            prescriptions=eval_res.get("prescriptions", []),
            model_b=model_b
        )


# ==============================================================================
# 三、 评测汇总统计 (仅有效样本参与质量指标)
# ==============================================================================

def build_benchmark_summary(
    case_results: List[Dict[str, Any]],
    model_b: str,
    model_c: str,
    total_elapsed: float = 0.0,
    max_workers: Optional[int] = None
) -> Dict[str, Any]:
    """汇总评测结果，将质量失败与流程故障严格分离。"""
    total_cases = len(case_results)
    valid_results = [r for r in case_results if bool(r.get("is_valid", True))]
    invalid_results = [r for r in case_results if not bool(r.get("is_valid", True))]
    system_failed = [r for r in invalid_results if r.get("error_stage") == "agent"]
    judge_failed = [r for r in invalid_results if r.get("error_stage") == "judge"]
    passed_cases = sum(1 for r in valid_results if bool(r.get("is_passed", False)))

    def avg_score(key: str) -> Optional[float]:
        values = []
        for result in valid_results:
            value = (result.get("scores") or {}).get(key)
            if value is not None:
                values.append(float(value))
        return round(sum(values) / len(values), 4) if values else None

    error_breakdown: Dict[str, int] = {}
    for result in invalid_results:
        error_type = str(result.get("error_type") or "unknown")
        error_breakdown[error_type] = error_breakdown.get(error_type, 0) + 1

    valid_cases = len(valid_results)
    invalid_cases = len(invalid_results)
    pass_rate = round(passed_cases / total_cases, 4) if total_cases else 0.0
    valid_pass_rate = round(passed_cases / valid_cases, 4) if valid_cases else 0.0

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model_b_system": model_b,
        "model_c_judge": model_c,
        "total_cases": total_cases,
        "valid_cases": valid_cases,
        "invalid_cases": invalid_cases,
        "passed_cases": passed_cases,
        "quality_failed_cases": max(0, valid_cases - passed_cases),
        "system_failed_cases": len(system_failed),
        "judge_failed_cases": len(judge_failed),
        "pass_rate": pass_rate,
        "valid_pass_rate": valid_pass_rate,
        "system_failure_rate": round(len(system_failed) / total_cases, 4) if total_cases else 0.0,
        "judge_failure_rate": round(len(judge_failed) / total_cases, 4) if total_cases else 0.0,
        "error_breakdown": error_breakdown,
        "average_scores": {
            "rag_triad_score": avg_score("rag_triad_score") or 0.0,
            "faithfulness": avg_score("faithfulness") or 0.0,
            "answer_relevance": avg_score("answer_relevance") or 0.0,
            "context_precision": avg_score("context_precision") or 0.0,
            "context_recall": avg_score("context_recall")
        },
        "performance": {
            "total_elapsed_seconds": total_elapsed,
            "average_case_seconds": round(total_elapsed / max(1, total_cases), 2),
            "concurrency_workers": max_workers
        },
        "elapsed_seconds": total_elapsed,
        "evaluation_basis": "quality_averages_over_valid_cases",
        "case_results": case_results
    }


# ==============================================================================
# 四、 高性能多线程并发评测调度器
# ==============================================================================

def run_benchmark_concurrent(
    cases: List[Dict[str, Any]],
    model_b: str = ALI_DEFAULT_ANSWER_MODEL,
    model_c: str = ALI_DEFAULT_JUDGE_MODEL,
    max_workers: int = 4
) -> Dict[str, Any]:
    """
    【并发执行引擎】使用线程池并发执行批量问答与独立打分
    """
    total = len(cases)
    print(f"\n{'='*75}")
    print(f"🚀 [启动并发评测流水线] 总用例数: {total} | 并发工作线程: {max_workers}")
    print(f"   考生系统 (模型 B): 【{model_b}】")
    print(f"   独立裁判 (模型 C): 【{model_c}】")
    print(f"{'='*75}\n", flush=True)

    results = []
    completed_count = 0
    start_all = time.time()

    # 提前在主线程预热远程客户端与裁判单例（评测中台已与向量库解耦）
    get_agent_service()
    get_ragas_evaluator(model_name=model_c)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_case = {
            executor.submit(evaluate_single_case, c, model_b, model_c): c
            for c in cases
        }

        for future in as_completed(future_to_case):
            completed_count += 1
            res = future.result()
            results.append(res)

            is_valid = bool(res.get("is_valid", True))
            s = res.get("scores") or {}
            with _console_lock:
                if not is_valid:
                    error_label = "系统故障" if res.get("error_stage") == "agent" else "裁判失败"
                    print(
                        f"[{completed_count:2d}/{total:2d}] ⚠ {error_label} 用例 #{res['id']:02d} "
                        f"【{res['category'][:8]}】 {res.get('error_type') or 'unknown'} | 不计分 | 耗时: {res['total_latency']}s",
                        file=sys.__stdout__,
                        flush=True
                    )
                else:
                    status_symbol = "✓" if res["is_passed"] else "✗"
                    rec_str = f"{s['context_recall']:.2f}" if s.get("context_recall") is not None else "N/A"
                    print(
                        f"[{completed_count:2d}/{total:2d}] {status_symbol} 用例 #{res['id']:02d} "
                        f"【{res['category'][:8]}】 三元组: {float(s.get('rag_triad_score', 0.0)):.2f} "
                        f"(忠实: {float(s.get('faithfulness', 0.0)):.2f} | 相关: {float(s.get('answer_relevance', 0.0)):.2f} | "
                        f"精度: {float(s.get('context_precision', 0.0)):.2f} | 召回: {rec_str}) | 耗时: {res['total_latency']}s",
                        file=sys.__stdout__,
                        flush=True
                    )
                if res.get("diagnoses"):
                    diag_brief = str(res["diagnoses"][0]).replace("\n", " ").strip()
                    if len(diag_brief) > 80:
                        diag_brief = diag_brief[:80] + "..."
                    print(f"       └─ {diag_brief}\n", file=sys.__stdout__, flush=True)
                else:
                    print("", file=sys.__stdout__, flush=True)

    # 按照用例 ID 重新排序
    results.sort(key=lambda x: x["id"])

    # 汇总统计：仅有效样本参与质量指标
    total_elapsed = round(time.time() - start_all, 2)
    summary = build_benchmark_summary(
        case_results=results,
        model_b=model_b,
        model_c=model_c,
        total_elapsed=total_elapsed,
        max_workers=max_workers
    )
    return summary


# ==============================================================================
# 五、 导出专业 Excel 评测报告与 JSON 看板
# ==============================================================================

def export_evaluation_report(summary: Dict[str, Any], output_excel_path: str) -> str:
    """
    【报告生成】基于评测集导出专业 Excel 报表：
    - 工作表 1（主表）: 评测集与逐题量化指标 (完整保留评测集前9列，顺延生成实际回答、检索切片与各量化指标)
    - 工作表 2（看板）: 全链路宏观总览看板 (汇总统计、指标雷达与优化指南)
    """
    wb = openpyxl.Workbook()
    
    # 样式配置
    font_title = Font(name="微软雅黑", size=16, bold=True, color="1F4E79")
    font_section = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
    
    fill_dataset = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid") # 原评测集深蓝
    fill_qa = PatternFill(start_color="1B6A78", end_color="1B6A78", fill_type="solid")      # 检索与作答青蓝
    fill_metrics = PatternFill(start_color="1E7E34", end_color="1E7E34", fill_type="solid") # 量化指标深绿
    fill_diag = PatternFill(start_color="4A235A", end_color="4A235A", fill_type="solid")    # 诊断处方深紫
    
    fill_pass = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")    # 达标淡绿
    fill_fail = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")    # 未达标淡红
    fill_invalid = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid") # 流程故障淡黄
    align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    align_left = Alignment(horizontal="left", vertical="center", wrap_text=True)
    border_thin = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9")
    )

    # ==============================================================================
    # 填充工作表 1：基准评测集与逐题量化指标 (默认首选主表)
    # ==============================================================================
    ws_main = wb.active
    ws_main.title = "评测集及量化指标明细"

    detail_headers = [
        # 1-9: 评测集原始字段 (与待审核基准测试集完全对齐)
        "序号", "问题分类", "测试问题 (Query)", "黄金标准答案 (Ground Truth)",
        "来源切片依据 (Source Chunks)", "来源文档", "审核状态",
        "人工修正答案", "审核备注",
        
        # 10-21: 在每道题后面追加的实际表现与量化指标
        "系统回答 (Model B)", "实际检索切片数", "实际检索切片内容",
        "🎯 三元组总分 (Harmonic)", "🛡️ 事实忠实度", "🎯 答案相关性",
        "🔍 上下文精度", "📚 上下文召回率", "评测结果", "裁判归因诊断", "处方优化建议", "耗时(秒)"
    ]
    ws_main.append(detail_headers)
    ws_main.row_dimensions[1].height = 32

    # 设置表头颜色区分
    for col_idx in range(1, len(detail_headers) + 1):
        cell = ws_main.cell(row=1, column=col_idx)
        cell.font = font_section
        cell.alignment = align_center
        cell.border = border_thin
        if col_idx <= 9:
            cell.fill = fill_dataset  # 评测集原题：深蓝
        elif col_idx <= 12:
            cell.fill = fill_qa       # 系统作答与检索：青蓝
        elif col_idx <= 18:
            cell.fill = fill_metrics  # 量化打分与判定：深绿
        else:
            cell.fill = fill_diag     # 诊断处方与耗时：深紫

    for row_idx, r in enumerate(summary.get("case_results", []), start=2):
        s = r.get("scores") or {}
        is_valid = bool(r.get("is_valid", True))
        is_passed = bool(r.get("is_passed", False)) if is_valid else False
        if not is_valid:
            status_str = "系统故障" if r.get("error_stage") == "agent" else "裁判失败"
        else:
            status_str = "通过" if is_passed else "未达标"

        diag_val = r.get("diagnoses") or []
        if isinstance(diag_val, list):
            diag_str = "\n".join(str(d) for d in diag_val)
        else:
            diag_str = str(diag_val)

        presc_val = r.get("prescriptions") or []
        if isinstance(presc_val, list):
            presc_str = "\n".join(str(p) for p in presc_val)
        else:
            presc_str = str(presc_val)

        rag_triad = s.get("rag_triad_score")
        faith = s.get("faithfulness")
        ans_rel = s.get("answer_relevance")
        ctx_prec = s.get("context_precision")
        ctx_rec = s.get("context_recall")

        def score_value(value: Any) -> Any:
            if not is_valid or value is None:
                return "N/A"
            return round(float(value), 4)

        row_vals = [
            r.get("id", row_idx - 1),
            r.get("category", ""),
            r.get("query", ""),
            r.get("raw_ground_truth") or r.get("ground_truth", ""),
            r.get("source_chunks", ""),
            r.get("source_doc", ""),
            r.get("review_status", "通过"),
            r.get("reviewed_gt", ""),
            r.get("review_comment", ""),
            r.get("system_answer", ""),
            r.get("contexts_count", 0),
            r.get("contexts_preview", ""),
            score_value(rag_triad),
            score_value(faith),
            score_value(ans_rel),
            score_value(ctx_prec),
            score_value(ctx_rec),
            status_str,
            diag_str,
            presc_str,
            r.get("total_latency", r.get("qa_latency", 0.0))
        ]
        ws_main.append(row_vals)

        # 单元格格式化与指标高亮
        for col in range(1, len(detail_headers) + 1):
            c = ws_main.cell(row=row_idx, column=col)
            c.font = Font(name="微软雅黑", size=10)
            c.border = border_thin
            # 居中对齐列
            if col in [1, 2, 6, 7, 11, 13, 14, 15, 16, 17, 18, 21]:
                c.alignment = align_center
            else:
                c.alignment = align_left

        # 审核状态列高亮
        status_review = str(r.get("review_status", ""))
        if "通过" in status_review:
            ws_main.cell(row=row_idx, column=7).fill = fill_pass

        # 指标分值高亮 (>=0.80 绿色)
        for score_col in [13, 14, 15, 16]:
            val_c = ws_main.cell(row=row_idx, column=score_col)
            try:
                if float(val_c.value) >= 0.80:
                    val_c.fill = fill_pass
            except Exception:
                pass

        # 评测判定结果列高亮 (第 18 列)
        pass_c = ws_main.cell(row=row_idx, column=18)
        if not is_valid:
            pass_c.fill = fill_invalid
        else:
            pass_c.fill = fill_pass if is_passed else fill_fail

        ws_main.row_dimensions[row_idx].height = 55

    # 列宽设定
    col_widths = {
        1: 8, 2: 16, 3: 35, 4: 35, 5: 35, 6: 18, 7: 12, 8: 30, 9: 15,
        10: 45, 11: 12, 12: 35,
        13: 16, 14: 14, 15: 14, 16: 14, 17: 14, 18: 12,
        19: 35, 20: 35, 21: 12
    }
    for col_idx, width in col_widths.items():
        ws_main.column_dimensions[get_column_letter(col_idx)].width = width

    # ==============================================================================
    # 填充工作表 2：评测总览与宏观看板
    # ==============================================================================
    ws_summary = wb.create_sheet(title="评测总览看板")
    ws_summary.merge_cells("A1:E1")
    ws_summary["A1"] = "🎯 Agentic RAG 黄金基准自动化评测综合报表"
    ws_summary["A1"].font = font_title
    ws_summary["A1"].alignment = align_center
    ws_summary.row_dimensions[1].height = 40

    ws_summary.append([])
    now_default = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timestamp_val = summary.get("timestamp") or now_default
    model_b_val = summary.get("model_b_system") or "qwen3.7-plus"
    model_c_val = summary.get("model_c_judge") or "qwen3.8-max-0902"
    perf = summary.get("performance") or {}
    total_cases_count = summary.get("total_cases", len(summary.get("case_results", [])))
    valid_cases_count = summary.get("valid_cases", total_cases_count)
    invalid_cases_count = summary.get("invalid_cases", 0)
    system_failed_count = summary.get("system_failed_cases", 0)
    judge_failed_count = summary.get("judge_failed_cases", 0)
    valid_pass_rate = summary.get("valid_pass_rate", summary.get("pass_rate", 0.0))
    failure_rate = round(invalid_cases_count / total_cases_count, 4) if total_cases_count else 0.0
    total_elapsed = perf.get("total_elapsed_seconds", summary.get("elapsed_seconds", 0.0))
    avg_elapsed = perf.get("average_case_seconds", round(float(total_elapsed) / max(1, total_cases_count), 2))

    ws_summary.append(["评测时间", timestamp_val, "", "考生系统 (模型 B)", model_b_val])
    ws_summary.append(["总用例数", total_cases_count, "", "独立裁判 (模型 C)", model_c_val])
    ws_summary.append(["有效样本数", valid_cases_count, "", "全量并发耗时", f"{total_elapsed} 秒"])
    ws_summary.append([
        "流程故障数",
        f"{invalid_cases_count}（系统 {system_failed_count} / 裁判 {judge_failed_count}）",
        "",
        "流程故障率",
        f"{failure_rate * 100:.1f}%"
    ])
    ws_summary.append(["全部样本通过率", f"{(summary.get('pass_rate', 0.0) * 100):.1f}%", "", "单题平均耗时", f"{avg_elapsed} 秒"])
    ws_summary.append(["有效样本通过率", f"{(valid_pass_rate * 100):.1f}%", "", "有效样本未达标", summary.get("quality_failed_cases", 0)])

    ws_summary.append([])
    ws_summary.append(["核心指标名称", "量化分值 (0-1.00)", "健康度评级", "指标计算逻辑", "改进优化指引"])
    
    summary_header_row = 10
    for col in range(1, 6):
        cell = ws_summary.cell(row=summary_header_row, column=col)
        cell.font = font_section
        cell.fill = fill_dataset
        cell.alignment = align_center

    avg_s = summary.get("average_scores", {})
    metrics_data = [
        ("RAG 三元组综合分 (Harmonic Mean)", avg_s.get("rag_triad_score", 0.0), "系统综合质量", "忠实度、相关性与精确度的调和平均数，单项低分将严厉下拉"),
        ("事实忠实度 (Faithfulness)", avg_s.get("faithfulness", 0.0), "抗幻觉能力", "回答中的原子声明被检索切片证据完全支撑的百分比 (无幻觉)"),
        ("答案相关性 (Answer Relevance)", avg_s.get("answer_relevance", 0.0), "答题切题度", "生成的回答是否直击用户提问核心，无答非所问与冗余套话"),
        ("上下文精确度 (Context Precision)", avg_s.get("context_precision", 0.0), "信噪比表现", "实际检索返回的切片片段中，对答题有真实贡献的信息占比"),
        ("上下文召回率 (Context Recall)", avg_s.get("context_recall") or 0.0, "信息覆盖率", "知识库标准切片中的核心事实被系统成功检索召回的比率")
    ]

    for m_name, val, dim, desc in metrics_data:
        grade = "卓越 (优秀)" if val >= 0.85 else ("良好 (达标)" if val >= 0.70 else "需优化告警")
        guide = "保持当前检索配置" if val >= 0.85 else ("建议调高 Top-K 或优化切片语义分块" if "召回" in m_name else "建议增强 Prompt 事实约束并剔除冗余噪音")
        ws_summary.append([m_name, f"{val:.4f}", grade, dim + ": " + desc, guide])

    for r in range(11, 16):
        val_cell = ws_summary.cell(row=r, column=2)
        score_f = float(val_cell.value)
        val_cell.fill = fill_pass if score_f >= 0.80 else fill_fail
        for c in range(1, 6):
            ws_summary.cell(row=r, column=c).border = border_thin
            ws_summary.cell(row=r, column=c).alignment = align_center if c in [2, 3] else align_left

    ws_summary.column_dimensions["A"].width = 34
    ws_summary.column_dimensions["B"].width = 20
    ws_summary.column_dimensions["C"].width = 18
    ws_summary.column_dimensions["D"].width = 45
    ws_summary.column_dimensions["E"].width = 35

    # 保存时间戳报告
    saved_paths = []
    try:
        wb.save(output_excel_path)
        saved_paths.append(output_excel_path)
    except Exception as e:
        print(f"⚠️ 保存时间戳 Excel 提示: {e}")

    # 同时同步保存一份为固定的“基准评测集_带量化指标.xlsx”，方便用户一键打开
    inplace_excel_path = os.path.join(BENCHMARK_DIR, "基准评测集_带量化指标.xlsx")
    if os.path.abspath(output_excel_path) != os.path.abspath(inplace_excel_path):
        try:
            wb.save(inplace_excel_path)
            saved_paths.append(inplace_excel_path)
        except Exception as e:
            print(f"⚠️ 同步固定基准评测集 Excel 提示 (可能被外部软件打开独占): {e}")

    # 保存一份最新 JSON
    try:
        with open(LATEST_REPORT_JSON, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存最新 JSON 提示: {e}")

    return output_excel_path


# ==============================================================================
# 五、 终端炫彩结果展示卡片
# ==============================================================================

def print_terminal_summary_card(summary: Dict[str, Any]):
    """在终端输出精致的评测报告卡片"""
    s = summary.get("average_scores") or {}
    p = summary.get("performance") or {}

    bar_len = 25
    def make_bar(score: float) -> str:
        filled = int(score * bar_len)
        return "█" * filled + "░" * (bar_len - filled)

    total_c = summary.get('total_cases', len(summary.get('case_results', [])))
    valid_c = summary.get('valid_cases', total_c)
    passed_c = summary.get('passed_cases', 0)
    pass_r = summary.get('pass_rate', 0.0)
    valid_pass_r = summary.get('valid_pass_rate', pass_r)
    invalid_c = summary.get('invalid_cases', 0)
    total_time = p.get('total_elapsed_seconds', summary.get('elapsed_seconds', 0.0))
    avg_time = p.get('average_case_seconds', 0.0)

    print("\n" + "=" * 75)
    print(" 📊  Agentic RAG 企业级评测总览看板 (Ragas Benchmark Report)")
    print("=" * 75)
    print(f" ⏱️ 评测时间: {summary.get('timestamp', '--')}  |  考生模型: {summary.get('model_b_system', 'qwen3.7-plus')}  |  裁判模型: {summary.get('model_c_judge', 'qwen3.8-max-0902')}")
    print(f" 📈 全量通过率: {passed_c}/{total_c} ({pass_r*100:.1f}%)"
          f"  |  有效样本通过率: {passed_c}/{valid_c} ({valid_pass_r*100:.1f}%)")
    if invalid_c:
        error_breakdown = summary.get("error_breakdown") or {}
        reason_text = ", ".join(f"{k}={v}" for k, v in error_breakdown.items()) or "unknown"
        print(f" ⚠️ 流程故障: {invalid_c} 题 ({reason_text})，质量均值已排除故障样本")
    print(f" ⏱️ 总耗时: {total_time}s  |  单题平均: {avg_time}s")
    print("-" * 75)
    print(f" 🎯 RAG 三元组调和得分: [{make_bar(s['rag_triad_score'])}]  {s['rag_triad_score']:.4f}")
    print(f" 🛡️ 事实忠实度 (无幻觉): [{make_bar(s['faithfulness'])}]  {s['faithfulness']:.4f}")
    print(f" 🎯 答案相关性 (无冗余): [{make_bar(s['answer_relevance'])}]  {s['answer_relevance']:.4f}")
    print(f" 🔍 上下文检索精确度  : [{make_bar(s['context_precision'])}]  {s['context_precision']:.4f}")
    if s["context_recall"] is not None:
        print(f" 📚 上下文检索召回率  : [{make_bar(s['context_recall'])}]  {s['context_recall']:.4f}")
    print("=" * 75 + "\n")


# ==============================================================================
# 六、 CLI 命令行入口
# ==============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agentic RAG 高并发自动化基准评测运行器")
    parser.add_argument("--excel", default=DEFAULT_BENCHMARK_EXCEL, help="人工审核后的基准测试集 Excel 路径")
    parser.add_argument("--model-b", default=ALI_DEFAULT_ANSWER_MODEL, help="模型 B (生成答案系统模型)")
    parser.add_argument("--model-c", default=ALI_DEFAULT_JUDGE_MODEL, help="模型 C (独立裁判客观模型，默认使用 qwen3.8-max-0902)")
    parser.add_argument("--workers", type=int, default=2, help="并发执行线程数 (推荐 2，平稳抗限流)")
    parser.add_argument("-q", "--quick", action="store_true", help="极速冒烟测试模式 (仅测前 3 题，~8秒极速反馈)")
    parser.add_argument("--limit", type=int, default=None, help="限制评测题数")
    parser.add_argument("--retry-failed", action="store_true", help="仅重跑上一轮评测未达标的错题")

    args = parser.parse_args()

    # 1. 加载审核后的题库
    print(f"📂 正在从 Excel 读取基准测试题库: {args.excel}")
    all_cases = load_reviewed_cases(args.excel)
    if not all_cases:
        print("❌ 未在表格中读取到有效测试用例，请先运行 dataset_generator.py 生成。")
        sys.exit(1)

    # 2. 筛选用例
    cases_to_run = all_cases
    if args.retry_failed and os.path.exists(LATEST_REPORT_JSON):
        try:
            with open(LATEST_REPORT_JSON, "r", encoding="utf-8") as f:
                prev_report = json.load(f)
            failed_ids = {r["id"] for r in prev_report.get("case_results", []) if not r.get("is_passed", True)}
            cases_to_run = [c for c in all_cases if c["id"] in failed_ids]
            print(f"🔄 [错题重跑模式] 从上一轮报告中锁定 {len(cases_to_run)} 道未通过题目。")
        except Exception:
            pass

    workers = args.workers
    if args.quick:
        cases_to_run = cases_to_run[:3]
        workers = min(2, len(cases_to_run))
        print("⚡ [极速冒烟模式启用] 仅评测前 3 道具有代表性的核心题用作快速验证。")
    elif args.limit:
        cases_to_run = cases_to_run[:args.limit]
        workers = min(workers, len(cases_to_run))

    # 3. 启动高并发评测
    summary = run_benchmark_concurrent(
        cases=cases_to_run,
        model_b=args.model_b,
        model_c=args.model_c,
        max_workers=workers
    )

    # 4. 导出 Excel 报告
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_excel = os.path.join(BENCHMARK_DIR, f"评测报告_{ts}.xlsx")
    export_evaluation_report(summary, out_excel)

    # 5. 打印终端卡片
    print_terminal_summary_card(summary)
    print(f"🎉 评测全部完成！")
    print(f"📊 综合 Excel 报表已生成: {out_excel}")
    print(f"📝 Web 看板最新数据: {LATEST_REPORT_JSON}")
