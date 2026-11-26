# -*- coding: utf-8 -*-
"""
RAG_project 重构优化全链路离线自动化验收测试 (unittest)。
不依赖外部大模型网络或本地 Ollama 服务，可在任意 Python >=3.12 环境快速验证核心修复。
"""

import asyncio
import importlib
import importlib.util
import logging
import os
import py_compile
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


class TestCompileAndSyntax(unittest.TestCase):
    """1. 全工程核心 Python 源码语法编译检查。"""

    TARGET_FILES = [
        "app.py",
        "common/__init__.py",
        "common/logging_config.py",
        "common/models.py",
        "Agentic_RAG/app.py",
        "Agentic_RAG/agent.py",
        "Agentic_RAG/mcp_client.py",
        "Agentic_RAG/models.py",
        "Agentic_RAG/schemas.py",
        "Agentic_RAG/tools.py",
        "Agentic_RAG/routers/chat.py",
        "Agentic_RAG/routers/session.py",
        "services/data_platform/app.py",
        "services/data_platform/chat_history_db.py",
        "services/data_platform/mcp_server.py",
        "services/data_platform/models.py",
        "services/data_platform/reranker.py",
        "services/data_platform/schemas.py",
        "services/data_platform/vector_store.py",
        "services/eval_platform/app.py",
        "services/eval_platform/benchmark_runner.py",
        "services/eval_platform/dataset_generator.py",
        "services/eval_platform/evaluator.py",
        "services/eval_platform/models.py",
        "services/eval_platform/schemas.py",
    ]

    def test_all_python_files_compile(self):
        for rel_path in self.TARGET_FILES:
            full_path = PROJECT_ROOT / rel_path
            self.assertTrue(full_path.exists(), f"文件不存在: {rel_path}")
            py_compile.compile(str(full_path), doraise=True)


class TestLoggingConfig(unittest.TestCase):
    """2. common/logging_config.py 防套娃工厂与脱敏测试。"""

    def test_get_logger_installs_factory_once(self):
        from common import logging_config

        before_factory = logging.getLogRecordFactory()
        l1 = logging_config.get_logger("test.logger.a")
        after_first = logging.getLogRecordFactory()
        l2 = logging_config.get_logger("test.logger.b")
        after_second = logging.getLogRecordFactory()

        self.assertIsNotNone(l1)
        self.assertIsNotNone(l2)
        self.assertIs(after_first, after_second)
        self.assertIsNotNone(before_factory)

    def test_mask_sensitive(self):
        from common.logging_config import mask_sensitive

        masked = mask_sensitive("Authorization: Bearer sk-1234567890abcdef and phone 13812345678")
        self.assertNotIn("sk-1234567890abcdef", masked)
        self.assertNotIn("13812345678", masked)
        self.assertIn("138****5678", masked)


class TestDataPlatformChatHistoryDB(unittest.TestCase):
    """3. 会话数据库自定义 session_id 补建与外键级联删除测试。"""

    def test_custom_session_id_prevents_fk_error(self):
        from services.data_platform.chat_history_db import DataPlatformChatDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_chat.db")
            db = DataPlatformChatDB(db_path=db_path)

            custom_id = "custom-session-001"
            created = db.create_session(title="补建会话", session_id=custom_id)
            self.assertEqual(created["session_id"], custom_id)

            msg_id = db.add_message(
                session_id=custom_id,
                role="user",
                content="测试消息外键完整性",
                steps=[{"tool": "search_knowledge_base"}],
                referenced_docs=["doc_a.md"],
            )
            self.assertGreater(msg_id, 0)

            messages = db.get_session_messages(custom_id)
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["referenced_docs"], ["doc_a.md"])

            self.assertTrue(db.delete_session(custom_id))
            self.assertIsNone(db.get_session(custom_id))
            self.assertEqual(db.get_session_messages(custom_id), [])


class TestMCPClientAndToolsFallback(unittest.TestCase):
    """4. MCP 降级参数匹配与运行中事件循环安全隔离测试。"""

    def test_call_mcp_tool_sync_inside_running_event_loop_and_fallback(self):
        agentic_dir = str(PROJECT_ROOT / "Agentic_RAG")
        if agentic_dir not in sys.path:
            sys.path.insert(0, agentic_dir)

        import mcp_client

        async def _broken_mcp(*args, **kwargs):
            raise ConnectionError("模拟 MCP 端口未启动")

        def _fallback_search(query: str, doc_name_filter=None):
            return f"fallback:{query}|{doc_name_filter}"

        async def _invoke_from_loop():
            with mock.patch.object(mcp_client, "_async_call_mcp_tool", side_effect=_broken_mcp):
                return mcp_client.call_mcp_tool_sync(
                    tool_name="search_knowledge_base",
                    arguments={"query": "超时配置", "doc_name_filter": "规范.md"},
                    fallback_func=_fallback_search,
                    trace_id="trace-123",
                )

        result = asyncio.run(_invoke_from_loop())
        self.assertEqual(result, "fallback:超时配置|规范.md")

    @unittest.skipUnless(_has_module("langchain_core"), "当前环境未安装 langchain_core")
    def test_tools_fallback_signatures_match_mcp_arguments(self):
        agentic_dir = str(PROJECT_ROOT / "Agentic_RAG")
        if agentic_dir not in sys.path:
            sys.path.insert(0, agentic_dir)

        import mcp_client
        import tools

        with mock.patch.object(mcp_client, "_async_call_mcp_tool", side_effect=RuntimeError("mcp down")):
            with mock.patch.object(tools, "_call_data_platform_retrieve", return_value="mocked-retrieve-ok"):
                res_search = tools.search_knowledge_base.invoke({
                    "query": "网关超时",
                    "doc_name_filter": "架构.md",
                })
                self.assertEqual(res_search, "mocked-retrieve-ok")

            fake_resp = mock.MagicMock(status_code=200)
            fake_resp.json.return_value = {"data": {"formatted_text": "mocked-read-ok"}}
            with mock.patch("requests.get", return_value=fake_resp):
                res_read = tools.read_specific_document.invoke({
                    "doc_name": "架构.md",
                    "max_chunks": 4,
                })
                self.assertEqual(res_read, "mocked-read-ok")

            fake_docs_resp = mock.MagicMock(status_code=200)
            fake_docs_resp.json.return_value = {
                "data": {"documents": [{"doc_name": "架构.md", "chunk_count": 3, "file_size_formatted": "2.0 KB"}]}
            }
            with mock.patch("requests.get", return_value=fake_docs_resp):
                res_list = tools.list_available_documents.invoke({})
                self.assertIn("架构.md", res_list)


class TestFrontendBackendContracts(unittest.TestCase):
    """5. 前后端静态契约与已知缺陷回归检查。"""

    def test_benchmark_js_and_eval_app_contracts(self):
        benchmark_js = (PROJECT_ROOT / "services/eval_platform/static/js/benchmark.js").read_text(encoding="utf-8")
        self.assertNotIn("cachedConfig", benchmark_js)
        self.assertIn("currentFilteredJudgeCases", benchmark_js)
        self.assertIn("caseItem.qa_latency ?? caseItem.latency", benchmark_js)

        eval_app_py = (PROJECT_ROOT / "services/eval_platform/app.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/benchmark/export-cases"', eval_app_py)
        self.assertIn('"persist": False', eval_app_py)

    def test_index_js_sse_and_agent_streaming_contracts(self):
        index_js = (PROJECT_ROOT / "Agentic_RAG/static/js/index.js").read_text(encoding="utf-8")
        loop_idx = index_js.index("while (true) {")
        event_decl_idx = index_js.index("let currentEvent = 'message';")
        self.assertLess(event_decl_idx, loop_idx, "currentEvent 必须声明在 SSE 读取循环之外")
        self.assertNotIn("startUploadWithPrecheck", index_js)

        agent_py = (PROJECT_ROOT / "Agentic_RAG/agent.py").read_text(encoding="utf-8")
        self.assertIn("streaming=True", agent_py)
        self.assertIn("async def ask_stream_async", agent_py)

    def test_chat_request_persist_field(self):
        from Agentic_RAG.schemas import ChatRequest

        req_default = ChatRequest(query="你好")
        req_ephemeral = ChatRequest(query="评测问题", persist=False)
        self.assertTrue(req_default.persist)
        self.assertFalse(req_ephemeral.persist)


class TestEvalRunnerAndSummary(unittest.TestCase):
    """6. 评测汇总逻辑与 Excel 导出验证。"""

    @unittest.skipUnless(_has_module("openpyxl"), "当前环境未安装 openpyxl")
    def test_build_summary_and_export_excel(self):
        eval_dir = str(PROJECT_ROOT / "services/eval_platform")
        if eval_dir not in sys.path:
            sys.path.insert(0, eval_dir)
        import benchmark_runner

        case_results = [
            {
                "id": 1,
                "category": "🔍 精度-高噪辨析题",
                "query": "超时时间是多少？",
                "ground_truth": "3000ms",
                "system_answer": "3000ms",
                "contexts_count": 2,
                "contexts_preview": "片段1",
                "scores": {
                    "rag_triad_score": 0.95,
                    "faithfulness": 1.0,
                    "answer_relevance": 0.95,
                    "context_precision": 0.90,
                    "context_recall": 1.0,
                },
                "is_valid": True,
                "is_passed": True,
                "diagnoses": ["健康"],
                "prescriptions": ["保持"],
                "total_latency": 1.2,
            }
        ]
        summary = benchmark_runner.build_benchmark_summary(
            case_results=case_results,
            model_b="qwen3.8-flash",
            model_c="qwen3.8-max-0902",
            total_elapsed=1.5,
        )
        self.assertEqual(summary["passed_cases"], 1)
        self.assertEqual(summary["valid_pass_rate"], 1.0)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_xlsx = os.path.join(tmpdir, "report.xlsx")
            benchmark_runner.export_evaluation_report(summary, out_xlsx)
            self.assertTrue(os.path.exists(out_xlsx))
            self.assertGreater(os.path.getsize(out_xlsx), 0)


class TestAgentContextSplitting(unittest.TestCase):
    """7. Agent 精读切片拆分、无效报错过滤与跨工具去重测试。"""

    @unittest.skipUnless(_has_module("langchain_core"), "当前环境未安装 langchain_core")
    def test_split_and_dedupe_contexts_handles_read_chunks_and_duplicates(self):
        agentic_dir = str(PROJECT_ROOT / "Agentic_RAG")
        if agentic_dir not in sys.path:
            sys.path.insert(0, agentic_dir)
        import agent

        search_obs = (
            "【来源 1】文档: 《测试规范.md》 (切片 2/3)\n"
            "相关度距离: 320.0 | 重排得分: 0.85\n"
            "内容片段:\n"
            "第二章正文：超时时间为 3000ms。\n"
            "----------------------------------------"
        )
        read_obs = (
            "=== 正在精读文档《测试规范.md》前 3 个章节切片 ===\n\n"
            "【切片 1/3】\n第一章正文：架构概览。\n----------------------------------------\n\n"
            "【切片 2/3】\n第二章正文：超时时间为 3000ms。\n----------------------------------------\n\n"
            "【切片 3/3】\n第三章正文：熔断冷却 30 秒。\n----------------------------------------"
        )
        error_obs = "知识库中未找到名为《不存在文档》的文档。"

        contexts = agent._split_and_dedupe_contexts([error_obs, search_obs, read_obs])
        self.assertEqual(len(contexts), 3)
        joined = "\n".join(contexts)
        self.assertNotIn("未找到名为", joined)
        self.assertIn("第一章正文", joined)
        self.assertIn("第二章正文", joined)
        self.assertIn("第三章正文", joined)


if __name__ == "__main__":
    unittest.main()
