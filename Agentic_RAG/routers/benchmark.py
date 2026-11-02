"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
子路由模块：工业级五阶段基准评测流水线 (routers/benchmark.py)

【模块定位】
包含完整的五阶段评测中枢 API：
- 阶段一：qwen3.8-max 靶向业务知识库自动命题
- 阶段二：黄金题库审题修正与待审核 Excel/JSON 导出
- 阶段三：考生系统 (qwen3.8-flash + gte-rerank-v2) 批量客观作答与作答快照持久化
- 阶段四：裁判模型 (qwen3.8-max) 深度原子反思与 Ragas 四维量化去幻觉打分
- 阶段五：21 列完整量化报表同步写盘 (防刷新丢数据) 与即时下载

【铁律规范】
- 出题 & 独立裁判：必须且仅使用 qwen3.8-max
- 考生作答：必须且仅使用 qwen3.8-flash
- 重排引擎：统一由阿里百炼云端 gte-rerank-v2 提供
- 批处理循环中注入 await asyncio.sleep(0.02) 彻底杜绝协程假死
================================================================================
"""

import os
import json
import time
import asyncio
from typing import List, Dict, Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse

CURRENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LATEST_ANSWERS_JSON = os.path.join(CURRENT_DIR, "benchmarks", "最新作答记录.json")

from agent import get_agent_service, is_agent_stop_output, build_agent_failure_answer
from evaluator import get_ragas_evaluator
from models import (
    ALI_DEFAULT_ANSWER_MODEL,
    ALI_DEFAULT_JUDGE_MODEL,
    DEFAULT_RERANK_MODEL,
    OLLAMA_EMBEDDING_MODEL
)
from dataset_generator import (
    parse_guide_markdown,
    generate_benchmark_from_docs,
    export_to_excel,
    DOCS_DIR
)
from benchmark_runner import (
    load_reviewed_cases,
    export_evaluation_report,
    build_benchmark_summary,
    LATEST_REPORT_JSON
)
from schemas import (
    BenchmarkGenerateRequest,
    BenchmarkSaveCasesRequest,
    BenchmarkRunAnswersRequest,
    BenchmarkRunJudgeRequest,
    SyncExportRequest
)

router = APIRouter(tags=["基准评测流水线"])


# ==============================================================================
# 一、 评测报告持久化快照与自愈恢复接口
# ==============================================================================

@router.get("/api/eval/latest-report", summary="获取最新一轮并发评测综合报告 (自愈快照)")
@router.get("/api/benchmark/latest-report", summary="获取最新一轮并发评测综合报告")
async def get_latest_eval_report():
    """读取 benchmarks/最新评测报告.json，供前端页面自愈恢复"""
    report_file = os.path.join(CURRENT_DIR, "benchmarks", "最新评测报告.json")
    if not os.path.exists(report_file):
        return {"code": 404, "message": "尚未执行过基准评测，暂无报告"}
    try:
        with open(report_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"code": 200, "data": {"summary": data}}
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


# ==============================================================================
# 二、 企业级五阶段全流程评测流水线核心 API (/api/benchmark/...)
# ==============================================================================

@router.get("/api/benchmark/config", summary="获取评测中枢动态模型配置")
async def get_benchmark_config():
    """
    提供前端评测中枢动态模型元数据，实现 models.py 一处修改，评测页面与调用 100% 同步热更新
    """
    return {
        "code": 200,
        "data": {
            "answer_model": ALI_DEFAULT_ANSWER_MODEL,
            "judge_model": ALI_DEFAULT_JUDGE_MODEL,
            "rerank_model": DEFAULT_RERANK_MODEL,
            "embedding_model": OLLAMA_EMBEDDING_MODEL
        }
    }


@router.get("/api/benchmark/cases", summary="获取当前待审核基准测试集用例列表")
async def get_benchmark_cases():
    """读取已存在的题库，若不存在则从精选指南自动解析"""
    excel_path = os.path.join(CURRENT_DIR, "benchmarks", "待审核基准测试集.xlsx")
    json_path = os.path.join(CURRENT_DIR, "benchmarks", "待审核基准测试集.json")

    cases = []
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                cases = json.load(f)
        except Exception:
            pass

    if not cases and os.path.exists(excel_path):
        try:
            cases = load_reviewed_cases(excel_path)
        except Exception:
            pass

    if not cases:
        cases = parse_guide_markdown(case_count=case_count)
        if cases:
            export_to_excel(cases, excel_path)
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(cases, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return {"code": 200, "data": cases}


@router.post("/api/benchmark/generate-cases", summary="让 qwen3.8-max 智能生成定向测试题")
async def generate_benchmark_cases(req: BenchmarkGenerateRequest):
    """
    【阶段一】调用 qwen3.8-max 派生考题或快速装载指南用例
    """
    model_a = req.model_a or "qwen3.8-max"
    case_count = int(req.case_count or 10)
    if case_count <= 0:
        case_count = 10
    json_path = os.path.join(CURRENT_DIR, "benchmarks", "待审核基准测试集.json")
    excel_path = os.path.join(CURRENT_DIR, "benchmarks", "待审核基准测试集.xlsx")
    doc_dir_label = os.path.relpath(DOCS_DIR, CURRENT_DIR).replace(os.sep, "/")
    mode_label = "精选题库导入" if req.mode == "from-guide" else "知识库自动命题"
    started_at = time.perf_counter()

    print(f"\n[评测 1/5] 出题 | {mode_label}")
    if req.mode == "from-guide":
        print(f"  目标 {case_count} 题 | 来源 {doc_dir_label}")
    else:
        print(f"  模型 {model_a} | 目标 {case_count} 题 | 目录 {doc_dir_label}")

    if req.mode == "from-guide":
        cases = parse_guide_markdown(case_count=case_count)
    else:
        cases = generate_benchmark_from_docs(docs_dir=DOCS_DIR, model_a=model_a, max_workers=2, case_count=case_count)

    if not cases:
        print("  ✗ 出题失败 | 未生成题目，请检查文档目录或模型 API 连通性")
        return JSONResponse(status_code=500, content={"code": 500, "message": "未能生成考题，请检查文档目录与模型连通性"})

    export_to_excel(cases, excel_path)
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(cases, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    output_label = os.path.relpath(excel_path, CURRENT_DIR).replace(os.sep, "/")
    elapsed = time.perf_counter() - started_at
    print(f"  已保存: {output_label} (Excel + JSON)")
    print(f"[评测 1/5] 完成 | {len(cases)} 题 | {elapsed:.1f}s | 下一步: 审核题库\n")
    return {"code": 200, "data": cases}


@router.post("/api/benchmark/save-cases", summary="保存用户在页面上编辑后的题库修改")
async def save_benchmark_cases(req: BenchmarkSaveCasesRequest):
    """
    【阶段二】接收前端编辑后的测试题列表，持久化写回 Excel
    """
    excel_path = os.path.join(CURRENT_DIR, "benchmarks", "待审核基准测试集.xlsx")
    try:
        export_to_excel(req.cases, excel_path)
        print(f"[评测 2/5] 题库已保存 | {len(req.cases)} 题 | benchmarks/待审核基准测试集.xlsx")
        return {"code": 200, "message": "题库修改已成功保存至 Excel"}
    except Exception as e:
        print(f"  ✗ 题库保存失败 | {e}")
        return JSONResponse(status_code=500, content={"code": 500, "message": f"保存失败: {str(e)}"})


@router.get("/api/benchmark/export-cases", summary="导出待审核基准测试集 Excel 文件")
async def export_benchmark_cases():
    """
    【阶段二】下载当前待审核基准测试集.xlsx
    """
    excel_path = os.path.join(CURRENT_DIR, "benchmarks", "待审核基准测试集.xlsx")
    if not os.path.exists(excel_path):
        cases = parse_guide_markdown(case_count=case_count)
        export_to_excel(cases, excel_path)
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(cases, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return FileResponse(
        path=excel_path,
        filename="Agentic_RAG_待审核基准测试集.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def _prepare_target_cases(limit: Optional[int]) -> List[Dict[str, Any]]:
    """加载待作答的通过状态考题"""
    excel_path = os.path.join(CURRENT_DIR, "benchmarks", "待审核基准测试集.xlsx")
    json_path = os.path.join(CURRENT_DIR, "benchmarks", "待审核基准测试集.json")

    cases = []
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                cases = json.load(f)
        except Exception:
            pass

    if not cases and os.path.exists(excel_path):
        cases = load_reviewed_cases(excel_path)
    if not cases:
        cases = parse_guide_markdown(case_count=case_count)

    valid_cases = [c for c in cases if not any(r in str(c.get("review_status", "")) for r in ["剔除", "删除", "废弃", "不通过"])]
    if not valid_cases:
        valid_cases = cases

    if limit and limit > 0:
        return valid_cases[:limit]
    return valid_cases


@router.post("/api/benchmark/run-answers-stream", summary="考生系统批量作答 (SSE 实时流式推流)")
async def run_benchmark_answers_stream(req: BenchmarkRunAnswersRequest):
    """
    【阶段三 4并发异步流式】考生系统开启 4 并发工作池，多题同时检索与作答，哪道做完哪道即刻 SSE 上屏！
    """
    target_cases = _prepare_target_cases(req.limit)
    agent_service = get_agent_service()
    model_b = req.model_b or ALI_DEFAULT_ANSWER_MODEL
    total = len(target_cases)
    concurrency_limit = 4

    print(f"\n[评测 3/5] 作答 | 题目 {total} | 模型 {model_b} | 并发 {concurrency_limit}")

    async def event_generator():
        sem = asyncio.Semaphore(concurrency_limit)
        queue = asyncio.Queue()

        async def worker(idx, c):
            async with sem:
                q = c["query"]
                preview_q = (q[:35] + "...") if len(q) > 35 else q
                print(f"  [{idx+1}/{total}] 开始 | {preview_q}")

                await queue.put(("progress", {
                    "current": idx + 1,
                    "total": total,
                    "query": q,
                    "category": c.get("category")
                }))

                t0 = time.time()
                try:
                    # 使用 asyncio.to_thread 在后台工作线程池并发调用，彻底释放主协程事件循环
                    qa_res = await asyncio.to_thread(agent_service.ask, user_query=q, model_name=model_b)
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
                latency = round(time.time() - t0, 2)

                ans_preview = ans.replace("\n", " ").strip()
                ans_preview = (ans_preview[:40] + "...") if len(ans_preview) > 40 else ans_preview
                if qa_success:
                    print(f"  [{idx+1}/{total}] 完成 | {latency}s | {ans_preview}")
                else:
                    print(f"  [{idx+1}/{total}] 系统故障 | {qa_error_type or 'agent_error'} | 不计分")

                item = {
                    "id": c.get("id"),
                    "category": c.get("category", "综合业务推理"),
                    "query": q,
                    "ground_truth": c.get("reviewed_ground_truth") or c.get("ground_truth") or "",
                    "raw_ground_truth": c.get("ground_truth") or "",
                    "source_chunks": c.get("source_chunks", ""),
                    "source_doc": c.get("source_doc", ""),
                    "review_status": c.get("review_status", "通过"),
                    "reviewed_gt": c.get("reviewed_ground_truth", ""),
                    "review_comment": c.get("review_notes", ""),
                    "system_answer": ans,
                    "contexts": ctxs,
                    "contexts_count": len(ctxs),
                    "contexts_preview": "\n---\n".join([f"[{i+1}] {ctx[:200]}" for i, ctx in enumerate(ctxs[:3])]),
                    "execution_status": "success" if qa_success else "failed",
                    "is_valid": qa_success,
                    "error_stage": None if qa_success else "agent",
                    "error_type": qa_error_type if not qa_success else None,
                    "retry_recommended": not qa_success,
                    "model_b": model_b,
                    "latency": latency,
                    "orig_idx": idx
                }
                await queue.put(("case_done", item))

        # 启动所有协程任务
        workers = [asyncio.create_task(worker(i, c)) for i, c in enumerate(target_cases)]

        answered_results = []
        completed_count = 0

        while completed_count < total:
            event_type, payload = await queue.get()
            if event_type == "progress":
                yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            elif event_type == "case_done":
                completed_count += 1
                answered_results.append(payload)
                yield f"event: case_done\ndata: {json.dumps({'current': completed_count, 'total': total, 'case': payload}, ensure_ascii=False)}\n\n"

        await asyncio.gather(*workers)

        # 依题目原始索引保序排列
        answered_results.sort(key=lambda x: x.get("orig_idx", 0))
        for item in answered_results:
            item.pop("orig_idx", None)

        try:
            with open(LATEST_ANSWERS_JSON, "w", encoding="utf-8") as f:
                json.dump(answered_results, f, ensure_ascii=False, indent=2)
            print(f"[评测 3/5] 完成 | {len(answered_results)} 题 | 已保存 benchmarks/最新作答记录.json\n")
        except Exception as e:
            print(f"  ✗ 作答记录保存失败 | {e}\n")

        yield f"event: all_done\ndata: {json.dumps({'total': len(answered_results), 'data': answered_results}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.post("/api/benchmark/run-answers", summary="考生系统批量检索并生成答案 (同步兼容)")
async def run_benchmark_answers(req: BenchmarkRunAnswersRequest):
    """
    【阶段三 同步兼容】
    """
    target_cases = _prepare_target_cases(req.limit)
    agent_service = get_agent_service()
    model_b = req.model_b or ALI_DEFAULT_ANSWER_MODEL
    answered_results = []

    print(f"\n[评测 3/5] 作答 | 题目 {len(target_cases)} | 模型 {model_b} | 串行")

    for idx, c in enumerate(target_cases):
        await asyncio.sleep(0.02)
        q = c["query"]
        t0 = time.time()
        try:
            qa_res = agent_service.ask(user_query=q, model_name=model_b)
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
        latency = round(time.time() - t0, 2)
        print(f"  [{idx+1}/{len(target_cases)}] {'完成' if qa_success else '系统故障'} | {latency}s")

        answered_results.append({
            "id": c.get("id"),
            "category": c.get("category", "综合业务推理"),
            "query": q,
            "ground_truth": c.get("reviewed_ground_truth") or c.get("ground_truth") or "",
            "raw_ground_truth": c.get("ground_truth") or "",
            "source_chunks": c.get("source_chunks", ""),
            "source_doc": c.get("source_doc", ""),
            "review_status": c.get("review_status", "通过"),
            "reviewed_gt": c.get("reviewed_ground_truth", ""),
            "review_comment": c.get("review_notes", ""),
            "system_answer": ans,
            "contexts": ctxs,
            "contexts_count": len(ctxs),
            "contexts_preview": "\n---\n".join([f"[{i+1}] {ctx[:200]}" for i, ctx in enumerate(ctxs[:3])]),
            "execution_status": "success" if qa_success else "failed",
            "is_valid": qa_success,
            "error_stage": None if qa_success else "agent",
            "error_type": qa_error_type if not qa_success else None,
            "retry_recommended": not qa_success,
            "model_b": model_b,
            "latency": latency
        })

    try:
        with open(LATEST_ANSWERS_JSON, "w", encoding="utf-8") as f:
            json.dump(answered_results, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ✗ 作答记录保存失败 | {e}\n")

    return {"code": 200, "data": answered_results}


@router.get("/api/benchmark/latest-answers", summary="获取最新一轮考生作答缓存记录")
async def get_latest_benchmark_answers():
    if not os.path.exists(LATEST_ANSWERS_JSON):
        return {"code": 404, "message": "暂无作答缓存记录"}
    try:
        with open(LATEST_ANSWERS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"code": 200, "data": data}
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@router.post("/api/benchmark/run-judge-stream", summary="裁判模型量化评估 (SSE 实时流式推流)")
async def run_benchmark_judge_stream(req: BenchmarkRunJudgeRequest):
    """
    【阶段四 4并发异步流式】裁判模型开启 4 并发裁决，4 道题同时原子反思打分，耗时直降 75%！
    """
    model_c = req.model_c or ALI_DEFAULT_JUDGE_MODEL
    evaluator = get_ragas_evaluator(model_name=model_c)
    total_cases = len(req.answered_cases)
    concurrency_limit = 4

    print(f"\n[评测 4/5] 裁判 | 题目 {total_cases} | 模型 {model_c} | 并发 {concurrency_limit}")

    async def event_generator():
        sem = asyncio.Semaphore(concurrency_limit)
        queue = asyncio.Queue()
        start_all = time.time()

        async def judge_worker(idx, item):
            async with sem:
                q = item["query"]
                ans = item.get("system_answer", "")
                ctxs = item.get("contexts", [])
                gt = item.get("ground_truth") or item.get("raw_ground_truth") or ""
                category = item.get("category", "")

                preview_q = (q[:35] + "...") if len(q) > 35 else q
                print(f"  [{idx+1}/{total_cases}] 开始 | {preview_q}")

                await queue.put(("progress", {
                    "current": idx + 1,
                    "total": total_cases,
                    "stage": "judge",
                    "query": q,
                    "category": category
                }))

                if not bool(item.get("is_valid", True)) or item.get("execution_status") == "failed":
                    judge_latency = 0.0
                    result_item = dict(item)
                    result_item.update({
                        "scores": {},
                        "is_passed": False,
                        "is_valid": False,
                        "evaluation_status": "skipped",
                        "error_stage": item.get("error_stage", "agent"),
                        "error_type": item.get("error_type", "agent_error"),
                        "retry_recommended": True,
                        "qa_latency": item.get("latency", 0.0),
                        "judge_latency": judge_latency,
                        "total_latency": round(float(item.get("latency", 0.0)), 2),
                        "orig_idx": idx
                    })
                    print(f"  [{idx+1}/{total_cases}] 跳过裁判 | 系统故障 | 不计分")
                    await queue.put(("case_done", result_item))
                    return

                t0 = time.time()
                try:
                    # 使用 asyncio.to_thread 并发调用裁判评估
                    eval_res = await asyncio.to_thread(
                        evaluator.evaluate_single,
                        query=q,
                        response=ans,
                        contexts=ctxs,
                        ground_truth=gt,
                        category=category
                    )
                except Exception as e:
                    eval_res = {
                        "scores": {},
                        "is_valid": False,
                        "evaluation_status": "failed",
                        "error_stage": "judge",
                        "error_type": "judge_error",
                        "retry_recommended": True,
                        "diagnoses": [f"裁判调用异常：{str(e)[:160]}"],
                        "prescriptions": ["本题本轮不计分，请在裁判接口恢复后重跑。"]
                    }

                judge_latency = round(time.time() - t0, 2)
                eval_valid = bool(
                    eval_res.get("is_valid", eval_res.get("evaluation_status") == "success")
                )
                scores = eval_res.get("scores", {}) if eval_valid else {}
                triad_score = float(scores.get("rag_triad_score", 0.0) or 0.0)
                is_passed = bool(eval_valid and triad_score >= 0.80)
                status_tag = "通过" if is_passed else ("未通过" if eval_valid else "不计分")

                if eval_valid:
                    f_score = int(float(scores.get("faithfulness", 0.0) or 0.0) * 100)
                    r_score = int(float(scores.get("answer_relevance", 0.0) or 0.0) * 100)
                    p_score = int(float(scores.get("context_precision", 0.0) or 0.0) * 100)
                    print(
                        f"  [{idx+1}/{total_cases}] 完成 | 综合 {triad_score*100:.1f} | "
                        f"忠实 {f_score}% 相关 {r_score}% 精度 {p_score}% | {judge_latency}s | {status_tag}"
                    )
                else:
                    print(
                        f"  [{idx+1}/{total_cases}] 裁判失败 | "
                        f"{eval_res.get('error_type', 'judge_error')} | 不计分 | {judge_latency}s"
                    )

                result_item = {
                    "id": item.get("id"),
                    "category": category,
                    "query": q,
                    "ground_truth": gt,
                    "raw_ground_truth": item.get("raw_ground_truth", gt),
                    "source_chunks": item.get("source_chunks", ""),
                    "source_doc": item.get("source_doc", ""),
                    "review_status": item.get("review_status", "通过"),
                    "reviewed_gt": item.get("reviewed_gt", ""),
                    "review_comment": item.get("review_comment", ""),
                    "system_answer": ans,
                    "contexts_count": item.get("contexts_count", len(ctxs)),
                    "contexts_preview": item.get("contexts_preview", ""),
                    "scores": scores,
                    "is_passed": is_passed,
                    "is_valid": eval_valid,
                    "execution_status": item.get("execution_status", "success"),
                    "evaluation_status": eval_res.get("evaluation_status", "success" if eval_valid else "failed"),
                    "error_stage": None if eval_valid else eval_res.get("error_stage", "judge"),
                    "error_type": None if eval_valid else eval_res.get("error_type", "judge_error"),
                    "retry_recommended": not eval_valid,
                    "diagnoses": eval_res.get("diagnoses", []),
                    "prescriptions": eval_res.get("prescriptions", []),
                    "model_b": item.get("model_b", ALI_DEFAULT_ANSWER_MODEL),
                    "qa_latency": item.get("latency", 0.0),
                    "judge_latency": judge_latency,
                    "total_latency": round(float(item.get("latency", 0.0)) + judge_latency, 2),
                    "orig_idx": idx
                }
                await queue.put(("case_done", result_item))

        judge_tasks = [asyncio.create_task(judge_worker(i, item)) for i, item in enumerate(req.answered_cases)]

        case_results = []
        completed_count = 0

        while completed_count < total_cases:
            event_type, payload = await queue.get()
            if event_type == "progress":
                yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            elif event_type == "case_done":
                completed_count += 1
                case_results.append(payload)
                yield f"event: case_done\ndata: {json.dumps({'current': completed_count, 'total': total_cases, 'case': payload}, ensure_ascii=False)}\n\n"

        await asyncio.gather(*judge_tasks)

        # 题目保序排序
        case_results.sort(key=lambda x: x.get("orig_idx", 0))
        for item in case_results:
            item.pop("orig_idx", None)

        elapsed_total = round(time.time() - start_all, 2)
        model_b = req.answered_cases[0].get("model_b", ALI_DEFAULT_ANSWER_MODEL) if req.answered_cases else ALI_DEFAULT_ANSWER_MODEL
        summary = build_benchmark_summary(
            case_results=case_results,
            model_b=model_b,
            model_c=model_c,
            total_elapsed=elapsed_total,
            max_workers=concurrency_limit
        )

        try:
            report_excel_path = os.path.join(CURRENT_DIR, "benchmarks", "基准评测集_带量化指标.xlsx")
            export_evaluation_report(summary=summary, output_excel_path=report_excel_path)
            with open(LATEST_REPORT_JSON, "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
            print(
                f"[评测 4/5] 完成 | 有效通过 {summary['passed_cases']}/{summary['valid_cases']} "
                f"({summary['valid_pass_rate']*100:.1f}%) | 流程故障 {summary['invalid_cases']} | "
                f"已保存 benchmarks/基准评测集_带量化指标.xlsx\n"
            )
        except Exception as e:
            print(f"  ✗ 评测报表保存失败 | {e}\n")

        yield f"event: all_done\ndata: {json.dumps({'summary': summary, 'case_results': case_results}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.post("/api/benchmark/run-judge", summary="裁判模型 (qwen3.8-max) 量化评估打分与诊断 (同步兼容)")
async def run_benchmark_judge(req: BenchmarkRunJudgeRequest):
    """
    【阶段四 同步兼容】裁判模型按四大题型规则评估并生成 21 列综合报表
    """
    model_c = req.model_c or ALI_DEFAULT_JUDGE_MODEL
    evaluator = get_ragas_evaluator(model_name=model_c)
    case_results = []
    total_cases = len(req.answered_cases)

    print(f"\n[评测 4/5] 裁判 | 题目 {total_cases} | 模型 {model_c} | 串行")

    start_all = time.time()

    for idx, item in enumerate(req.answered_cases):
        await asyncio.sleep(0.02)
        q = item["query"]
        ans = item.get("system_answer", "")
        ctxs = item.get("contexts", [])
        gt = item.get("ground_truth") or item.get("raw_ground_truth") or ""
        category = item.get("category", "")

        preview_q = (q[:35] + "...") if len(q) > 35 else q
        print(f"  [{idx+1}/{total_cases}] 开始 | {preview_q}")

        if not bool(item.get("is_valid", True)) or item.get("execution_status") == "failed":
            case_results.append({
                "id": item.get("id"),
                "category": category,
                "query": q,
                "ground_truth": gt,
                "raw_ground_truth": item.get("raw_ground_truth", gt),
                "source_chunks": item.get("source_chunks", ""),
                "source_doc": item.get("source_doc", ""),
                "review_status": item.get("review_status", "通过"),
                "reviewed_gt": item.get("reviewed_gt", ""),
                "review_comment": item.get("review_comment", ""),
                "system_answer": ans,
                "contexts_count": item.get("contexts_count", len(ctxs)),
                "contexts_preview": item.get("contexts_preview", ""),
                "scores": {},
                "is_passed": False,
                "is_valid": False,
                "execution_status": "failed",
                "evaluation_status": "skipped",
                "error_stage": item.get("error_stage", "agent"),
                "error_type": item.get("error_type", "agent_error"),
                "retry_recommended": True,
                "diagnoses": ["Agent 未能完成有效作答，本题跳过裁判且不计分。"],
                "prescriptions": ["修复作答链路后重跑本题。"],
                "model_b": item.get("model_b", ALI_DEFAULT_ANSWER_MODEL),
                "qa_latency": item.get("latency", 0.0),
                "judge_latency": 0.0,
                "total_latency": round(float(item.get("latency", 0.0)), 2)
            })
            print(f"  跳过裁判 | 系统故障 | 不计分")
            continue

        t0 = time.time()
        try:
            eval_res = evaluator.evaluate_single(
                query=q,
                response=ans,
                contexts=ctxs,
                ground_truth=gt,
                category=category
            )
        except Exception as e:
            eval_res = {
                "scores": {},
                "is_valid": False,
                "evaluation_status": "failed",
                "error_stage": "judge",
                "error_type": "judge_error",
                "retry_recommended": True,
                "diagnoses": [f"裁判调用异常：{str(e)[:160]}"],
                "prescriptions": ["本题本轮不计分，请在裁判接口恢复后重跑。"]
            }

        judge_latency = round(time.time() - t0, 2)
        eval_valid = bool(eval_res.get("is_valid", eval_res.get("evaluation_status") == "success"))
        scores = eval_res.get("scores", {}) if eval_valid else {}
        triad_score = float(scores.get("rag_triad_score", 0.0) or 0.0)
        is_passed = bool(eval_valid and triad_score >= 0.80)

        if eval_valid:
            f_score = int(float(scores.get("faithfulness", 0.0) or 0.0) * 100)
            r_score = int(float(scores.get("answer_relevance", 0.0) or 0.0) * 100)
            p_score = int(float(scores.get("context_precision", 0.0) or 0.0) * 100)
            print(f"  完成 | 综合 {triad_score*100:.1f} | 忠实 {f_score}% 相关 {r_score}% 精度 {p_score}% | {'通过' if is_passed else '未通过'}")
        else:
            print(f"  裁判失败 | {eval_res.get('error_type', 'judge_error')} | 不计分")

        case_results.append({
            "id": item.get("id"),
            "category": category,
            "query": q,
            "ground_truth": gt,
            "raw_ground_truth": item.get("raw_ground_truth", gt),
            "source_chunks": item.get("source_chunks", ""),
            "source_doc": item.get("source_doc", ""),
            "review_status": item.get("review_status", "通过"),
            "reviewed_gt": item.get("reviewed_gt", ""),
            "review_comment": item.get("review_comment", ""),
            "system_answer": ans,
            "contexts_count": item.get("contexts_count", len(ctxs)),
            "contexts_preview": item.get("contexts_preview", ""),
            "scores": scores,
            "is_passed": is_passed,
            "is_valid": eval_valid,
            "execution_status": item.get("execution_status", "success"),
            "evaluation_status": eval_res.get("evaluation_status", "success" if eval_valid else "failed"),
            "error_stage": None if eval_valid else eval_res.get("error_stage", "judge"),
            "error_type": None if eval_valid else eval_res.get("error_type", "judge_error"),
            "retry_recommended": not eval_valid,
            "diagnoses": eval_res.get("diagnoses", []),
            "prescriptions": eval_res.get("prescriptions", []),
            "model_b": item.get("model_b", ALI_DEFAULT_ANSWER_MODEL),
            "qa_latency": item.get("latency", 0.0),
            "judge_latency": judge_latency,
            "total_latency": round(float(item.get("latency", 0.0)) + judge_latency, 2)
        })

    elapsed_total = round(time.time() - start_all, 2)
    model_b = req.answered_cases[0].get("model_b", ALI_DEFAULT_ANSWER_MODEL) if req.answered_cases else ALI_DEFAULT_ANSWER_MODEL
    summary = build_benchmark_summary(
        case_results=case_results,
        model_b=model_b,
        model_c=model_c,
        total_elapsed=elapsed_total
    )

    try:
        report_excel_path = os.path.join(CURRENT_DIR, "benchmarks", "基准评测集_带量化指标.xlsx")
        export_evaluation_report(summary=summary, output_excel_path=report_excel_path)
        with open(LATEST_REPORT_JSON, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(
            f"[评测 4/5] 完成 | 有效通过 {summary['passed_cases']}/{summary['valid_cases']} "
            f"({summary['valid_pass_rate']*100:.1f}%) | 流程故障 {summary['invalid_cases']} | "
            f"已保存 benchmarks/基准评测集_带量化指标.xlsx\n"
        )
    except Exception as e:
        print(f"  ✗ 评测报表保存失败 | {e}\n")

    return {
        "code": 200,
        "data": {
            "summary": summary,
            "case_results": case_results
        }
    }


@router.post("/api/benchmark/sync-and-export", summary="前端同步当前最新评测报告并触发即时写入")
async def sync_and_export_report(req: SyncExportRequest):
    """
    【阶段五】接收前端当前实际展示的最新评测数据，立即重新生成 Excel 报表与 JSON 文件
    """
    try:
        summary = req.summary
        if not summary or not summary.get("case_results"):
            return JSONResponse(status_code=400, content={"code": 400, "message": "评测数据为空"})

        report_excel_path = os.path.join(CURRENT_DIR, "benchmarks", "基准评测集_带量化指标.xlsx")
        export_evaluation_report(summary=summary, output_excel_path=report_excel_path)
        with open(LATEST_REPORT_JSON, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        print(f"[评测 5/5] 报表已同步 | {len(summary.get('case_results', []))} 条 | benchmarks/基准评测集_带量化指标.xlsx")
        return {"code": 200, "message": "报表同步成功"}
    except Exception as e:
        print(f"  ✗ 报表同步失败 | {e}")
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@router.get("/api/benchmark/download-report", summary="下载最新的 21 列基准评测报告 Excel 表格")
async def download_benchmark_report():
    """
    【阶段五】下载基准评测集_带量化指标.xlsx (强制防浏览器缓存)
    """
    excel_path = os.path.join(CURRENT_DIR, "benchmarks", "基准评测集_带量化指标.xlsx")
    if not os.path.exists(excel_path):
        return JSONResponse(status_code=404, content={"code": 404, "message": "尚未生成量化评测报告"})

    timestamp_filename = f"Agentic_RAG_基准评测集_带量化指标_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
    return FileResponse(
        path=excel_path,
        filename=timestamp_filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )
