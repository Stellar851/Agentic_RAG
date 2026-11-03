"""
================================================================================
企业级工业评测中台微服务 (Evaluation Platform)
主服务接入与评测调度中枢 (app.py)

【微服务定位】
端口: 8002
职责:
1. 提供独立的评测看板工作台 Web UI (http://localhost:8002/)；
2. 调度五阶段自动化评测流水线：
   - 阶段一：qwen3.8-max 深入知识库自动命题
   - 阶段二：黄金题库人工审核与 Excel/JSON 导出持久化
   - 阶段三：作为独立考场，通过 HTTP 协议向目标 Agent (如 http://localhost:8000/api/chat) 并发派题收集作答
   - 阶段四：独立裁判 qwen3.8-max 深度反思打分 (Ragas 四维指标 + 根因诊断处方)
   - 阶段五：即时写盘防丢与 21 列综合指标 Excel 报表编译下载
3. 彻底实现评测与业务解耦 (Eval-as-a-Service)，可评测任意外部业务 Agent。
================================================================================
"""

import os
import sys
import json
import time
import asyncio
import traceback
from typing import List, Dict, Any, Optional

import httpx
from fastapi import FastAPI, UploadFile, File, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

BENCHMARK_DIR = os.path.join(CURRENT_DIR, "benchmarks")
os.makedirs(BENCHMARK_DIR, exist_ok=True)

LATEST_ANSWERS_JSON = os.path.join(BENCHMARK_DIR, "最新作答记录.json")
LATEST_REPORT_JSON = os.path.join(BENCHMARK_DIR, "最新评测报告.json")
BENCHMARK_HTML_PATH = os.path.join(CURRENT_DIR, "templates", "benchmark.html")

# 被测 Agent 服务的默认地址 (可通过环境变量 AGENT_SERVICE_URL 动态配置)
DEFAULT_AGENT_CHAT_URL = os.getenv("AGENT_SERVICE_URL", "http://localhost:8000/api/chat")

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
    build_benchmark_summary
)
from schemas import (
    BenchmarkGenerateRequest,
    BenchmarkSaveCasesRequest,
    BenchmarkRunAnswersRequest,
    BenchmarkRunJudgeRequest,
    SyncExportRequest
)

app = FastAPI(
    title="企业级工业评测中台微服务 (Evaluation Platform)",
    description="五阶段自动化全流程基准评测流水线与质量量化中枢 (Eval-as-a-Service)",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态资源
app.mount("/static", StaticFiles(directory=os.path.join(CURRENT_DIR, "static")), name="static")


# ==============================================================================
# 一、 评测看板首页与健康检查
# ==============================================================================

@app.get("/", response_class=HTMLResponse, summary="评测工作台中枢首页")
async def benchmark_index():
    """读取并渲染评测中枢工作台前端页面 benchmark.html"""
    if not os.path.exists(BENCHMARK_HTML_PATH):
        raise HTTPException(status_code=404, detail="benchmark.html 页面未找到")
    with open(BENCHMARK_HTML_PATH, "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)


@app.get("/api/eval/health", summary="评测中台健康状态")
async def health():
    return {
        "code": 200,
        "service": "Evaluation Platform Microservice",
        "port": 8002,
        "target_agent_url": DEFAULT_AGENT_CHAT_URL,
        "docs_dir": DOCS_DIR
    }


# ==============================================================================
# 二、 评测报告持久化快照与配置接口
# ==============================================================================

@app.get("/api/eval/latest-report", summary="获取最新一轮并发评测综合报告")
@app.get("/api/benchmark/latest-report", summary="获取最新评测报告快照 (用于前端自愈)")
async def get_latest_eval_report():
    if not os.path.exists(LATEST_REPORT_JSON):
        return {"code": 404, "message": "尚未执行过基准评测，暂无报告"}
    try:
        with open(LATEST_REPORT_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"code": 200, "data": {"summary": data}}
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@app.get("/api/benchmark/latest-answers", summary="获取最新考生作答记录快照")
@app.get("/api/eval/latest-answers", summary="获取最新考生作答记录快照 (兼容路由)")
async def get_latest_answered_cases():
    if not os.path.exists(LATEST_ANSWERS_JSON):
        return {"code": 404, "message": "尚未执行过考生作答，暂无作答记录"}
    try:
        with open(LATEST_ANSWERS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data:
            if "system_answer" not in item:
                item["system_answer"] = item.get("answer", "")
            if "contexts_count" not in item:
                item["contexts_count"] = len(item.get("contexts", []))
        return {"code": 200, "data": data}
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@app.get("/api/benchmark/config", summary="获取评测中枢动态模型配置")
async def get_benchmark_config():
    return {
        "code": 200,
        "data": {
            "answer_model": ALI_DEFAULT_ANSWER_MODEL,
            "judge_model": ALI_DEFAULT_JUDGE_MODEL,
            "rerank_model": DEFAULT_RERANK_MODEL,
            "embedding_model": OLLAMA_EMBEDDING_MODEL,
            "agent_url": DEFAULT_AGENT_CHAT_URL
        }
    }


@app.get("/api/benchmark/cases", summary="获取当前待审核基准测试集用例列表")
async def get_benchmark_cases():
    excel_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.xlsx")
    json_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.json")

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
        cases = parse_guide_markdown(case_count=10)
        if cases:
            export_to_excel(cases, excel_path)
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(cases, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return {"code": 200, "data": cases}


# ==============================================================================
# 三、 阶段一：自动命题与题库装载
# ==============================================================================

@app.post("/api/benchmark/generate-cases", summary="阶段一：命题专家派生考题")
async def generate_benchmark_cases(req: BenchmarkGenerateRequest):
    model_a = req.model_a or "qwen3.8-max"
    case_count = int(req.case_count or 10)
    if case_count <= 0:
        case_count = 10

    json_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.json")
    excel_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.xlsx")
    started_at = time.perf_counter()

    if req.mode == "from-guide":
        cases = parse_guide_markdown(case_count=case_count)
    else:
        cases = generate_benchmark_from_docs(
            docs_dir=DOCS_DIR,
            model_name=model_a,
            max_cases=case_count
        )

    export_to_excel(cases, excel_path)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=2)

    elapsed = round(time.perf_counter() - started_at, 2)
    return {
        "code": 200,
        "message": f"成功生成并持久化 {len(cases)} 道考题 (耗时 {elapsed}s)",
        "data": cases
    }


# ==============================================================================
# 四、 阶段二：审题与题库保存
# ==============================================================================

@app.post("/api/benchmark/save-cases", summary="阶段二：审题修正后保存持久化")
async def save_reviewed_cases(req: BenchmarkSaveCasesRequest):
    cases = req.cases
    json_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.json")
    excel_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.xlsx")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=2)
    export_to_excel(cases, excel_path)

    return {"code": 200, "message": f"成功保存 {len(cases)} 道已审核测试集"}


# ==============================================================================
# 五、 阶段三：考生批量作答 (通过 HTTP 远程调用业务 Agent)
# ==============================================================================

def _prepare_target_cases(limit: Optional[int]) -> List[Dict[str, Any]]:
    excel_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.xlsx")
    json_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.json")
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
        cases = parse_guide_markdown(case_count=10)

    valid_cases = [c for c in cases if not any(r in str(c.get("review_status", "")) for r in ["剔除", "删除", "废弃", "不通过"])]
    if not valid_cases:
        valid_cases = cases
    if limit and limit > 0:
        return valid_cases[:limit]
    return valid_cases


async def _call_agent_http(query: str, model_name: str, agent_url: str = DEFAULT_AGENT_CHAT_URL) -> Dict[str, Any]:
    """通过 HTTP 协议向 Agent 服务发送提问 (解耦微服务)"""
    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            resp = await client.post(
                agent_url,
                json={"query": query, "model_name": model_name, "stream": False}
            )
            if resp.status_code == 200:
                payload = resp.json()
                data = payload.get("data", {})
                return {
                    "answer": data.get("answer", ""),
                    "contexts": data.get("referenced_docs", []),
                    "steps": data.get("steps", []),
                    "success": True,
                    "error_type": None
                }
            else:
                return {
                    "answer": f"Agent 服务响应异常 (HTTP {resp.status_code})",
                    "contexts": [],
                    "steps": [],
                    "success": False,
                    "error_type": f"http_{resp.status_code}"
                }
        except httpx.ConnectError:
            return {
                "answer": f"无法连接业务 Agent (请确认 Agent 服务在 {agent_url} 正常运行)",
                "contexts": [],
                "steps": [],
                "success": False,
                "error_type": "agent_connection_error"
            }
        except httpx.TimeoutException as e:
            return {
                "answer": f"调用 Agent 超时 (超过 180s 限制): {type(e).__name__}",
                "contexts": [],
                "steps": [],
                "success": False,
                "error_type": "agent_timeout"
            }
        except Exception as e:
            err_detail = f"{type(e).__name__}: {str(e)}" if str(e) else type(e).__name__
            return {
                "answer": f"调用 Agent 异常: {err_detail}",
                "contexts": [],
                "steps": [],
                "success": False,
                "error_type": "agent_exception"
            }


@app.post("/api/benchmark/run-answers-stream", summary="阶段三：考生批量作答 (SSE 实时推流)")
async def run_benchmark_answers_stream(req: BenchmarkRunAnswersRequest):
    target_cases = _prepare_target_cases(req.limit)
    model_b = req.model_b or ALI_DEFAULT_ANSWER_MODEL
    total = len(target_cases)
    concurrency_limit = 2

    print(f"\n[评测 3/5] 批量作答 | 题目 {total} | 考生模型 {model_b} | 并发 {concurrency_limit}")

    async def event_generator():
        sem = asyncio.Semaphore(concurrency_limit)
        queue = asyncio.Queue()

        async def worker(idx, c):
            async with sem:
                q = c["query"]
                await queue.put(("progress", {
                    "current": idx + 1,
                    "total": total,
                    "query": q,
                    "category": c.get("category")
                }))

                t0 = time.time()
                qa_res = await _call_agent_http(query=q, model_name=model_b)
                latency = round(time.time() - t0, 2)

                item = {
                    "id": c.get("id", idx + 1),
                    "category": c.get("category", "综合业务推理"),
                    "query": q,
                    "ground_truth": c.get("reviewed_ground_truth") or c.get("ground_truth") or "",
                    "source_doc": c.get("source_doc") or "",
                    "answer": qa_res.get("answer", ""),
                    "contexts": qa_res.get("contexts", []),
                    "steps": qa_res.get("steps", []),
                    "model_name": model_b,
                    "latency": latency,
                    "success": qa_res.get("success", False),
                    "error_type": qa_res.get("error_type")
                }
                await queue.put(("done_item", item))

        tasks = [asyncio.create_task(worker(i, c)) for i, c in enumerate(target_cases)]

        completed_count = 0
        all_results = []
        while completed_count < total:
            ev_type, payload = await queue.get()
            if ev_type == "progress":
                yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            elif ev_type == "done_item":
                payload["system_answer"] = payload.get("answer", "")
                payload["contexts_count"] = len(payload.get("contexts", []))
                all_results.append(payload)
                completed_count += 1
                frame_data = {
                    "current": completed_count,
                    "total": total,
                    "case": payload,
                    "item": payload
                }
                yield f"event: case_done\ndata: {json.dumps(frame_data, ensure_ascii=False)}\n\n"
                yield f"event: answer_item\ndata: {json.dumps(frame_data, ensure_ascii=False)}\n\n"

        await asyncio.gather(*tasks)

        # 作答全部完成，保存快照
        with open(LATEST_ANSWERS_JSON, "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)

        yield f"event: all_done\ndata: {json.dumps({'total': total, 'success_count': len(all_results), 'data': all_results}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )


# ==============================================================================
# 六、 阶段四：独立裁判反思量化打分
# ==============================================================================

@app.post("/api/benchmark/run-judge-stream", summary="阶段四：独立裁判反思打分 (SSE 实时推流)")
async def run_benchmark_judge_stream(req: BenchmarkRunJudgeRequest):
    answered_cases = req.answered_cases
    model_c = req.model_c or ALI_DEFAULT_JUDGE_MODEL
    total = len(answered_cases)
    concurrency_limit = 4
    evaluator = get_ragas_evaluator(model_name=model_c)

    print(f"\n[评测 4/5] 独立客观裁判打分 | 题目 {total} | 裁判模型 {model_c}")

    async def event_generator():
        t_start = time.time()
        sem = asyncio.Semaphore(concurrency_limit)
        queue = asyncio.Queue()

        async def worker(idx, c):
            async with sem:
                await queue.put(("progress", {"current": idx + 1, "total": total, "query": c.get("query", "")}))

                q = c.get("query", "")
                ans = c.get("answer", "")
                ctxs = c.get("contexts", [])
                gt = c.get("ground_truth", "")
                cat = c.get("category", "")

                try:
                    eval_res = await asyncio.to_thread(
                        evaluator.evaluate_single,
                        query=q,
                        response=ans,
                        contexts=ctxs,
                        ground_truth=gt,
                        category=cat
                    )
                except Exception as e:
                    eval_res = {
                        "faithfulness": 0.0,
                        "answer_relevance": 0.0,
                        "context_precision": 0.0,
                        "context_recall": 0.0,
                        "composite_score": 0.0,
                        "diagnosis": f"裁判异常: {str(e)}",
                        "actionable_advice": "请检查裁判模型网络与 API 配置"
                    }

                score = eval_res.get("composite_score", 0.0)
                is_passed = score >= 0.60
                item_evaluated = {**c, **eval_res, "is_passed": is_passed}
                await queue.put(("done_item", item_evaluated))

        tasks = [asyncio.create_task(worker(i, c)) for i, c in enumerate(answered_cases)]

        completed_count = 0
        all_evaluated = []
        while completed_count < total:
            ev_type, payload = await queue.get()
            if ev_type == "progress":
                yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            elif ev_type == "done_item":
                all_evaluated.append(payload)
                completed_count += 1
                frame_data = {
                    "current": completed_count,
                    "total": total,
                    "case": payload,
                    "item": payload
                }
                yield f"event: case_done\ndata: {json.dumps(frame_data, ensure_ascii=False)}\n\n"
                yield f"event: judge_item\ndata: {json.dumps(frame_data, ensure_ascii=False)}\n\n"

        await asyncio.gather(*tasks)

        total_elapsed = round(time.time() - t_start, 2)
        summary = build_benchmark_summary(
            case_results=all_evaluated,
            model_b=ALI_DEFAULT_ANSWER_MODEL,
            model_c=model_c,
            total_elapsed=total_elapsed
        )
        with open(LATEST_REPORT_JSON, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        # 阶段四完成时自动编译生成 21 列综合指标 Excel 权威报表，保证磁盘与前端永远实时同步
        try:
            excel_report_path = os.path.join(BENCHMARK_DIR, "基准评测集_带量化指标.xlsx")
            export_evaluation_report(summary, excel_report_path)
            print(f"📊 [自动报表同步] 阶段四评测打分结束，已自动编译最新报表: {excel_report_path}")
        except Exception as ex:
            print(f"⚠️ [自动报表同步失败]: {ex}")

        out_summary = {"summary": summary, **summary}
        yield f"event: all_done\ndata: {json.dumps(out_summary, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )


# ==============================================================================
# 七、 阶段五：即时同步写盘与报表下载
# ==============================================================================

@app.post("/api/benchmark/sync-export", summary="阶段五：即时写盘防丢与 Excel 报表编译")
@app.post("/api/benchmark/sync-and-export", summary="阶段五：即时写盘防丢与 Excel 报表编译 (前端兼容别名)")
async def sync_export_benchmark(req: SyncExportRequest):
    summary = req.summary
    with open(LATEST_REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    excel_report_path = os.path.join(BENCHMARK_DIR, "基准评测集_带量化指标.xlsx")
    export_evaluation_report(summary, excel_report_path)

    return {
        "code": 200,
        "message": "评测结果与 21 列综合指标 Excel 已成功同步写盘",
        "excel_path": excel_report_path
    }


@app.get("/api/benchmark/download-report", summary="下载 21 列综合指标 Excel 权威报表")
async def download_eval_report():
    excel_path = os.path.join(BENCHMARK_DIR, "基准评测集_带量化指标.xlsx")
    if not os.path.exists(excel_path):
        raise HTTPException(status_code=404, detail="评测报表尚未生成，请先完成评测流水线")
    return FileResponse(
        path=excel_path,
        filename="Agentic_RAG_基准评测集_带量化指标.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 70)
    print("🧪 [评测中台微服务] 正在启动...")
    print("📍 访问看板: http://localhost:8002/")
    print("🎯 目标 Agent: " + DEFAULT_AGENT_CHAT_URL)
    print("=" * 70 + "\n")
    uvicorn.run("app:app", host="0.0.0.0", port=8002, reload=False)
