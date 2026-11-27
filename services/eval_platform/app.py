"""
================================================================================
企业级工业评测中台微服务 (Evaluation Platform)
主服务接入与评测调度中枢 (app.py)

【微服务定位】
端口: 8002
职责:
1. 提供独立的评测看板工作台 Web UI (http://localhost:8002/)；
2. 调度五阶段自动化评测流水线：
   - 阶段一：qwen3.8-max-0902 深入知识库自动命题
   - 阶段二：黄金题库人工审核与 Excel/JSON 导出持久化
   - 阶段三：作为独立考场，通过 HTTP 协议向目标 Agent (如 http://localhost:8000/api/chat) 并发派题收集作答
   - 阶段四：独立裁判 qwen3.8-max-0902 深度反思打分 (Ragas 四维指标 + 根因诊断处方)
   - 阶段五：即时写盘防丢与 21 列综合指标 Excel 报表编译下载
3. 彻底实现评测与业务解耦 (Eval-as-a-Service)，可评测任意外部业务 Agent。
================================================================================
"""

import os
import sys
import json
import time
import random
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
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

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
    ALI_DEFAULT_GENERATOR_MODEL,
    DEFAULT_RERANK_MODEL,
    OLLAMA_EMBEDDING_MODEL
)
from dataset_generator import (
    parse_guide_markdown,
    generate_benchmark_from_docs,
    export_to_excel,
    DEFAULT_GEN_WORKERS,
    DOCS_DIR
)
from common.logging_config import configure_logging, get_logger, get_trace_id, install_trace_middleware
from benchmark_runner import (
    load_reviewed_cases,
    export_evaluation_report,
    build_benchmark_summary,
    DEFAULT_ANSWER_WORKERS,
    DEFAULT_JUDGE_WORKERS,
    DEFAULT_SUBMIT_STAGGER_SECONDS,
    ANSWERS_CHECKPOINT_JSONL,
    JUDGE_CHECKPOINT_JSONL,
    load_jsonl_checkpoint,
    append_jsonl_checkpoint,
    clear_jsonl_checkpoint,
    get_checkpoint_status,
    is_rate_limit_text,
    is_quota_exhausted_text,
)
from schemas import (
    BenchmarkGenerateRequest,
    BenchmarkSaveCasesRequest,
    BenchmarkRunAnswersRequest,
    BenchmarkRunJudgeRequest,
    BenchmarkRunPipelineRequest,
    SyncExportRequest
)

configure_logging("eval-platform")
logger = get_logger("eval_platform.app")

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

install_trace_middleware(
    app,
    "eval-platform",
    silence_prefixes=["/api/eval/health", "/api/benchmark/latest", "/api/benchmark/config"],
)

# 挂载静态资源
app.mount("/static", StaticFiles(directory=os.path.join(CURRENT_DIR, "static")), name="static")


# ==============================================================================
# 一、 评测看板首页与健康检查
# ==============================================================================

@app.get("/", response_class=HTMLResponse, summary="评测工作台中枢首页")
def benchmark_index():
    """读取并渲染评测中枢工作台前端页面 benchmark.html"""
    if not os.path.exists(BENCHMARK_HTML_PATH):
        raise HTTPException(status_code=404, detail="benchmark.html 页面未找到")
    with open(BENCHMARK_HTML_PATH, "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)


@app.get("/api/eval/health", summary="评测中台健康状态")
def health():
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
def get_latest_eval_report():
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
def get_latest_answered_cases():
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
            if "qa_latency" not in item and "latency" in item:
                item["qa_latency"] = item["latency"]
            if "contexts_preview" not in item:
                ctxs = item.get("contexts") or []
                item["contexts_preview"] = ("\n---\n".join(ctxs[:3]))[:800] if ctxs else ""
        return {"code": 200, "data": data}
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@app.get("/api/benchmark/config", summary="获取评测中枢动态模型配置")
def get_benchmark_config():
    return {
        "code": 200,
        "data": {
            "generator_model": ALI_DEFAULT_GENERATOR_MODEL,
            "answer_model": ALI_DEFAULT_ANSWER_MODEL,
            "judge_model": ALI_DEFAULT_JUDGE_MODEL,
            "rerank_model": DEFAULT_RERANK_MODEL,
            "embedding_model": OLLAMA_EMBEDDING_MODEL,
            "agent_url": DEFAULT_AGENT_CHAT_URL,
            "answer_concurrency": DEFAULT_ANSWER_WORKERS,
            "judge_concurrency": DEFAULT_JUDGE_WORKERS,
        }
    }


@app.get("/api/benchmark/checkpoint-status", summary="查询断点续跑与作答复用状态")
def benchmark_checkpoint_status():
    return {"code": 200, "data": get_checkpoint_status()}


@app.get("/api/benchmark/cases", summary="获取当前待审核基准测试集用例列表")
def get_benchmark_cases():
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


@app.get("/api/benchmark/export-cases", summary="导出当前待审核基准测试集 Excel")
def export_benchmark_cases():
    excel_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.xlsx")
    json_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.json")

    if not os.path.exists(excel_path):
        cases = []
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    cases = json.load(f)
            except Exception:
                cases = []
        if not cases:
            cases = parse_guide_markdown(case_count=10)
        if not cases:
            raise HTTPException(status_code=404, detail="待审核测试集为空，无法导出")
        export_to_excel(cases, excel_path)

    return FileResponse(
        path=excel_path,
        filename="Agentic_RAG_待审核基准测试集.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


# ==============================================================================
# 三、 阶段一：自动命题与题库装载
# ==============================================================================

def _load_existing_benchmark_cases() -> List[Dict[str, Any]]:
    """读取当前本地已持久化的待审核基准测试集，用于增量补齐。"""
    json_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.json")
    excel_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.xlsx")
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    if os.path.exists(excel_path):
        try:
            return load_reviewed_cases(excel_path)
        except Exception:
            pass
    return []


@app.post("/api/benchmark/generate-cases", summary="阶段一：命题专家派生考题")
async def generate_benchmark_cases(req: BenchmarkGenerateRequest):
    model_a = req.model_a or ALI_DEFAULT_GENERATOR_MODEL
    case_count = int(req.case_count or 10)
    if case_count <= 0:
        case_count = 10

    json_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.json")
    excel_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.xlsx")
    started_at = time.perf_counter()

    if req.mode == "from-guide":
        cases = await asyncio.to_thread(parse_guide_markdown, None, case_count)
    else:
        existing_cases = _load_existing_benchmark_cases() if req.append_existing else None
        cases = await asyncio.to_thread(
            generate_benchmark_from_docs,
            DOCS_DIR,
            model_a,
            DEFAULT_GEN_WORKERS,
            case_count,
            existing_cases,
            None,
        )

    await asyncio.to_thread(export_to_excel, cases, excel_path)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=2)

    elapsed = round(time.perf_counter() - started_at, 2)
    return {
        "code": 200,
        "message": f"成功生成并持久化 {len(cases)} 道考题 (耗时 {elapsed}s)",
        "data": cases
    }


@app.post("/api/benchmark/generate-cases-stream", summary="阶段一：命题专家派生考题 (SSE 实时推流)")
async def generate_benchmark_cases_stream(req: BenchmarkGenerateRequest):
    model_a = req.model_a or ALI_DEFAULT_GENERATOR_MODEL
    case_count = int(req.case_count or 10)
    if case_count <= 0:
        case_count = 10

    json_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.json")
    excel_path = os.path.join(BENCHMARK_DIR, "待审核基准测试集.xlsx")
    existing_cases = _load_existing_benchmark_cases() if (req.append_existing and req.mode != "from-guide") else None

    async def event_generator():
        started_at = time.perf_counter()
        if req.mode == "from-guide":
            meta_payload = {
                "event": "meta",
                "model_a": model_a,
                "available_docs": 1,
                "total_batches": 1,
                "target_count": case_count,
                "existing_count": 0,
                "round": 1,
                "is_backfill": False,
            }
            yield f"event: meta\ndata: {json.dumps(meta_payload, ensure_ascii=False)}\n\n"
            cases = await asyncio.to_thread(parse_guide_markdown, None, case_count)
            await asyncio.to_thread(export_to_excel, cases, excel_path)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(cases, f, ensure_ascii=False, indent=2)
            elapsed = round(time.perf_counter() - started_at, 2)
            done_payload = {
                "total": len(cases),
                "elapsed_seconds": elapsed,
                "message": f"成功载入并持久化 {len(cases)} 道指南考题 (耗时 {elapsed}s)",
                "data": cases,
            }
            yield f"event: all_done\ndata: {json.dumps(done_payload, ensure_ascii=False)}\n\n"
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _on_progress(payload: Dict[str, Any]) -> None:
            ev_name = str(payload.get("event") or "batch_done")
            loop.call_soon_threadsafe(queue.put_nowait, (ev_name, payload))

        worker_task = asyncio.create_task(
            asyncio.to_thread(
                generate_benchmark_from_docs,
                DOCS_DIR,
                model_a,
                DEFAULT_GEN_WORKERS,
                case_count,
                existing_cases,
                _on_progress,
            )
        )

        while not worker_task.done() or not queue.empty():
            try:
                ev_name, payload = await asyncio.wait_for(queue.get(), timeout=0.2)
                yield f"event: {ev_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                continue

        try:
            cases = await worker_task
        except Exception as exc:
            err_payload = {"message": f"出题流水线异常: {exc}"}
            yield f"event: error\ndata: {json.dumps(err_payload, ensure_ascii=False)}\n\n"
            return

        await asyncio.to_thread(export_to_excel, cases, excel_path)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(cases, f, ensure_ascii=False, indent=2)

        elapsed = round(time.perf_counter() - started_at, 2)
        done_payload = {
            "total": len(cases),
            "elapsed_seconds": elapsed,
            "message": f"成功生成并持久化 {len(cases)} 道考题 (耗时 {elapsed}s)",
            "data": cases,
        }
        yield f"event: all_done\ndata: {json.dumps(done_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ==============================================================================
# 四、 阶段二：审题与题库保存
# ==============================================================================

@app.post("/api/benchmark/save-cases", summary="阶段二：审题修正后保存持久化")
def save_reviewed_cases(req: BenchmarkSaveCasesRequest):
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


async def _call_agent_http(
    query: str,
    model_name: str,
    agent_url: str = DEFAULT_AGENT_CHAT_URL,
    client: Optional[httpx.AsyncClient] = None,
    rate_limit_retries: int = 3,
) -> Dict[str, Any]:
    """通过 HTTP 协议向 Agent 服务发送提问 (解耦微服务，内置 429 指数退避与配额熔断识别)"""
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=180.0)
    try:
        for retry_idx in range(rate_limit_retries + 1):
            try:
                resp = await client.post(
                    agent_url,
                    json={"query": query, "model_name": model_name, "stream": False, "persist": False},
                    headers={"X-Trace-ID": get_trace_id()}
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    data = payload.get("data", {})
                    contexts = data.get("contexts", [])
                    steps = data.get("steps", [])
                    if not contexts and steps:
                        for s in steps:
                            obs = s.get("observation") or s.get("observation_preview") or ""
                            if obs and ("【切片" in obs or "【来源" in obs or "=== 正在精读" in obs):
                                contexts.append(obs)

                    ans_text = str(data.get("answer", ""))
                    success_flag = bool(data.get("success", True))
                    err_type = data.get("error_type")
                    err_probe = f"{err_type or ''} {ans_text}"
                    if not success_flag and is_quota_exhausted_text(err_probe):
                        return {
                            "answer": ans_text,
                            "contexts": contexts,
                            "steps": steps,
                            "success": False,
                            "error_type": "agent_quota",
                            "quota_exhausted": True,
                        }
                    if not success_flag and (err_type == "http_429" or is_rate_limit_text(err_probe)) and retry_idx < rate_limit_retries:
                        await asyncio.sleep(min(16.0, 2.0 ** (retry_idx + 1)) + random.uniform(0.0, 0.8))
                        continue

                    return {
                        "answer": ans_text,
                        "contexts": contexts,
                        "steps": steps,
                        "success": success_flag,
                        "error_type": err_type,
                        "quota_exhausted": False,
                    }

                body_text = resp.text[:240]
                if resp.status_code == 403 or is_quota_exhausted_text(body_text):
                    return {
                        "answer": f"Agent 服务额度耗尽或鉴权受限 (HTTP {resp.status_code}): {body_text}",
                        "contexts": [],
                        "steps": [],
                        "success": False,
                        "error_type": "agent_quota",
                        "quota_exhausted": True,
                    }
                if (resp.status_code == 429 or is_rate_limit_text(body_text)) and retry_idx < rate_limit_retries:
                    await asyncio.sleep(min(16.0, 2.0 ** (retry_idx + 1)) + random.uniform(0.0, 0.8))
                    continue
                return {
                    "answer": f"Agent 服务响应异常 (HTTP {resp.status_code})",
                    "contexts": [],
                    "steps": [],
                    "success": False,
                    "error_type": f"http_{resp.status_code}",
                    "quota_exhausted": False,
                }
            except httpx.ConnectError:
                return {
                    "answer": f"无法连接业务 Agent (请确认 Agent 服务在 {agent_url} 正常运行)",
                    "contexts": [],
                    "steps": [],
                    "success": False,
                    "error_type": "agent_connection_error",
                    "quota_exhausted": False,
                }
            except httpx.TimeoutException as e:
                return {
                    "answer": f"调用 Agent 超时 (超过 180s 限制): {type(e).__name__}",
                    "contexts": [],
                    "steps": [],
                    "success": False,
                    "error_type": "agent_timeout",
                    "quota_exhausted": False,
                }
            except Exception as e:
                err_detail = f"{type(e).__name__}: {str(e)}" if str(e) else type(e).__name__
                if is_quota_exhausted_text(err_detail):
                    return {
                        "answer": f"调用 Agent 配额耗尽: {err_detail}",
                        "contexts": [],
                        "steps": [],
                        "success": False,
                        "error_type": "agent_quota",
                        "quota_exhausted": True,
                    }
                if is_rate_limit_text(err_detail) and retry_idx < rate_limit_retries:
                    await asyncio.sleep(min(16.0, 2.0 ** (retry_idx + 1)) + random.uniform(0.0, 0.8))
                    continue
                logger.warning(
                    "调用业务 Agent 异常",
                    extra={"event": "agent.call_failed", "agent_url": agent_url, "error_detail": err_detail},
                    exc_info=True,
                )
                return {
                    "answer": f"调用 Agent 异常: {err_detail}",
                    "contexts": [],
                    "steps": [],
                    "success": False,
                    "error_type": "agent_exception",
                    "quota_exhausted": False,
                }
    finally:
        if owns_client:
            await client.aclose()


def _clamp_concurrency(value: Optional[int], default_val: int) -> int:
    try:
        parsed = int(value) if value is not None else default_val
    except Exception:
        parsed = default_val
    return max(1, min(32, parsed))


def _build_answer_item(c: Dict[str, Any], idx: int, qa_res: Dict[str, Any], model_b: str, latency: float) -> Dict[str, Any]:
    ctxs = qa_res.get("contexts", []) or []
    ans = qa_res.get("answer", "")
    return {
        "id": c.get("id", idx + 1),
        "category": c.get("category", "综合业务推理"),
        "query": c["query"],
        "ground_truth": c.get("reviewed_ground_truth") or c.get("reviewed_gt") or c.get("ground_truth") or "",
        "raw_ground_truth": c.get("raw_ground_truth") or "",
        "source_chunks": c.get("source_chunks") or "",
        "source_doc": c.get("source_doc") or "",
        "review_status": c.get("review_status") or "通过",
        "reviewed_gt": c.get("reviewed_gt") or "",
        "review_comment": c.get("review_comment") or "",
        "answer": ans,
        "system_answer": ans,
        "contexts": ctxs,
        "contexts_count": len(ctxs),
        "contexts_preview": ("\n---\n".join(ctxs[:3]))[:800] if ctxs else "",
        "steps": qa_res.get("steps", []),
        "model_name": model_b,
        "latency": latency,
        "qa_latency": latency,
        "success": qa_res.get("success", False),
        "error_type": qa_res.get("error_type"),
        "quota_exhausted": bool(qa_res.get("quota_exhausted", False)),
    }


def _evaluate_case_result_item(
    c: Dict[str, Any],
    evaluator: Any,
) -> Dict[str, Any]:
    q = c.get("query", "")
    ans = c.get("answer") or c.get("system_answer") or ""
    ctxs = c.get("contexts", []) or []
    gt = c.get("ground_truth", "")
    cat = c.get("category", "")
    qa_latency = float(c.get("qa_latency") or c.get("latency") or 0.0)

    if c.get("success") is False or c.get("error_type"):
        err_type = c.get("error_type") or "agent_error"
        return {
            **c,
            "system_answer": ans,
            "contexts": ctxs,
            "contexts_count": len(ctxs),
            "contexts_preview": c.get("contexts_preview") or (("\n---\n".join(ctxs[:3]))[:800] if ctxs else ""),
            "qa_latency": qa_latency,
            "judge_latency": 0.0,
            "total_latency": qa_latency,
            "scores": {},
            "is_valid": False,
            "is_passed": False,
            "execution_status": "failed",
            "evaluation_status": "skipped",
            "error_stage": "agent",
            "error_type": err_type,
            "retry_recommended": True,
            "quota_exhausted": bool(c.get("quota_exhausted") or err_type == "agent_quota"),
            "diagnoses": ["Agent 未能完成有效作答，本题跳过裁判且不计分。"],
            "prescriptions": ["修复作答链路后重跑本题。"],
        }

    try:
        eval_res = evaluator.evaluate_single(
            query=q,
            response=ans,
            contexts=ctxs,
            ground_truth=gt,
            category=cat
        )
    except Exception as e:
        err_type = "judge_quota" if is_quota_exhausted_text(e) else (
            "judge_rate_limit" if is_rate_limit_text(e) else "judge_exception"
        )
        eval_res = {
            "is_valid": False,
            "evaluation_status": "failed",
            "error_stage": "judge",
            "error_type": err_type,
            "retry_recommended": True,
            "diagnoses": [f"裁判异常: {str(e)[:180]}"],
            "prescriptions": ["请检查裁判模型网络与 API 配置后重跑。"]
        }

    judge_latency = float(eval_res.get("elapsed_seconds", 0.0) or 0.0)
    total_latency = round(qa_latency + judge_latency, 2)
    if eval_res.get("is_valid", eval_res.get("evaluation_status") == "success"):
        scores = eval_res.get("scores", {})
        triad_score = float(scores.get("rag_triad_score", 0.0) or 0.0)
        return {
            **c,
            "system_answer": ans,
            "contexts": ctxs,
            "contexts_count": len(ctxs),
            "contexts_preview": c.get("contexts_preview") or (("\n---\n".join(ctxs[:3]))[:800] if ctxs else ""),
            "qa_latency": qa_latency,
            "judge_latency": judge_latency,
            "total_latency": total_latency,
            "scores": scores,
            "is_valid": True,
            "is_passed": triad_score >= 0.80,
            "execution_status": "success",
            "evaluation_status": "success",
            "error_stage": None,
            "error_type": None,
            "retry_recommended": False,
            "quota_exhausted": False,
            "diagnoses": eval_res.get("diagnoses", []),
            "prescriptions": eval_res.get("prescriptions", []),
        }

    err_type = eval_res.get("error_type", "judge_error")
    return {
        **c,
        "system_answer": ans,
        "contexts": ctxs,
        "contexts_count": len(ctxs),
        "contexts_preview": c.get("contexts_preview") or (("\n---\n".join(ctxs[:3]))[:800] if ctxs else ""),
        "qa_latency": qa_latency,
        "judge_latency": judge_latency,
        "total_latency": total_latency,
        "scores": {},
        "is_valid": False,
        "is_passed": False,
        "execution_status": "failed",
        "evaluation_status": "failed",
        "error_stage": eval_res.get("error_stage", "judge"),
        "error_type": err_type,
        "retry_recommended": True,
        "quota_exhausted": bool(err_type == "judge_quota" or is_quota_exhausted_text(" ".join(eval_res.get("diagnoses", [])))),
        "diagnoses": eval_res.get("diagnoses", ["裁判未返回有效评分。"]),
        "prescriptions": eval_res.get("prescriptions", ["本题本轮不计分，请重跑。"]),
    }


@app.post("/api/benchmark/run-answers-stream", summary="阶段三：考生批量作答 (SSE 实时推流)")
async def run_benchmark_answers_stream(req: BenchmarkRunAnswersRequest):
    target_cases = _prepare_target_cases(req.limit)
    model_b = req.model_b or ALI_DEFAULT_ANSWER_MODEL
    total = len(target_cases)
    concurrency_limit = _clamp_concurrency(req.concurrency, DEFAULT_ANSWER_WORKERS)
    resume = bool(req.resume)

    if not resume:
        clear_jsonl_checkpoint(ANSWERS_CHECKPOINT_JSONL)
    cached_answers = (
        {
            cid: rec
            for cid, rec in load_jsonl_checkpoint(ANSWERS_CHECKPOINT_JSONL).items()
            if rec.get("success", False)
        }
        if resume else {}
    )

    print(f"\n[评测 3/5] 批量作答 | 题目 {total} | 考生模型 {model_b} | 并发 {concurrency_limit} | 续跑 {resume}")

    async def event_generator():
        sem = asyncio.Semaphore(concurrency_limit)
        queue: asyncio.Queue = asyncio.Queue()
        quota_abort_reason: Optional[str] = None
        completed_count = 0
        all_results: List[Dict[str, Any]] = []

        # 先推送可断点复用的已作答题目
        pending_cases: List[tuple] = []
        for idx, c in enumerate(target_cases):
            cid = int(c.get("id", idx + 1))
            if resume and cid in cached_answers:
                cached = dict(cached_answers[cid])
                cached["system_answer"] = cached.get("system_answer") or cached.get("answer", "")
                cached["contexts_count"] = len(cached.get("contexts", []))
                all_results.append(cached)
                completed_count += 1
                frame_data = {"current": completed_count, "total": total, "case": cached, "item": cached, "resumed": True}
                yield f"event: case_done\ndata: {json.dumps(frame_data, ensure_ascii=False)}\n\n"
                yield f"event: answer_item\ndata: {json.dumps(frame_data, ensure_ascii=False)}\n\n"
            else:
                pending_cases.append((idx, c))

        if pending_cases:
            async with httpx.AsyncClient(timeout=180.0) as shared_client:
                async def worker(idx, c):
                    if DEFAULT_SUBMIT_STAGGER_SECONDS > 0:
                        await asyncio.sleep(min(idx, concurrency_limit) * min(DEFAULT_SUBMIT_STAGGER_SECONDS, 0.15))
                    if quota_abort_reason is not None:
                        return
                    async with sem:
                        if quota_abort_reason is not None:
                            return
                        q = c["query"]
                        await queue.put(("progress", {
                            "current": idx + 1,
                            "total": total,
                            "query": q,
                            "category": c.get("category")
                        }))
                        t0 = time.time()
                        qa_res = await _call_agent_http(query=q, model_name=model_b, client=shared_client)
                        latency = round(time.time() - t0, 2)
                        item = _build_answer_item(c, idx, qa_res, model_b, latency)
                        if item.get("success"):
                            append_jsonl_checkpoint(ANSWERS_CHECKPOINT_JSONL, item)
                        await queue.put(("done_item", item))

                tasks = [asyncio.create_task(worker(idx, c)) for idx, c in pending_cases]
                done_task = asyncio.gather(*tasks)
                while not done_task.done() or not queue.empty():
                    try:
                        ev_type, payload = await asyncio.wait_for(queue.get(), timeout=0.2)
                    except asyncio.TimeoutError:
                        continue
                    if ev_type == "progress":
                        yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    elif ev_type == "done_item":
                        all_results.append(payload)
                        completed_count += 1
                        if payload.get("quota_exhausted") and quota_abort_reason is None:
                            quota_abort_reason = f"作答模型 ({model_b}) 额度耗尽，已安全暂停并保留 {len(all_results) - 1} 题进度"
                            yield f"event: quota_paused\ndata: {json.dumps({'stage': 'answer', 'reason': quota_abort_reason, 'completed': completed_count, 'total': total}, ensure_ascii=False)}\n\n"
                        frame_data = {"current": completed_count, "total": total, "case": payload, "item": payload}
                        yield f"event: case_done\ndata: {json.dumps(frame_data, ensure_ascii=False)}\n\n"
                        yield f"event: answer_item\ndata: {json.dumps(frame_data, ensure_ascii=False)}\n\n"
                await done_task

        all_results.sort(key=lambda x: int(x.get("id", 0) or 0))
        with open(LATEST_ANSWERS_JSON, "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)

        yield f"event: all_done\ndata: {json.dumps({'total': total, 'success_count': len(all_results), 'aborted_by_quota': bool(quota_abort_reason), 'abort_reason': quota_abort_reason, 'data': all_results}, ensure_ascii=False)}\n\n"

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
    concurrency_limit = _clamp_concurrency(req.concurrency, DEFAULT_JUDGE_WORKERS)
    resume = bool(req.resume)
    evaluator = get_ragas_evaluator(model_name=model_c)

    if not resume:
        clear_jsonl_checkpoint(JUDGE_CHECKPOINT_JSONL)
    cached_judges = (
        {
            cid: rec
            for cid, rec in load_jsonl_checkpoint(JUDGE_CHECKPOINT_JSONL).items()
            if rec.get("is_valid", False)
        }
        if resume else {}
    )

    print(f"\n[评测 4/5] 独立客观裁判打分 | 题目 {total} | 裁判模型 {model_c} | 并发 {concurrency_limit} | 续跑 {resume}")

    async def event_generator():
        t_start = time.time()
        sem = asyncio.Semaphore(concurrency_limit)
        queue: asyncio.Queue = asyncio.Queue()
        quota_abort_reason: Optional[str] = None
        completed_count = 0
        all_evaluated: List[Dict[str, Any]] = []

        pending_cases: List[tuple] = []
        for idx, c in enumerate(answered_cases):
            cid = int(c.get("id", idx + 1) or (idx + 1))
            if resume and cid in cached_judges:
                cached = dict(cached_judges[cid])
                all_evaluated.append(cached)
                completed_count += 1
                frame_data = {"current": completed_count, "total": total, "case": cached, "item": cached, "resumed": True}
                yield f"event: case_done\ndata: {json.dumps(frame_data, ensure_ascii=False)}\n\n"
                yield f"event: judge_item\ndata: {json.dumps(frame_data, ensure_ascii=False)}\n\n"
            else:
                pending_cases.append((idx, c))

        if pending_cases:
            async def worker(idx, c):
                if DEFAULT_SUBMIT_STAGGER_SECONDS > 0:
                    await asyncio.sleep(min(idx, concurrency_limit) * min(DEFAULT_SUBMIT_STAGGER_SECONDS, 0.1))
                if quota_abort_reason is not None:
                    return
                async with sem:
                    if quota_abort_reason is not None:
                        return
                    await queue.put(("progress", {"current": idx + 1, "total": total, "query": c.get("query", "")}))
                    item_evaluated = await asyncio.to_thread(_evaluate_case_result_item, c, evaluator)
                    if item_evaluated.get("is_valid"):
                        append_jsonl_checkpoint(JUDGE_CHECKPOINT_JSONL, item_evaluated)
                    await queue.put(("done_item", item_evaluated))

            tasks = [asyncio.create_task(worker(idx, c)) for idx, c in pending_cases]
            done_task = asyncio.gather(*tasks)
            while not done_task.done() or not queue.empty():
                try:
                    ev_type, payload = await asyncio.wait_for(queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                if ev_type == "progress":
                    yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                elif ev_type == "done_item":
                    all_evaluated.append(payload)
                    completed_count += 1
                    if payload.get("quota_exhausted") and quota_abort_reason is None:
                        quota_abort_reason = f"裁判模型 ({model_c}) 额度耗尽，已安全暂停并保留 {len(all_evaluated) - 1} 题进度"
                        yield f"event: quota_paused\ndata: {json.dumps({'stage': 'judge', 'reason': quota_abort_reason, 'completed': completed_count, 'total': total}, ensure_ascii=False)}\n\n"
                    frame_data = {"current": completed_count, "total": total, "case": payload, "item": payload}
                    yield f"event: case_done\ndata: {json.dumps(frame_data, ensure_ascii=False)}\n\n"
                    yield f"event: judge_item\ndata: {json.dumps(frame_data, ensure_ascii=False)}\n\n"
            await done_task

        all_evaluated.sort(key=lambda x: int(x.get("id", 0) or 0))
        total_elapsed = round(time.time() - t_start, 2)
        actual_model_b = next(
            (c.get("model_name") for c in answered_cases if c.get("model_name")),
            ALI_DEFAULT_ANSWER_MODEL
        )
        summary = build_benchmark_summary(
            case_results=all_evaluated,
            model_b=actual_model_b,
            model_c=model_c,
            total_elapsed=total_elapsed,
            max_workers=concurrency_limit,
        )
        summary["aborted_by_quota"] = bool(quota_abort_reason)
        summary["abort_reason"] = quota_abort_reason
        summary["planned_total_cases"] = total
        with open(LATEST_REPORT_JSON, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

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


@app.post("/api/benchmark/run-pipeline-stream", summary="一键双模型异步流水线评测 (边作答边打分)")
async def run_benchmark_pipeline_stream(req: BenchmarkRunPipelineRequest):
    target_cases = _prepare_target_cases(req.limit)
    model_b = req.model_b or ALI_DEFAULT_ANSWER_MODEL
    model_c = req.model_c or ALI_DEFAULT_JUDGE_MODEL
    total = len(target_cases)
    ans_conc = _clamp_concurrency(req.answer_concurrency, DEFAULT_ANSWER_WORKERS)
    judge_conc = _clamp_concurrency(req.judge_concurrency, DEFAULT_JUDGE_WORKERS)
    resume = bool(req.resume)
    reuse_answers = bool(req.reuse_answers)

    if not resume and not reuse_answers:
        clear_jsonl_checkpoint(ANSWERS_CHECKPOINT_JSONL)
        clear_jsonl_checkpoint(JUDGE_CHECKPOINT_JSONL)
    elif reuse_answers and not resume:
        clear_jsonl_checkpoint(JUDGE_CHECKPOINT_JSONL)

    cached_judges: Dict[int, Dict[str, Any]] = {}
    cached_answers: Dict[int, Dict[str, Any]] = {}
    if resume:
        cached_judges = {
            cid: rec
            for cid, rec in load_jsonl_checkpoint(JUDGE_CHECKPOINT_JSONL).items()
            if rec.get("is_valid", False)
        }
        cached_answers = {
            cid: rec
            for cid, rec in load_jsonl_checkpoint(ANSWERS_CHECKPOINT_JSONL).items()
            if rec.get("success", False)
        }
    if reuse_answers:
        latest_ans: Dict[int, Dict[str, Any]] = {}
        if os.path.exists(LATEST_ANSWERS_JSON):
            try:
                with open(LATEST_ANSWERS_JSON, "r", encoding="utf-8") as f:
                    raw_latest = json.load(f)
                if isinstance(raw_latest, list):
                    for item in raw_latest:
                        if isinstance(item, dict) and item.get("id") is not None and item.get("success", False):
                            latest_ans[int(item["id"])] = item
            except Exception:
                latest_ans = {}
        ckpt_ans = {
            cid: rec
            for cid, rec in load_jsonl_checkpoint(ANSWERS_CHECKPOINT_JSONL).items()
            if rec.get("success", False)
        }
        cached_answers = {**latest_ans, **ckpt_ans, **cached_answers}

    evaluator = get_ragas_evaluator(model_name=model_c)
    print(
        f"\n[一键双流水线] 题目 {total} | 作答 {model_b} (并发 {ans_conc}) -> 裁判 {model_c} (并发 {judge_conc}) | "
        f"续跑 {resume} | 复用 {reuse_answers}"
    )

    async def event_generator():
        t_start = time.time()
        ans_sem = asyncio.Semaphore(ans_conc)
        judge_sem = asyncio.Semaphore(judge_conc)
        queue: asyncio.Queue = asyncio.Queue()

        quota_abort_reason: Optional[str] = None
        all_answers: List[Dict[str, Any]] = []
        all_evaluated: List[Dict[str, Any]] = []
        ans_done_count = 0
        judge_done_count = 0
        resumed_count = 0
        reused_count = 0

        yield f"event: pipeline_meta\ndata: {json.dumps({'total': total, 'model_b': model_b, 'model_c': model_c, 'answer_concurrency': ans_conc, 'judge_concurrency': judge_conc, 'resume': resume, 'reuse_answers': reuse_answers}, ensure_ascii=False)}\n\n"

        pending_judge_only: List[tuple] = []
        pending_answer: List[tuple] = []

        for idx, c in enumerate(target_cases):
            cid = int(c.get("id", idx + 1) or (idx + 1))
            if resume and cid in cached_judges:
                rec = dict(cached_judges[cid])
                all_evaluated.append(rec)
                judge_done_count += 1
                resumed_count += 1
                if cid in cached_answers:
                    ans_rec = dict(cached_answers[cid])
                    all_answers.append(ans_rec)
                    ans_done_count += 1
                    yield f"event: answer_done\ndata: {json.dumps({'current': ans_done_count, 'total': total, 'case': ans_rec, 'resumed': True}, ensure_ascii=False)}\n\n"
                elif rec.get("system_answer"):
                    ctxs = rec.get("contexts") or ([rec["contexts_preview"]] if rec.get("contexts_preview") else [])
                    ans_rec = {
                        "id": cid,
                        "category": rec.get("category", "综合业务推理"),
                        "query": rec.get("query", c.get("query", "")),
                        "ground_truth": rec.get("ground_truth", c.get("ground_truth", "")),
                        "answer": rec["system_answer"],
                        "system_answer": rec["system_answer"],
                        "contexts": ctxs,
                        "contexts_count": rec.get("contexts_count", len(ctxs)),
                        "contexts_preview": rec.get("contexts_preview", ""),
                        "qa_latency": rec.get("qa_latency", 0.0),
                        "latency": rec.get("qa_latency", 0.0),
                        "model_name": rec.get("model_b", model_b),
                        "success": True,
                        "error_type": None,
                    }
                    all_answers.append(ans_rec)
                    ans_done_count += 1
                    yield f"event: answer_done\ndata: {json.dumps({'current': ans_done_count, 'total': total, 'case': ans_rec, 'resumed': True}, ensure_ascii=False)}\n\n"
                yield f"event: judge_done\ndata: {json.dumps({'current': judge_done_count, 'total': total, 'case': rec, 'resumed': True}, ensure_ascii=False)}\n\n"
                continue

            if cid in cached_answers:
                ans_rec = dict(cached_answers[cid])
                all_answers.append(ans_rec)
                ans_done_count += 1
                if reuse_answers:
                    reused_count += 1
                else:
                    resumed_count += 1
                yield f"event: answer_done\ndata: {json.dumps({'current': ans_done_count, 'total': total, 'case': ans_rec, 'resumed': True}, ensure_ascii=False)}\n\n"
                pending_judge_only.append((idx, ans_rec))
                continue

            pending_answer.append((idx, c))

        async def _judge_task(ans_item: Dict[str, Any]):
            if quota_abort_reason is not None:
                return
            async with judge_sem:
                if quota_abort_reason is not None:
                    return
                await queue.put(("judge_progress", {"query": ans_item.get("query", ""), "id": ans_item.get("id")}))
                evaluated = await asyncio.to_thread(_evaluate_case_result_item, ans_item, evaluator)
                if evaluated.get("is_valid"):
                    append_jsonl_checkpoint(JUDGE_CHECKPOINT_JSONL, evaluated)
                await queue.put(("judge_done", evaluated))

        async with httpx.AsyncClient(timeout=180.0) as shared_client:
            judge_tasks: List[asyncio.Task] = []

            for idx, ans_rec in pending_judge_only:
                judge_tasks.append(asyncio.create_task(_judge_task(ans_rec)))

            async def _answer_and_dispatch(idx: int, c: Dict[str, Any]):
                if DEFAULT_SUBMIT_STAGGER_SECONDS > 0:
                    await asyncio.sleep(min(idx, ans_conc) * min(DEFAULT_SUBMIT_STAGGER_SECONDS, 0.15))
                if quota_abort_reason is not None:
                    return
                async with ans_sem:
                    if quota_abort_reason is not None:
                        return
                    await queue.put(("answer_progress", {"query": c.get("query", ""), "id": c.get("id", idx + 1)}))
                    t0 = time.time()
                    qa_res = await _call_agent_http(query=c["query"], model_name=model_b, client=shared_client)
                    latency = round(time.time() - t0, 2)
                    ans_item = _build_answer_item(c, idx, qa_res, model_b, latency)
                    if ans_item.get("success"):
                        append_jsonl_checkpoint(ANSWERS_CHECKPOINT_JSONL, ans_item)
                    await queue.put(("answer_done", ans_item))

                    if quota_abort_reason is not None and ans_item.get("success"):
                        return
                    if ans_item.get("quota_exhausted"):
                        # 作答额度耗尽，不占用裁判调用
                        evaluated = _evaluate_case_result_item(ans_item, evaluator)
                        await queue.put(("judge_done", evaluated))
                        return
                    await _judge_task(ans_item)

            pipeline_tasks = judge_tasks + [asyncio.create_task(_answer_and_dispatch(idx, c)) for idx, c in pending_answer]
            all_done_future = asyncio.gather(*pipeline_tasks)

            while not all_done_future.done() or not queue.empty():
                try:
                    ev_type, payload = await asyncio.wait_for(queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                if ev_type == "answer_progress":
                    yield f"event: answer_progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                elif ev_type == "judge_progress":
                    yield f"event: judge_progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                elif ev_type == "answer_done":
                    all_answers.append(payload)
                    ans_done_count += 1
                    if payload.get("quota_exhausted") and quota_abort_reason is None:
                        quota_abort_reason = f"作答模型 ({model_b}) 额度耗尽，已安全暂停并保留 {len(all_answers) - 1} 题作答"
                        yield f"event: quota_paused\ndata: {json.dumps({'stage': 'answer', 'reason': quota_abort_reason, 'completed': judge_done_count, 'total': total}, ensure_ascii=False)}\n\n"
                    yield f"event: answer_done\ndata: {json.dumps({'current': ans_done_count, 'total': total, 'case': payload}, ensure_ascii=False)}\n\n"
                elif ev_type == "judge_done":
                    all_evaluated.append(payload)
                    judge_done_count += 1
                    if payload.get("quota_exhausted") and quota_abort_reason is None:
                        quota_abort_reason = f"裁判模型 ({model_c}) 额度耗尽，已安全暂停并保留 {len(all_evaluated) - 1} 题评分"
                        yield f"event: quota_paused\ndata: {json.dumps({'stage': 'judge', 'reason': quota_abort_reason, 'completed': judge_done_count, 'total': total}, ensure_ascii=False)}\n\n"
                    yield f"event: judge_done\ndata: {json.dumps({'current': judge_done_count, 'total': total, 'case': payload}, ensure_ascii=False)}\n\n"

            await all_done_future

        all_answers.sort(key=lambda x: int(x.get("id", 0) or 0))
        all_evaluated.sort(key=lambda x: int(x.get("id", 0) or 0))

        if all_answers:
            with open(LATEST_ANSWERS_JSON, "w", encoding="utf-8") as f:
                json.dump(all_answers, f, ensure_ascii=False, indent=2)

        total_elapsed = round(time.time() - t_start, 2)
        summary = build_benchmark_summary(
            case_results=all_evaluated,
            model_b=model_b,
            model_c=model_c,
            total_elapsed=total_elapsed,
            max_workers=ans_conc,
        )
        summary["aborted_by_quota"] = bool(quota_abort_reason)
        summary["abort_reason"] = quota_abort_reason
        summary["planned_total_cases"] = total
        summary["performance"]["answer_workers"] = ans_conc
        summary["performance"]["judge_workers"] = judge_conc
        summary["performance"]["resumed_cases"] = resumed_count
        summary["performance"]["reused_answer_cases"] = reused_count

        with open(LATEST_REPORT_JSON, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        try:
            excel_report_path = os.path.join(BENCHMARK_DIR, "基准评测集_带量化指标.xlsx")
            export_evaluation_report(summary, excel_report_path)
            print(f"📊 [自动报表同步] 双流水线评测结束，已自动编译最新报表: {excel_report_path}")
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
def sync_export_benchmark(req: SyncExportRequest):
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
def download_eval_report():
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
