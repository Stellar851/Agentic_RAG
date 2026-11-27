import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_DIR = os.path.join(PROJECT_ROOT, "services", "eval_platform")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if EVAL_DIR not in sys.path:
    sys.path.insert(0, EVAL_DIR)

import benchmark_runner
import dataset_generator
import evaluator as evaluator_mod
import app as eval_app


def _sample_cases(count: int = 4):
    return [
        {
            "id": i,
            "category": "综合业务推理",
            "query": f"测试问题 {i} 的规范阈值是多少？",
            "ground_truth": f"标准答案 {i}",
            "raw_ground_truth": f"标准答案 {i}",
            "source_chunks": f"切片依据 {i}",
            "source_doc": f"文档{i}.md",
            "review_status": "通过",
            "reviewed_gt": "",
            "review_comment": "",
        }
        for i in range(1, count + 1)
    ]


class _FakeAgent:
    def __init__(self, delay_s: float = 0.0, quota_on_query: str = ""):
        self.delay_s = delay_s
        self.quota_on_query = quota_on_query
        self.calls = []
        self._lock = threading.Lock()

    def ask(self, user_query: str, model_name: str = None, verbose: bool = False):
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        with self._lock:
            self.calls.append(user_query)
        if self.quota_on_query and self.quota_on_query in user_query:
            return {
                "answer": "Error 403: Allocated quota exceeded for this model.",
                "contexts": [],
                "success": False,
                "error_type": "http_403",
            }
        return {
            "answer": f"回答：{user_query}",
            "contexts": [f"切片证据：{user_query}"],
            "success": True,
            "error_type": None,
        }


class _FakeEvaluator:
    def __init__(self, delay_s: float = 0.0, quota_on_query: str = ""):
        self.delay_s = delay_s
        self.quota_on_query = quota_on_query
        self.calls = []
        self._lock = threading.Lock()

    def evaluate_single(self, query: str, response: str, contexts, ground_truth=None, category=None):
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        with self._lock:
            self.calls.append(query)
        if self.quota_on_query and self.quota_on_query in query:
            return {
                "scores": {},
                "is_valid": False,
                "evaluation_status": "failed",
                "error_stage": "judge",
                "error_type": "judge_quota",
                "diagnoses": ["403 quota exceeded"],
                "prescriptions": ["充值后重跑"],
                "elapsed_seconds": self.delay_s,
            }
        return {
            "scores": {
                "rag_triad_score": 0.92,
                "faithfulness": 0.95,
                "answer_relevance": 0.92,
                "context_precision": 0.90,
                "context_recall": 1.0,
            },
            "is_valid": True,
            "evaluation_status": "success",
            "diagnoses": ["✅ 【链路健康】"],
            "prescriptions": ["维持当前配置"],
            "elapsed_seconds": self.delay_s,
        }


class TestDualPipelineEvaluation(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.ans_ckpt = os.path.join(self.tmp_dir.name, "ckpt_answers.jsonl")
        self.judge_ckpt = os.path.join(self.tmp_dir.name, "ckpt_judge.jsonl")
        self.latest_ans = os.path.join(self.tmp_dir.name, "latest_answers.json")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_dual_pipeline_runs_concurrently_and_saves_checkpoints(self):
        cases = _sample_cases(4)
        fake_agent = _FakeAgent(delay_s=0.12)
        fake_eval = _FakeEvaluator(delay_s=0.12)

        t0 = time.time()
        with patch.object(benchmark_runner, "get_agent_service", return_value=fake_agent), \
             patch.object(benchmark_runner, "get_ragas_evaluator", return_value=fake_eval), \
             patch.object(benchmark_runner, "LATEST_ANSWERS_JSON", self.latest_ans):
            summary = benchmark_runner.run_benchmark_concurrent(
                cases=cases,
                answer_workers=4,
                judge_workers=4,
                stagger_seconds=0.0,
                answers_checkpoint_path=self.ans_ckpt,
                judge_checkpoint_path=self.judge_ckpt,
            )
        elapsed = time.time() - t0

        self.assertEqual(summary["total_cases"], 4)
        self.assertEqual(summary["valid_cases"], 4)
        self.assertEqual(summary["passed_cases"], 4)
        self.assertFalse(summary["aborted_by_quota"])
        # 4 题如果纯串行至少需要 4*(0.12+0.12)=0.96s，流水线并发应在 0.65s 内完成
        self.assertLess(elapsed, 0.70)
        self.assertEqual(len(benchmark_runner.load_jsonl_checkpoint(self.ans_ckpt)), 4)
        self.assertEqual(len(benchmark_runner.load_jsonl_checkpoint(self.judge_ckpt)), 4)

    def test_evaluator_429_rate_limit_retries_with_backoff(self):
        attempts = {"count": 0}

        class _FlakyLLM:
            def invoke(self, prompt: str):
                attempts["count"] += 1
                if attempts["count"] < 3:
                    raise RuntimeError("Error code: 429 - Too Many Requests: rate limit exceeded")
                return types.SimpleNamespace(
                    content=json.dumps({
                        "faithfulness": 0.96,
                        "answer_relevance": 0.94,
                        "context_precision": 0.91,
                        "context_recall": 1.0,
                        "diagnoses": ["✅ 正常"],
                        "prescriptions": ["保持"],
                    })
                )

        with patch.object(evaluator_mod, "get_deepseek_llm", return_value=_FlakyLLM()), \
             patch.object(evaluator_mod.time, "sleep", return_value=None):
            engine = evaluator_mod.RagasEvaluator(model_name="qwen3.8-max-0902")
            res = engine.evaluate_single(
                query="超时时间是多少？",
                response="3000ms",
                contexts=["单次请求超时阈值严格设定为 3000ms。"],
                ground_truth="3000ms",
            )

        self.assertTrue(res["is_valid"])
        self.assertEqual(attempts["count"], 3)
        self.assertGreaterEqual(res["scores"]["rag_triad_score"], 0.90)

    def test_resume_and_reuse_answers_skip_completed_work(self):
        cases = _sample_cases(3)
        # 预置：第 1 题已完成裁判；第 2 题已完成作答未裁判；第 3 题未开始
        benchmark_runner.append_jsonl_checkpoint(self.judge_ckpt, {
            "id": 1,
            "category": "综合业务推理",
            "query": cases[0]["query"],
            "ground_truth": cases[0]["ground_truth"],
            "system_answer": "缓存回答 1",
            "contexts": ["缓存切片 1"],
            "contexts_count": 1,
            "contexts_preview": "缓存切片 1",
            "scores": {"rag_triad_score": 0.95, "faithfulness": 0.95, "answer_relevance": 0.95, "context_precision": 0.95, "context_recall": 1.0},
            "is_valid": True,
            "is_passed": True,
            "qa_latency": 1.0,
            "judge_latency": 1.0,
            "total_latency": 2.0,
            "diagnoses": ["ok"],
            "prescriptions": ["ok"],
        })
        benchmark_runner.append_jsonl_checkpoint(self.ans_ckpt, {
            "id": 2,
            "category": "综合业务推理",
            "query": cases[1]["query"],
            "ground_truth": cases[1]["ground_truth"],
            "answer": "缓存回答 2",
            "system_answer": "缓存回答 2",
            "contexts": ["缓存切片 2"],
            "qa_latency": 0.5,
            "success": True,
            "error_type": None,
        })

        fake_agent = _FakeAgent()
        fake_eval = _FakeEvaluator()

        with patch.object(benchmark_runner, "get_agent_service", return_value=fake_agent), \
             patch.object(benchmark_runner, "get_ragas_evaluator", return_value=fake_eval), \
             patch.object(benchmark_runner, "LATEST_ANSWERS_JSON", self.latest_ans):
            summary = benchmark_runner.run_benchmark_concurrent(
                cases=cases,
                answer_workers=2,
                judge_workers=2,
                resume=True,
                stagger_seconds=0.0,
                answers_checkpoint_path=self.ans_ckpt,
                judge_checkpoint_path=self.judge_ckpt,
            )

        self.assertEqual(len(fake_agent.calls), 1)
        self.assertIn("测试问题 3", fake_agent.calls[0])
        self.assertEqual(len(fake_eval.calls), 2)
        self.assertEqual(summary["valid_cases"], 3)

        # 验证 reuse_answers=True 跳过全部作答仅重跑裁判
        fake_agent_2 = _FakeAgent()
        fake_eval_2 = _FakeEvaluator()
        with patch.object(benchmark_runner, "get_agent_service", return_value=fake_agent_2), \
             patch.object(benchmark_runner, "get_ragas_evaluator", return_value=fake_eval_2), \
             patch.object(benchmark_runner, "LATEST_ANSWERS_JSON", self.latest_ans):
            summary_reuse = benchmark_runner.run_benchmark_concurrent(
                cases=cases,
                answer_workers=2,
                judge_workers=2,
                reuse_answers=True,
                stagger_seconds=0.0,
                answers_checkpoint_path=self.ans_ckpt,
                judge_checkpoint_path=self.judge_ckpt,
            )

        self.assertEqual(len(fake_agent_2.calls), 0)
        self.assertEqual(len(fake_eval_2.calls), 3)
        self.assertEqual(summary_reuse["valid_cases"], 3)

    def test_quota_exhausted_circuit_breaker_stops_remaining_cases(self):
        cases = _sample_cases(4)
        fake_agent = _FakeAgent(quota_on_query="测试问题 2")
        fake_eval = _FakeEvaluator()

        # 顺序慢速单并发确保第 2 题命中配额后，第 3/4 题尚未完成即触发熔断
        with patch.object(benchmark_runner, "get_agent_service", return_value=fake_agent), \
             patch.object(benchmark_runner, "get_ragas_evaluator", return_value=fake_eval), \
             patch.object(benchmark_runner, "LATEST_ANSWERS_JSON", self.latest_ans):
            summary = benchmark_runner.run_benchmark_concurrent(
                cases=cases,
                answer_workers=1,
                judge_workers=1,
                stagger_seconds=0.0,
                answers_checkpoint_path=self.ans_ckpt,
                judge_checkpoint_path=self.judge_ckpt,
            )

        self.assertTrue(summary["aborted_by_quota"])
        self.assertIn("额度耗尽", summary["abort_reason"])
        # 第 1 题已保存成功
        ckpt_ans = benchmark_runner.load_jsonl_checkpoint(self.ans_ckpt)
        self.assertIn(1, ckpt_ans)

    def test_web_pipeline_stream_and_checkpoint_status_endpoints(self):
        cases = _sample_cases(2)
        fake_eval = _FakeEvaluator()

        async def _mock_call_agent_http(query: str, model_name: str, agent_url=None, client=None, rate_limit_retries=3):
            return {
                "answer": f"Web 回答：{query}",
                "contexts": [f"Web 切片：{query}"],
                "steps": [],
                "success": True,
                "error_type": None,
                "quota_exhausted": False,
            }

        latest_ans_path = os.path.join(self.tmp_dir.name, "web_latest_ans.json")
        latest_rpt_path = os.path.join(self.tmp_dir.name, "web_latest_rpt.json")

        with patch.object(eval_app, "_prepare_target_cases", return_value=cases), \
             patch.object(eval_app, "_call_agent_http", side_effect=_mock_call_agent_http), \
             patch.object(eval_app, "get_ragas_evaluator", return_value=fake_eval), \
             patch.object(eval_app, "export_evaluation_report", return_value="ok.xlsx"), \
             patch.object(eval_app, "ANSWERS_CHECKPOINT_JSONL", self.ans_ckpt), \
             patch.object(eval_app, "JUDGE_CHECKPOINT_JSONL", self.judge_ckpt), \
             patch.object(eval_app, "LATEST_ANSWERS_JSON", latest_ans_path), \
             patch.object(eval_app, "LATEST_REPORT_JSON", latest_rpt_path):
            client = TestClient(eval_app.app)
            resp = client.post(
                "/api/benchmark/run-pipeline-stream",
                json={"limit": 2, "answer_concurrency": 2, "judge_concurrency": 2},
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.text
            self.assertIn("event: pipeline_meta", body)
            self.assertIn("event: answer_done", body)
            self.assertIn("event: judge_done", body)
            self.assertIn("event: all_done", body)

            status_resp = client.get("/api/benchmark/checkpoint-status")
            self.assertEqual(status_resp.status_code, 200)
            self.assertEqual(status_resp.json()["code"], 200)

    def test_hybrid_dataset_generator_100_cases_and_cross_doc_pairs(self):
        docs_dir = os.path.join(self.tmp_dir.name, "mock_docs")
        os.makedirs(docs_dir, exist_ok=True)
        prefixes = [
            "01_法律合规_网络安全法", "02_法律合规_数据安全法", "03_TiDB官方_架构",
            "04_TiDB官方_悲观事务", "05_K8s官方_Pod生命周期", "06_K8s官方_RBAC鉴权",
            "07_FastAPI官方_异步并发", "09_FastAPI官方_OAuth2安全",
        ]
        all_names = []
        for idx in range(47):
            base = prefixes[idx] if idx < len(prefixes) else f"doc_{idx + 1:02d}_业务规范"
            fname = f"{base}.md"
            all_names.append(fname)
            content = (f"# {fname} 核心章节\n" + ("这是详细业务条款与参数配置说明。" * 450))
            with open(os.path.join(docs_dir, fname), "w", encoding="utf-8") as f:
                f.write(content)

        call_records = []
        lock = threading.Lock()

        def _fake_invoke_json(prompt: str, model_a: str, rate_limit_retries: int = 3, max_tokens: int = 2048):
            with lock:
                idx = len(call_records) + 1
                call_records.append((model_a, prompt))
            if "【文档 A：" in prompt:
                return [
                    {
                        "category": "📚 召回率-多跳聚合题",
                        "query": f"跨文档综合多跳问题 {idx}-1",
                        "ground_truth": "融合文档A与文档B的答案1",
                        "source_chunks": "摘录A+B",
                    },
                    {
                        "category": "📚 召回率-多跳聚合题",
                        "query": f"跨文档综合多跳问题 {idx}-2",
                        "ground_truth": "融合文档A与文档B的答案2",
                        "source_chunks": "摘录A+B",
                    },
                ]
            return [
                {
                    "category": "🔍 精度-高噪辨析题",
                    "query": f"单文档精准问题 {idx}-1",
                    "ground_truth": "标准答案1",
                    "source_chunks": "摘录1",
                },
                {
                    "category": "🎯 相关性-指令约束题",
                    "query": f"单文档约束问题 {idx}-2",
                    "ground_truth": "标准答案2",
                    "source_chunks": "摘录2",
                },
            ]

        with patch.object(dataset_generator, "_invoke_generator_json", side_effect=_fake_invoke_json):
            cases = dataset_generator.generate_benchmark_from_docs(
                docs_dir=docs_dir,
                max_workers=8,
                case_count=100,
            )

        self.assertEqual(len(cases), 100)
        self.assertEqual(len(call_records), 50)
        self.assertTrue(all(m == "glm-5.2" for m, _ in call_records))

        cross_cases = [c for c in cases if "+" in str(c.get("source_doc", ""))]
        self.assertEqual(len(cross_cases), 24)

        covered = set()
        for c in cases:
            for part in str(c.get("source_doc") or "").split("+"):
                if part.strip():
                    covered.add(part.strip())
        self.assertEqual(len(covered), 47)

        # 验证长文档确实采样了后续章节片段
        self.assertTrue(any("后续章节片段" in p for _, p in call_records))

    def test_invoke_generator_disables_thinking_and_falls_back(self):
        captured_kwargs = []

        def _fake_get_llm(**kwargs):
            captured_kwargs.append(kwargs)
            if "extra_body" in kwargs:
                class _UnsupportedLLM:
                    def invoke(self, prompt: str):
                        raise RuntimeError("Error 400: unknown parameter enable_thinking")
                return _UnsupportedLLM()

            class _OkLLM:
                def invoke(self, prompt: str):
                    return types.SimpleNamespace(
                        content='[{"category": "精度", "query": "测试提问", "ground_truth": "标准答案", "source_chunks": "原文"}]'
                    )
            return _OkLLM()

        with patch.object(dataset_generator, "get_deepseek_llm", side_effect=_fake_get_llm):
            res = dataset_generator._invoke_generator_json("请出题", model_a="glm-5.2")

        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["query"], "测试提问")
        self.assertEqual(len(captured_kwargs), 2)
        self.assertEqual(captured_kwargs[0]["max_tokens"], 4096)
        self.assertEqual(captured_kwargs[0]["extra_body"], {"enable_thinking": False})
        self.assertNotIn("extra_body", captured_kwargs[1])

    def test_extract_json_list_salvages_truncated_array(self):
        truncated = (
            '```json\n'
            '[\n'
            '  {"category": "精度", "query": "第1题完整问题？", "ground_truth": "答案1", "source_chunks": "片段1"},\n'
            '  {"category": "约束", "query": "第2题未截断的被中断\n'
        )
        salvaged = dataset_generator._extract_json_list_safely(truncated)
        self.assertEqual(len(salvaged), 1)
        self.assertEqual(salvaged[0]["query"], "第1题完整问题？")

    def test_incremental_backfill_and_deficit_recovery(self):
        docs_dir = os.path.join(self.tmp_dir.name, "inc_docs")
        os.makedirs(docs_dir, exist_ok=True)
        doc_names = []
        for idx in range(1, 21):
            fname = f"{idx:02d}_业务规范_{idx}.md"
            doc_names.append(fname)
            with open(os.path.join(docs_dir, fname), "w", encoding="utf-8") as f:
                f.write(f"# {fname}\n" + ("这是详细测试规则文本。" * 200))

        existing = [
            {
                "id": i,
                "category": "🔍 精度-高噪辨析题",
                "query": f"已有旧题 {i}",
                "ground_truth": f"旧答案 {i}",
                "source_chunks": "旧片段",
                "source_doc": doc_names[(i - 1) % 10],
                "review_status": "待确认",
            }
            for i in range(1, 49)
        ]

        counter = {"n": 0}
        lock = threading.Lock()
        events = []

        def _flaky_invoke_json(prompt: str, model_a: str, rate_limit_retries: int = 3, max_tokens: int = 4096):
            with lock:
                counter["n"] += 1
                cur = counter["n"]
            # 模拟前 3 次调用截断为空，验证自动缺口补齐机制会拉满到 100 题
            if cur <= 3:
                return []
            if "【文档 A：" in prompt:
                return [
                    {"category": "📚 召回率-多跳聚合题", "query": f"补齐跨文档题 {cur}-1", "ground_truth": "gt1", "source_chunks": "s1"},
                    {"category": "📚 召回率-多跳聚合题", "query": f"补齐跨文档题 {cur}-2", "ground_truth": "gt2", "source_chunks": "s2"},
                ]
            return [
                {"category": "🔍 精度-高噪辨析题", "query": f"补齐单文档题 {cur}-1", "ground_truth": "gt1", "source_chunks": "s1"},
                {"category": "🎯 相关性-指令约束题", "query": f"补齐单文档题 {cur}-2", "ground_truth": "gt2", "source_chunks": "s2"},
            ]

        with patch.object(dataset_generator, "_invoke_generator_json", side_effect=_flaky_invoke_json):
            final_cases = dataset_generator.generate_benchmark_from_docs(
                docs_dir=docs_dir,
                model_a="glm-5.2",
                max_workers=4,
                case_count=100,
                existing_cases=existing,
                on_progress=events.append,
            )

        self.assertEqual(len(final_cases), 100)
        self.assertEqual(final_cases[0]["query"], "已有旧题 1")
        self.assertEqual(final_cases[47]["query"], "已有旧题 48")
        ev_names = [e["event"] for e in events]
        self.assertIn("meta", ev_names)
        self.assertIn("batch_done", ev_names)
        self.assertTrue(any(e.get("is_backfill") for e in events if e.get("event") == "meta"))

    def test_generate_cases_stream_sse_endpoint(self):
        bench_dir = os.path.join(self.tmp_dir.name, "bench")
        docs_dir = os.path.join(self.tmp_dir.name, "sse_docs")
        os.makedirs(bench_dir, exist_ok=True)
        os.makedirs(docs_dir, exist_ok=True)
        for idx in range(1, 5):
            with open(os.path.join(docs_dir, f"0{idx}_doc.md"), "w", encoding="utf-8") as f:
                f.write(f"# 文档 {idx}\n详细内容描述。")

        json_path = os.path.join(bench_dir, "待审核基准测试集.json")
        seed_cases = _sample_cases(3)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(seed_cases, f, ensure_ascii=False, indent=2)

        seq = {"n": 0}

        def _fake_invoke(prompt: str, model_a: str, rate_limit_retries: int = 3, max_tokens: int = 4096):
            seq["n"] += 1
            return [
                {"category": "🔍 精度-高噪辨析题", "query": f"SSE 新增问题 {seq['n']}-1", "ground_truth": "gt1", "source_chunks": "c1"},
                {"category": "🎯 相关性-指令约束题", "query": f"SSE 新增问题 {seq['n']}-2", "ground_truth": "gt2", "source_chunks": "c2"},
            ]

        with patch.object(eval_app, "BENCHMARK_DIR", bench_dir), \
             patch.object(eval_app, "DOCS_DIR", docs_dir), \
             patch.object(dataset_generator, "_invoke_generator_json", side_effect=_fake_invoke):
            client = TestClient(eval_app.app)
            resp = client.post(
                "/api/benchmark/generate-cases-stream",
                json={"mode": "from-docs", "model_a": "glm-5.2", "case_count": 6, "append_existing": True},
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn("event: meta", body)
        self.assertIn("event: batch_done", body)
        self.assertIn("event: all_done", body)

        with open(json_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(len(saved), 6)
        self.assertEqual(saved[0]["query"], seed_cases[0]["query"])
        self.assertTrue(os.path.exists(os.path.join(bench_dir, "待审核基准测试集.xlsx")))

    def test_batch_ollama_embeddings_uses_unnormalized_endpoint_concurrently(self):
        import requests
        from common import models as common_models

        called_urls = []
        lock = threading.Lock()

        def _fake_post(self_sess, url, json=None, timeout=None):
            with lock:
                called_urls.append(url)
            prompt = (json or {}).get("prompt", "")
            idx = int(prompt.split("_")[-1]) if "_" in prompt else 0
            return types.SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"embedding": [float(idx), float(idx + 1)]},
            )

        client = common_models.BatchOllamaEmbeddings(base_url="http://127.0.0.1:11434", max_workers=4)
        texts = [f"chunk_{i}" for i in range(6)]
        with patch.object(requests.Session, "post", new=_fake_post):
            vecs = client.embed_documents(texts)
            qvec = client.embed_query("chunk_99")

        self.assertEqual(len(vecs), 6)
        self.assertEqual(vecs, [[float(i), float(i + 1)] for i in range(6)])
        self.assertEqual(qvec, [99.0, 100.0])
        self.assertTrue(all(u.endswith("/api/embeddings") for u in called_urls))

    def test_evaluator_disables_thinking_and_falls_back_on_400(self):
        captured_kwargs = []

        def _fake_get_llm(**kwargs):
            captured_kwargs.append(kwargs)
            if "extra_body" in kwargs:
                class _UnsupportedJudgeLLM:
                    def invoke(self, prompt: str):
                        raise RuntimeError("Error 400: unknown parameter enable_thinking")
                return _UnsupportedJudgeLLM()

            class _OkJudgeLLM:
                def invoke(self, prompt: str):
                    return types.SimpleNamespace(
                        content=json.dumps({
                            "faithfulness": 0.95,
                            "answer_relevance": 0.92,
                            "context_precision": 0.90,
                            "context_recall": 1.0,
                            "diagnoses": ["✅ 正常"],
                            "prescriptions": ["保持"],
                        })
                    )
            return _OkJudgeLLM()

        with patch.object(evaluator_mod, "get_deepseek_llm", side_effect=_fake_get_llm):
            engine = evaluator_mod.RagasEvaluator(model_name="kimi-k3")
            res = engine.evaluate_single(
                query="超时时间是多少？",
                response="3000ms",
                contexts=["超时阈值设定为 3000ms。"],
                ground_truth="3000ms",
            )

        self.assertTrue(res["is_valid"])
        self.assertEqual(len(captured_kwargs), 2)
        self.assertEqual(captured_kwargs[0]["max_tokens"], 4096)
        self.assertEqual(captured_kwargs[0]["extra_body"], {"enable_thinking": False})
        self.assertNotIn("extra_body", captured_kwargs[1])

    def test_agent_executor_uses_force_early_stopping(self):
        agent_py = os.path.join(PROJECT_ROOT, "Agentic_RAG", "agent.py")
        with open(agent_py, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn('early_stopping_method="force"', src)
        self.assertNotIn('early_stopping_method="generate"', src)

    def test_default_answer_model_and_compact_doc_list(self):
        from common import models as common_models
        from services.data_platform import mcp_server as data_mcp

        self.assertEqual(common_models.ALI_DEFAULT_ANSWER_MODEL, "qwen3.6-flash-2026-04-16")
        self.assertNotIn("qwen3.8-27b", common_models.SUPPORTED_MODELS)
        self.assertNotIn("qwen3.6-plus-2026-04-02", common_models.SUPPORTED_MODELS)
        self.assertNotIn("deepseek-v4.1-flash", common_models.SUPPORTED_MODELS)
        self.assertIn("qwen3.6-flash-2026-04-16", common_models.SUPPORTED_MODELS)

        class _FakeVS:
            def list_all_documents(self):
                return [
                    {"doc_name": "a.md", "chunk_count": 5, "file_size_formatted": "2.0 KB"},
                    {"doc_name": "b.md", "chunk_count": 12, "file_size_formatted": "8.0 KB"},
                ]

        with patch.object(data_mcp, "get_vector_store", return_value=_FakeVS()):
            out = data_mcp.list_available_documents()
        self.assertIn("知识库共 2 篇文档", out)
        self.assertIn("- a.md (5切片)", out)
        self.assertIn("- b.md (12切片)", out)


if __name__ == "__main__":
    unittest.main()
