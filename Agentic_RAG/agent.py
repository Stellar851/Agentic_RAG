"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
智能体编排与多跳检索模块 (agent.py)

【模块定位】
本文件是系统的"智能中枢"，实现了方案一 (Agentic RAG / Tool-Calling ReAct Agent)：
1. 解决核心痛点：多文档关联、概念穿透、多跳检索 (Multi-Hop Reasoning)。
2. 工具集合 (Tools)：
   - `list_available_documents`: 知识库宏观感知工具，获取所有可用文档名称。
   - `search_knowledge_base`: 向量语义检索工具，支持全库跨文档检索或单文档定向检索。
   - `read_specific_document`: 连续段落精读工具，支持查看指定文档的上下文脉络。
3. 智能决策循环 (ReAct Loop)：
   大模型根据用户提问，自主规划思考 (Thought)、决定调用何种工具 (Action)、
   观察检索结果 (Observation)，若发现信息不完整或出现交叉引用，主动发起第二跳、
   第三跳检索，最终融合多文档结论输出完整解答。
4. 思考过程追踪：
   捕获 Agent 的完整思考与工具调用轨迹，供前端可视化展示。
================================================================================
"""

import os
import sys
import re
import queue
import asyncio
import threading
import traceback
import functools
from typing import List, Dict, Any, Tuple, Optional

# 确保所有 print 自动强制刷新
print = functools.partial(print, flush=True)

# 确保 Windows 终端输出 UTF-8 编码，防止 emoji 或特殊字符在 GBK 环境下抛出 UnicodeEncodeError
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage

try:
    from langchain.agents import create_tool_calling_agent, AgentExecutor
except ImportError:
    from langchain_classic.agents import create_tool_calling_agent, AgentExecutor

# 导入内部模块
from models import get_deepseek_llm
from tools import (
    DATA_PLATFORM_URL,
    AGENT_TOOLS,
    list_available_documents,
    search_knowledge_base,
    read_specific_document,
    _get_data_platform_doc_names,
    _call_data_platform_retrieve,
)

try:
    from common.logging_config import get_trace_id, set_trace_id, reset_trace_id
except Exception:
    get_trace_id = lambda: "-"
    set_trace_id = lambda _tid=None: None
    reset_trace_id = lambda: None

# Agent 循环治理参数：多跳题允许 8 次工具迭代；超过预算后强制进入证据合成阶段。
AGENT_MAX_ITERATIONS = max(1, int(os.getenv("RAG_AGENT_MAX_ITERATIONS", "8")))
AGENT_RECOVERY_CONTEXT_CHARS = max(2000, int(os.getenv("RAG_AGENT_RECOVERY_CONTEXT_CHARS", "12000")))
AGENT_LLM_TIMEOUT_SECONDS = max(15, int(os.getenv("RAG_AGENT_LLM_TIMEOUT_SECONDS", "90")))
AGENT_LLM_MAX_RETRIES = max(0, int(os.getenv("RAG_AGENT_LLM_MAX_RETRIES", "1")))

AGENT_STOP_MARKERS = (
    "agent stopped due to max iterations",
    "agent stopped due to max iteration",
    "agent stopped due to iteration limit",
    "iteration limit or time limit",
)


def is_agent_stop_output(text: Any) -> bool:
    """识别 LangChain Agent 因迭代上限返回的内部终止文案。"""
    normalized = " ".join(str(text or "").lower().split())
    return any(marker in normalized for marker in AGENT_STOP_MARKERS)


def build_agent_failure_answer(contexts: Optional[List[str]] = None) -> str:
    """生成不泄露内部异常、且明确说明证据不足的降级回答。"""
    if contexts:
        evidence = " ".join(str(c) for c in contexts[:2]).strip()
        evidence = evidence[:360] + ("..." if len(evidence) > 360 else "")
        return (
            "系统未能在限定步骤内完成完整推理，因此当前无法给出可靠结论。"
            "以下为已检索到的部分线索，仅供缩小问题范围后重试：\n\n"
            f"{evidence}"
        )
    return "系统未能在限定步骤内完成检索与推理，当前无法给出可靠回答。请缩小问题范围后重试。"


_INVALID_OBSERVATION_MARKERS = (
    "未能检索到",
    "未检索到任何",
    "未能初筛到",
    "知识库中未找到名为",
    "未找到名为",
    "未能从数据中台",
)


def _context_body_fingerprint(part: str) -> str:
    """剥离来源/切片元信息头与分隔线，提取正文归一化指纹用于跨工具去重。"""
    body = str(part or "").strip()
    body = re.sub(
        r"^(?:《[^》]+》\s*)?【来源\s*\d+】文档:\s*《.*?》[^\n]*\n相关度距离:[^\n]*\n内容片段:\s*\n?",
        "",
        body,
    )
    body = re.sub(r"^(?:《[^》]+》\s*)?【切片\s*\d+/\d+】\s*\n?", "", body)
    body = re.sub(r"^---\s*\[切片\s*\d+\]\s*---\s*\n?", "", body)
    body = re.sub(r"(?m)^-{10,}\s*$", "", body)
    normalized = re.sub(r"\s+", " ", body).strip()
    return normalized or re.sub(r"\s+", " ", str(part or "")).strip()


def _split_and_dedupe_contexts(contexts: Optional[List[str]]) -> List[str]:
    """将工具观察结果拆成独立证据块并去重，避免长精读文本被截断或重复计分。"""
    collected: List[str] = []
    seen = set()

    for raw in contexts or []:
        text = str(raw or "").strip()
        if not text:
            continue

        if "【来源 " in text:
            parts = re.split(r"\n{2,}(?=【来源\s*\d+】)", text)
        elif re.search(r"【切片\s*\d+/\d+】", text):
            header_match = re.match(r"^===\s*正在精读文档《(.*?)》[^\n]*===\s*", text)
            doc_name = header_match.group(1).strip() if header_match else ""
            cleaned_text = text[header_match.end():].strip() if header_match else text
            parts = re.split(r"\n{0,2}(?=【切片\s*\d+/\d+】)", cleaned_text)
            if doc_name:
                parts = [
                    p if (not p.strip() or doc_name in p) else f"《{doc_name}》 {p.strip()}"
                    for p in parts
                ]
        elif "--- [切片" in text:
            parts = re.split(r"\n{0,2}(?=--- \[切片)", text)
        else:
            parts = [text]

        for part in parts:
            part = part.strip()
            if not part:
                continue
            if any(marker in part for marker in _INVALID_OBSERVATION_MARKERS):
                continue
            fingerprint = _context_body_fingerprint(part)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            collected.append(part)

    return collected


def collect_agent_contexts(intermediate_steps: List[Any]) -> List[str]:
    """从 Agent 工具轨迹中提取真实检索证据，供最终合成与 Ragas 评估复用。"""
    observations = []
    for step in intermediate_steps or []:
        try:
            action, observation = step
        except (TypeError, ValueError):
            continue
        if getattr(action, "tool", "") in {"search_knowledge_base", "read_specific_document"}:
            observations.append(str(observation or ""))
    return _split_and_dedupe_contexts(observations)


def _budget_contexts(contexts: List[str], total_chars: int = AGENT_RECOVERY_CONTEXT_CHARS) -> str:
    """按证据块公平分配字符预算，优先保留多跳题中后段检索到的事实。"""
    if not contexts:
        return "【无可用证据】"

    per_item = max(500, total_chars // min(len(contexts), 12))
    selected = []
    for index, context in enumerate(contexts[:12], 1):
        clipped = context[:per_item]
        if len(context) > per_item:
            clipped += "..."
        selected.append(f"【证据 {index}】\n{clipped}")
    return "\n\n".join(selected)


def _format_chat_history(chat_history: Optional[List[Dict[str, str]]]) -> List[Any]:
    """将历史对话字典列表转换为 LangChain 消息对象（保留最近 5 轮上下文）。"""
    formatted: List[Any] = []
    if not chat_history:
        return formatted
    for msg in chat_history[-10:]:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "user":
            formatted.append(HumanMessage(content=content))
        elif role == "assistant":
            formatted.append(AIMessage(content=content))
    return formatted


def _extract_steps_and_docs(
    intermediate_steps: List[Any],
    include_full_observation: bool = True
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """从 Agent 执行轨迹中提取结构化步骤与经数据中台校验后的引用文档清单。"""
    steps_trace: List[Dict[str, Any]] = []
    referenced_docs = set()

    for step in intermediate_steps or []:
        action, observation = step
        tool_name = action.tool
        tool_input = action.tool_input
        log_text = action.log

        if isinstance(tool_input, dict) and "doc_name" in tool_input and tool_input["doc_name"]:
            referenced_docs.add(tool_input["doc_name"])
        if isinstance(tool_input, dict) and tool_input.get("doc_name_filter"):
            referenced_docs.add(tool_input["doc_name_filter"])

        obs_str = str(observation)
        for m in re.findall(r"【来源\s*\d+】文档:\s*《(.*?)》", obs_str):
            referenced_docs.add(m)

        step_item: Dict[str, Any] = {
            "tool": tool_name,
            "input": tool_input,
            "thought": log_text.strip() if log_text else f"调用工具: {tool_name}",
            "observation_preview": (obs_str[:300] + "...") if len(obs_str) > 300 else obs_str,
        }
        if include_full_observation:
            step_item["observation"] = obs_str
        steps_trace.append(step_item)

    valid_existing_docs = _get_data_platform_doc_names()
    filtered_docs = sorted(d for d in referenced_docs if d in valid_existing_docs)
    return steps_trace, filtered_docs


# ==============================================================================
# 一、 智能体工具库 (Agent Tools)
# 架构优化解耦：工具定义与数据中台 HTTP/MCP 逻辑已独立封装至 tools.py 维护
# ==============================================================================

# ==============================================================================
# 二、 Agent 系统提示词设计 (Prompt Template)
# ==============================================================================

AGENT_SYSTEM_PROMPT = """你是一个企业级智能知识库首席分析专家，专注于【多文档关联分析与复杂逻辑推理】。
你的任务是通过高效使用提供的知识库工具，解答用户提出的各种复杂问题。

【核心原则：多文档关联与多跳检索 (Multi-Hop)】
1. **宏观感知**：如果用户提问涉及多个文档、综合对比、或者概念范围不明确，你可以先使用 `list_available_documents` 查看有哪些可用文档。
2. **多跳追问与穿透关联**：
   - 当在文档 A 中检索到某个重要概念、系统名词、上游依赖或接口定义时，如果文档 A 未作详述，你**必须主动发起第二轮或多轮检索**，查询文档 B、文档 C 中对应的定义或细节。
   - 切忌仅凭单一文档的零散片段断章取义！遇到跨文档关联问题，必须收集齐所有关联文档的关键证据后再给出结论。
3. **忠于事实，杜绝幻觉**：
   - 所有的事实、数据、结论必须严格基于本次实际检索到的知识库内容。
   - 严禁引用或推测本次未实际检索到的文档名称、具体条款编号或外部常识来补全论证。
   - 如果检索了多次仍然找不到相关信息，请诚实说明哪部分信息缺失，不要编造答案。
4. **多轮对话与指代消解 (Coreference Resolution)**：
   - 在多轮连续对话中，用户提问往往包含代词、比较或省略主语（例如“它有哪些模块？”、“与方案A相比呢？”、“这个怎么配置？”）。
   - 你必须结合之前的对话历史，精准识别用户所指代的具体实体、方案或文档。
   - 在调用 `search_knowledge_base` 等工具时，**【绝对禁止】直接拿模糊代词（如“它”、“这个”）作为查询词检索**！你必须自主将检索词改写为包含具体实体全称的明确独立查询词（例如：上一轮在聊“网关系统”，用户追问“它的端口是多少”时，你应使用 `search_knowledge_base(query='网关系统 端口号 配置')` 发起检索）。
5. **检索预算与证据收敛**：
   - 用户点名了具体文档时，优先调用 `search_knowledge_base(..., doc_name_filter="完整文档名")`，不要先做全库泛化检索。
   - **优先精准检索，克制整篇精读**：优先使用 `search_knowledge_base` 精准定位目标条款；严禁在未先检索定位前直接调用 `read_specific_document` 通读整篇文档，仅当检索片段被截断、缺少相邻条款或需要确认边界拒答时，才按需调用 `read_specific_document` 补读。
   - 同一问题不要重复执行相同检索；通常 2-4 次定向检索足够，原则上不要超过 4 次工具调用。
   - 一旦已获得回答所必需的核心证据，立即停止检索并作答，切勿为了“更完整”无限追加工具调用。
   - 如果证据不足，明确列出缺失点并基于已知内容回答，不得暴露 `Agent stopped due to max iterations` 等内部状态。
6. **输出格式优先级与回答结构**：
   - **最高优先级（严格服从用户显式格式约束）**：当用户提问中明确指定了输出格式、行数、编号/前缀或禁用项（例如“仅用三行”“格式为 X=Y”“每行必须以……开头”“不得包含背景、解释、总结、来源或任何额外文字”）时，**必须 100% 严格按用户指定格式输出纯文本内容**：
     - 绝对禁止额外附加“核心结论”“关联分析”“参考来源”或“来源：……”等段落；
     - 绝对禁止擅自添加 Markdown 加粗（如 `**第一阶段**：`）或保留原文表格中的额外副标签（如“绝不妥协：”“受限准入：”“全面放行：”），必须严格对齐用户指定的前缀与格式模板。
   - **默认回答结构**：仅当用户**未指定**输出格式时，才采用以下三段式结构：
     - **核心结论**：开门见山给出回答的主旨。
     - **关联分析/详细阐述**：分条理展开，清晰点出各个文档之间的逻辑链条（如：“依据《文档A》，...；同时《文档B》指出...”）。
     - **参考来源**：在末尾明确列出本次实际检索到的文档及相应章节/片段。
"""


# ==============================================================================
# 三、 Agent 构建器与问答执行引擎 (AgenticRAGService)
# ==============================================================================

class AgenticRAGService:
    """
    【类名】AgenticRAGService
    【功能】提供给 Web 接口调用的单例服务，封装 Agent 初始化、思考轨迹解析与结果封装
    【热切换特性】支持按请求动态传入 model_name，按需创建并缓存对应的 Tool-Calling AgentExecutor
    """

    def __init__(self):
        """初始化底层基础工具与提示词模板，执行器按需懒加载并缓存"""
        self.tools = AGENT_TOOLS
        # 缓存不同 (模型名称, streaming) 对应的 Agent Runnable 实例
        self._agents: Dict[Tuple[str, bool], Any] = {}
        # 迭代预算耗尽后的最终答案合成器按模型复用
        self._recovery_llms: Dict[str, Any] = {}

        self.prompt = ChatPromptTemplate.from_messages([
            ("system", AGENT_SYSTEM_PROMPT),
            MessagesPlaceholder(variable_name="chat_history"),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

    def _get_executor(
        self,
        model_name: Optional[str] = None,
        verbose: bool = False,
        streaming: bool = False
    ) -> Tuple[AgentExecutor, str]:
        """
        根据指定模型名称与流式标志获取线程安全的 AgentExecutor 实例。
        底层按 (actual_model, streaming) 复用不可变 Agent Runnable。
        """
        from models import ACTIVE_LLM_MODEL

        actual_model = model_name.strip() if model_name and model_name.strip() else ACTIVE_LLM_MODEL
        cache_key = (actual_model, bool(streaming))

        if cache_key not in self._agents:
            llm = get_deepseek_llm(
                model=actual_model,
                temperature=0.2,
                streaming=bool(streaming),
                timeout=AGENT_LLM_TIMEOUT_SECONDS,
                max_retries=AGENT_LLM_MAX_RETRIES,
            )
            agent = create_tool_calling_agent(
                llm=llm,
                tools=self.tools,
                prompt=self.prompt,
            )
            self._agents[cache_key] = agent

        executor = AgentExecutor(
            agent=self._agents[cache_key],
            tools=self.tools,
            verbose=verbose,
            return_intermediate_steps=True,
            max_iterations=AGENT_MAX_ITERATIONS,
            early_stopping_method="generate",
        )
        return executor, actual_model

    def _recover_answer_from_contexts(
        self,
        user_query: str,
        contexts: List[str],
        actual_model: str,
    ) -> Optional[str]:
        """工具预算耗尽后，基于已召回证据执行一次无工具最终合成。"""
        if not contexts:
            return None

        if actual_model not in self._recovery_llms:
            self._recovery_llms[actual_model] = get_deepseek_llm(
                model=actual_model,
                temperature=0.1,
                streaming=False,
                timeout=AGENT_LLM_TIMEOUT_SECONDS,
                max_retries=AGENT_LLM_MAX_RETRIES,
            )

        llm = self._recovery_llms[actual_model]
        evidence = _budget_contexts(contexts)
        prompt = f"""你是企业知识库的最终答案合成器。工具调用预算已经耗尽，禁止再请求任何工具。
请严格依据下方已检索证据回答用户问题。

要求：
1. 直接回答问题；用户指定了格式、项数或禁用内容时，必须严格遵守。
2. 只使用证据中明确出现的事实、数字、名称和条款，不得补写证据未支持的信息。
3. 多个证据冲突时，优先采用正文条款；无法判断时明确指出冲突。
4. 证据不足时，先给出能够确认的结论，再明确列出缺失信息，严禁编造。
5. 不要提及迭代次数、内部工具、系统错误或“无法继续调用工具”。

【用户问题】
{user_query}

【已检索证据】
{evidence}
"""
        try:
            response = llm.invoke(prompt)
            answer = response.content.strip() if hasattr(response, "content") else str(response).strip()
        except Exception as exc:
            print(f"  ⚠ 证据合成失败，使用本地降级回答: {str(exc)[:100]}")
            return None

        if not answer or is_agent_stop_output(answer):
            return None
        return answer

    def ask(
        self,
        user_query: str,
        chat_history: Optional[List[Dict[str, str]]] = None,
        model_name: Optional[str] = None,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """接收用户提问，执行多跳 Agentic 检索与推理，返回结构化答案与思考证据链。"""
        formatted_history = _format_chat_history(chat_history)

        try:
            executor, actual_model = self._get_executor(model_name, verbose=verbose, streaming=False)
            result = executor.invoke({
                "input": user_query,
                "chat_history": formatted_history,
            })

            answer_text = result.get("output", "")
            intermediate_steps = result.get("intermediate_steps", [])
            steps_trace, filtered_docs = _extract_steps_and_docs(intermediate_steps, include_full_observation=True)
            contexts_collected = collect_agent_contexts(intermediate_steps)

            degraded = False
            recovered_from = None
            if is_agent_stop_output(answer_text):
                recovered = self._recover_answer_from_contexts(
                    user_query=user_query,
                    contexts=contexts_collected,
                    actual_model=actual_model,
                )
                answer_text = recovered or build_agent_failure_answer(contexts_collected)
                degraded = True
                recovered_from = "agent_max_iterations"

            return {
                "answer": answer_text,
                "steps": steps_trace,
                "referenced_docs": filtered_docs,
                "contexts": contexts_collected,
                "model_name": actual_model,
                "success": True,
                "degraded": degraded,
                "recovered_from": recovered_from,
                "error_stage": None,
                "error_type": None,
                "retry_recommended": False,
            }

        except Exception as e:
            err_str = str(e)
            if "quota" in err_str.lower() or "403" in err_str:
                short_err = "阿里百炼 API 免费额度已耗尽 (Free quota exhausted，请检查账号余额或 API Key)"
                error_type = "agent_quota"
            elif "401" in err_str or "invalid_api_key" in err_str.lower():
                short_err = "阿里百炼 API Key 无效或未正确配置"
                error_type = "agent_auth"
            elif "11434" in err_str or "ollama" in err_str.lower():
                short_err = "Ollama 本地服务未运行或连接超时 (http://localhost:11434)"
                error_type = "agent_embedding_unavailable"
            else:
                short_err = f"{err_str[:120]}"
                error_type = "agent_exception"

            print(f"  ❌ 作答异常: {short_err}")
            return {
                "answer": f"抱歉，系统作答遇到异常：{short_err}",
                "steps": [],
                "referenced_docs": [],
                "contexts": [],
                "model_name": model_name or "unknown",
                "success": False,
                "error_stage": "agent",
                "error_type": error_type,
                "retry_recommended": True,
            }

    def ask_stream(
        self,
        user_query: str,
        chat_history: Optional[List[Dict[str, str]]] = None,
        model_name: Optional[str] = None,
    ):
        """
        流式问答生成器：实时产生思维链状态（step_start / step_end）和逐字打字机 token。
        通过 Queue 传递事件，支持 Web 前端 Server-Sent Events (SSE) 逐字平滑渲染。
        """
        event_queue: queue.Queue = queue.Queue()
        parent_trace_id = get_trace_id()

        class StreamCallbackHandler(BaseCallbackHandler):
            def __init__(self, q: queue.Queue):
                self.q = q
                self.in_tool = False

            def on_llm_new_token(self, token: str, **kwargs) -> None:
                if not self.in_tool and token:
                    self.q.put({"event": "token", "data": {"delta": token}})

            def on_tool_start(self, serialized, input_str, **kwargs) -> None:
                self.in_tool = True
                name = serialized.get("name", "tool") if isinstance(serialized, dict) else str(serialized)
                self.q.put({
                    "event": "step_start",
                    "data": {
                        "tool": name,
                        "input": str(input_str)[:200],
                    },
                })

            def on_tool_end(self, output, **kwargs) -> None:
                self.in_tool = False
                obs_str = str(output)
                self.q.put({
                    "event": "step_end",
                    "data": {
                        "observation_preview": (obs_str[:300] + "...") if len(obs_str) > 300 else obs_str,
                    },
                })

        formatted_history = _format_chat_history(chat_history)
        handler = StreamCallbackHandler(event_queue)

        def worker():
            if parent_trace_id and parent_trace_id != "-":
                set_trace_id(parent_trace_id)
            try:
                executor, actual_model = self._get_executor(model_name, streaming=True)
                result = executor.invoke(
                    {"input": user_query, "chat_history": formatted_history},
                    config={"callbacks": [handler]},
                )
                answer_text = result.get("output", "")
                intermediate_steps = result.get("intermediate_steps", [])
                steps_trace, filtered_docs = _extract_steps_and_docs(
                    intermediate_steps,
                    include_full_observation=False,
                )
                contexts_collected = collect_agent_contexts(intermediate_steps)

                degraded = False
                recovered_from = None
                if is_agent_stop_output(answer_text):
                    recovered = self._recover_answer_from_contexts(
                        user_query=user_query,
                        contexts=contexts_collected,
                        actual_model=actual_model,
                    )
                    answer_text = recovered or build_agent_failure_answer(contexts_collected)
                    degraded = True
                    recovered_from = "agent_max_iterations"

                event_queue.put({
                    "event": "done",
                    "data": {
                        "answer": answer_text,
                        "steps": steps_trace,
                        "referenced_docs": filtered_docs,
                        "contexts": contexts_collected,
                        "model_name": actual_model,
                        "success": True,
                        "degraded": degraded,
                        "recovered_from": recovered_from,
                    },
                })

            except Exception as e:
                traceback.print_exc()
                event_queue.put({
                    "event": "error",
                    "data": {
                        "message": f"Agent 执行异常: {str(e)}",
                        "error_stage": "agent",
                        "error_type": "agent_exception",
                        "retry_recommended": True,
                    },
                })
            finally:
                event_queue.put(None)
                reset_trace_id()

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        while True:
            item = event_queue.get()
            if item is None:
                break
            yield item

    async def ask_stream_async(
        self,
        user_query: str,
        chat_history: Optional[List[Dict[str, str]]] = None,
        model_name: Optional[str] = None,
    ):
        """异步流式包装器：通过线程队列桥接避免阻塞 FastAPI 主事件循环。"""
        gen = self.ask_stream(
            user_query=user_query,
            chat_history=chat_history,
            model_name=model_name,
        )
        sentinel = object()
        while True:
            item = await asyncio.to_thread(next, gen, sentinel)
            if item is sentinel:
                break
            yield item


# 全局单例服务
_global_agent_service: Optional[AgenticRAGService] = None


def get_agent_service() -> AgenticRAGService:
    """获取 Agentic RAG 服务的全局单例"""
    global _global_agent_service
    if _global_agent_service is None:
        _global_agent_service = AgenticRAGService()
    return _global_agent_service
