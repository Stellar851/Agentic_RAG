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
import json
import functools
import re
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

from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
try:
    from langchain.agents import create_tool_calling_agent, AgentExecutor
except ImportError:
    from langchain_classic.agents import create_tool_calling_agent, AgentExecutor
from langchain_core.messages import AIMessage, HumanMessage

# 导入内部模块
from models import get_deepseek_llm
# 已解耦：纯 HTTP 远程接入数据中台 (Port 8001)

# 数据中台微服务通信配置 (Port 8001)
DATA_PLATFORM_URL = os.getenv('DATA_PLATFORM_URL', 'http://127.0.0.1:8001')


def _get_data_platform_doc_names() -> set:
    """通过 HTTP 从数据中台获取最新合法文档名称集合"""
    import requests
    try:
        resp = requests.get(f"{DATA_PLATFORM_URL}/api/v1/data/documents", timeout=5)
        if resp.status_code == 200:
            docs = resp.json().get("data", {}).get("documents", [])
            return {d.get("doc_name") for d in docs if d.get("doc_name")}
    except Exception:
        pass
    return set()

def _call_data_platform_retrieve(query: str, doc_name_filter: Optional[str] = None) -> Optional[str]:
    import requests
    try:
        resp = requests.post(
            f"{DATA_PLATFORM_URL}/api/v1/data/retrieve",
            json={
                "query": query,
                "doc_name_filter": doc_name_filter,
                "top_k": 6 if doc_name_filter else 10,
                "top_n": 4 if doc_name_filter else 3
            },
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            metrics = data.get("metrics", {})
            cand_cnt = metrics.get("candidate_count", 0)
            fin_cnt = metrics.get("final_count", 0)
            top_sc = metrics.get("top1_score", 0.0)
            print(f"  ⚡ [数据中台赋能] 成功通过 HTTP 接入数据中台 (Port 8001) 初筛:{cand_cnt} | 精排:{fin_cnt} | 最高分:{top_sc}")
            return data.get("formatted_text")
    except Exception:
        pass
    return None


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


def _split_and_dedupe_contexts(contexts: Optional[List[str]]) -> List[str]:
    """将工具观察结果拆成独立证据块并去重，避免同一长文本被重复计分。"""
    collected: List[str] = []
    seen = set()
    for raw in contexts or []:
        text = str(raw or "").strip()
        if not text:
            continue

        if "【来源 " in text:
            parts = re.split(r"\n{2,}(?=【来源\s*\d+】)", text)
        elif "--- [切片" in text:
            parts = re.split(r"\n{0,2}(?=--- \[切片)", text)
        else:
            parts = [text]

        for part in parts:
            part = part.strip()
            if not part:
                continue
            if any(marker in part for marker in ("未能检索到", "未检索到任何", "未能初筛到")):
                continue
            fingerprint = re.sub(r"\s+", " ", part)
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


# ==============================================================================
# 一、 智能体工具库 (Agent Tools)
# ==============================================================================

@tool
def list_available_documents() -> str:
    """
    【工具功能】获取知识库中当前已上传的所有文档列表及简要统计信息。
    """
    import requests
    print("\n📚 [Agent 触发工具] list_available_documents: 正在通过 HTTP 从数据中台 (Port 8001) 读取知识库文档资产...")
    try:
        resp = requests.get(f"{DATA_PLATFORM_URL}/api/v1/data/documents", timeout=5)
        if resp.status_code == 200:
            docs = resp.json().get("data", {}).get("documents", [])
            if not docs:
                return "当前知识库为空，尚未录入任何业务文档。"
            summary_list = []
            for d in docs:
                summary_list.append({
                    "文档名称": d.get("doc_name"),
                    "切片总数": d.get("chunk_count") or d.get("total_chunks", 0),
                    "文件大小": d.get("file_size_formatted", "--")
                })
            return json.dumps(summary_list, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"数据中台连接异常: {e}"
    return "获取文档清单失败"


@tool
def search_knowledge_base(query: str, doc_name_filter: Optional[str] = None) -> str:
    """
    【工具功能】在知识库中进行语义相似度向量检索，由数据中台统一执行两阶段高信噪比漏斗过滤。
    """
    remote_data = _call_data_platform_retrieve(query=query, doc_name_filter=doc_name_filter)
    if remote_data is not None:
        return remote_data
    return f"未能从数据中台检索到与 '{query}' 相关的片段，或数据中台当前不可用。"


@tool
def read_specific_document(doc_name: str, max_chunks: int = 6) -> str:
    """
    【工具功能】连续精读指定文档的上下文片段。
    """
    import requests
    import urllib.parse
    print(f"\n📖 [Agent 触发工具] read_specific_document: 正在通过 HTTP 从数据中台精读文档《{doc_name}》...")
    try:
        resp = requests.get(
            f"{DATA_PLATFORM_URL}/api/v1/data/document-chunks",
            params={"doc_name": doc_name, "max_chunks": max_chunks},
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            return data.get("formatted_text", f"未读取到文档《{doc_name}》的内容。")
    except Exception as e:
        return f"精读文档《{doc_name}》异常: {e}"
    return f"未能从数据中台获取文档《{doc_name}》的切片内容。"


# 注册 Agent 可用的全部工具列表
AGENT_TOOLS = [
    list_available_documents,
    search_knowledge_base,
    read_specific_document
]


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
   - 所有的事实、数据、结论必须严格基于检索到的知识库内容。
   - 如果检索了多次仍然找不到相关信息，请诚实说明哪部分信息缺失，不要编造答案。
4. **多轮对话与指代消解 (Coreference Resolution)**：
   - 在多轮连续对话中，用户提问往往包含代词、比较或省略主语（例如“它有哪些模块？”、“与方案A相比呢？”、“这个怎么配置？”）。
   - 你必须结合之前的对话历史，精准识别用户所指代的具体实体、方案或文档。
   - 在调用 `search_knowledge_base` 等工具时，**【绝对禁止】直接拿模糊代词（如“它”、“这个”）作为查询词检索**！你必须自主将检索词改写为包含具体实体全称的明确独立查询词（例如：上一轮在聊“网关系统”，用户追问“它的端口是多少”时，你应使用 `search_knowledge_base(query='网关系统 端口号 配置')` 发起检索）。
5. **检索预算与证据收敛**：
   - 用户点名了具体文档时，优先调用 `search_knowledge_base(..., doc_name_filter="完整文档名")`，不要先做全库泛化检索。
   - 同一问题不要重复执行相同检索；通常 2~4 次定向检索足够，原则上不要超过 4 次工具调用。
   - 一旦已获得回答所必需的核心证据，立即停止检索并作答，切勿为了“更完整”无限追加工具调用。
   - 如果证据不足，明确列出缺失点并基于已知内容回答，不得暴露 `Agent stopped due to max iterations` 等内部状态。
6. **规范的回答结构**：
   - **核心结论**：开门见山给出回答的主旨。
   - **关联分析/详细阐述**：分条理展开，清晰点出各个文档之间的逻辑链条（如：“依据《文档A》，...；同时《文档B》指出...”）。
   - **参考来源**：在末尾明确列出参考了哪些文档及相应章节/片段。
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
        # 缓存不同模型名称对应的 Agent Runnable 实例 (不可变线程安全)
        self._agents = {}
        # 迭代预算耗尽后的最终答案合成器按模型复用
        self._recovery_llms = {}

        # 构造对话 Prompt 模板
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", AGENT_SYSTEM_PROMPT),
            MessagesPlaceholder(variable_name="chat_history"),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

    def _get_executor(self, model_name: Optional[str] = None, verbose: bool = False) -> Tuple[AgentExecutor, str]:
        """
        根据指定模型名称获取线程安全的 AgentExecutor 实例
        底层复用不可变 Agent Runnable，顶层创建独立轻量级 AgentExecutor，确保多线程并发安全。
        """
        from models import ACTIVE_LLM_MODEL
        actual_model = model_name.strip() if model_name and model_name.strip() else ACTIVE_LLM_MODEL

        if actual_model not in self._agents:
            llm = get_deepseek_llm(
                model=actual_model,
                temperature=0.2,
                streaming=False,
                timeout=AGENT_LLM_TIMEOUT_SECONDS,
                max_retries=AGENT_LLM_MAX_RETRIES
            )
            agent = create_tool_calling_agent(
                llm=llm,
                tools=self.tools,
                prompt=self.prompt
            )
            self._agents[actual_model] = agent

        agent = self._agents[actual_model]
        executor = AgentExecutor(
            agent=agent,
            tools=self.tools,
            verbose=verbose,
            return_intermediate_steps=True,
            max_iterations=AGENT_MAX_ITERATIONS,
            early_stopping_method="generate"
        )
        return executor, actual_model

    def _recover_answer_from_contexts(
        self,
        user_query: str,
        contexts: List[str],
        actual_model: str
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
                max_retries=AGENT_LLM_MAX_RETRIES
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

    def ask(self, user_query: str, chat_history: Optional[List[Dict[str, str]]] = None, model_name: Optional[str] = None, verbose: bool = False) -> Dict[str, Any]:
        """
        【功能】接收用户提问，执行多跳 Agentic 检索与推理，返回结构化答案与思考证据链

        【参数】
            user_query: 用户提问
            chat_history: 可选的历史会话列表，格式形如 [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
            model_name: 可选，指定本次推理调用的模型名称

        【返回值】
            dict: {
                "answer": str (最终给出的综合分析回答),
                "steps": List[dict] (Agent 思考推理的详细步骤，每个步骤包含调用的工具、参数和工具返回摘要),
                "referenced_docs": List[str] (本次回答中引用的所有文档列表),
                "model_name": str (实际使用的模型)
            }
        """
        # 1. 转换历史会话格式为 LangChain 消息对象
        formatted_history = []
        if chat_history:
            for msg in chat_history[-10:]:  # 保留最近 5 轮深度上下文
                role = msg.get("role")
                content = msg.get("content", "")
                if role == "user":
                    formatted_history.append(HumanMessage(content=content))
                elif role == "assistant":
                    formatted_history.append(AIMessage(content=content))

        # 2. 调用 Agent 执行推理
        try:
            executor, actual_model = self._get_executor(model_name, verbose=verbose)
            result = executor.invoke({
                "input": user_query,
                "chat_history": formatted_history
            })

            answer_text = result.get("output", "")
            intermediate_steps = result.get("intermediate_steps", [])

            # 3. 结构化提取 Agent 的思考轨迹与引用的文档集合
            steps_trace = []
            referenced_docs = set()

            for step in intermediate_steps:
                action, observation = step
                tool_name = action.tool
                tool_input = action.tool_input
                log_text = action.log

                # 记录用到的文档
                if isinstance(tool_input, dict) and "doc_name" in tool_input:
                    referenced_docs.add(tool_input["doc_name"])
                if isinstance(tool_input, dict) and "doc_name_filter" in tool_input and tool_input["doc_name_filter"]:
                    referenced_docs.add(tool_input["doc_name_filter"])

                # 仅精确提取检索来源中的文档名（格式为：【来源 x】文档: 《xxx》）
                obs_str = str(observation)
                import re
                matches = re.findall(r"【来源\s*\d+】文档:\s*《(.*?)》", obs_str)
                for m in matches:
                    referenced_docs.add(m)

                steps_trace.append({
                    "tool": tool_name,
                    "input": tool_input,
                    "thought": log_text.strip() if log_text else f"调用工具: {tool_name}",
                    "observation_preview": (obs_str[:300] + "...") if len(obs_str) > 300 else obs_str
                })

            # 双重校验：确保最终引用的文档必须是知识库中真实存在的文件，坚决过滤文档正文中的非文件名泛读书名号
            valid_existing_docs = _get_data_platform_doc_names()
            filtered_docs = {d for d in referenced_docs if d in valid_existing_docs}

            # 拆分并去重证据块，避免同一条长检索结果被重复送入裁判。
            contexts_collected = collect_agent_contexts(intermediate_steps)

            degraded = False
            recovered_from = None
            if is_agent_stop_output(answer_text):
                recovered = self._recover_answer_from_contexts(
                    user_query=user_query,
                    contexts=contexts_collected,
                    actual_model=actual_model
                )
                answer_text = recovered or build_agent_failure_answer(contexts_collected)
                degraded = True
                recovered_from = "agent_max_iterations"

            return {
                "answer": answer_text,
                "steps": steps_trace,
                "referenced_docs": sorted(list(filtered_docs)),
                "contexts": contexts_collected,
                "model_name": actual_model,
                "success": True,
                "degraded": degraded,
                "recovered_from": recovered_from,
                "error_stage": None,
                "error_type": None,
                "retry_recommended": False
            }

        except Exception as e:
            err_str = str(e)
            if "quota" in err_str.lower() or "403" in err_str:
                short_err = "阿里百炼 API 免费额度已耗尽 (Free quota exhausted，请检查账号余额或 API Key)"
            elif "401" in err_str or "invalid_api_key" in err_str.lower():
                short_err = "阿里百炼 API Key 无效或未正确配置"
            elif "11434" in err_str or "ollama" in err_str.lower():
                short_err = "Ollama 本地服务未运行或连接超时 (http://localhost:11434)"
            else:
                short_err = f"{err_str[:120]}"
            print(f"  ❌ 作答异常: {short_err}")
            if "quota" in err_str.lower() or "403" in err_str:
                error_type = "agent_quota"
            elif "401" in err_str or "invalid_api_key" in err_str.lower():
                error_type = "agent_auth"
            elif "11434" in err_str or "ollama" in err_str.lower():
                error_type = "agent_embedding_unavailable"
            else:
                error_type = "agent_exception"
            return {
                "answer": f"抱歉，系统作答遇到异常：{short_err}",
                "steps": [],
                "referenced_docs": [],
                "contexts": [],
                "model_name": model_name or "unknown",
                "success": False,
                "error_stage": "agent",
                "error_type": error_type,
                "retry_recommended": True
            }

    def ask_stream(self, user_query: str, chat_history: Optional[List[Dict[str, str]]] = None, model_name: Optional[str] = None):
        """
        【功能】流式问答生成器：实时产生思维链状态（step_start / step_end）和逐字打字机 token
        通过 Queue 传递事件，支持 Web 前端 Server-Sent Events (SSE) 逐字平滑渲染
        """
        import queue
        import threading
        import re
        from langchain_core.callbacks.base import BaseCallbackHandler

        event_queue = queue.Queue()

        class StreamCallbackHandler(BaseCallbackHandler):
            def __init__(self, q):
                self.q = q
                self.in_tool = False

            def on_llm_new_token(self, token: str, **kwargs) -> None:
                # 只有非工具调用阶段生成的 token，才属于给用户的最终回答正文
                if not self.in_tool and token:
                    self.q.put({"event": "token", "data": {"delta": token}})

            def on_tool_start(self, serialized, input_str, **kwargs) -> None:
                self.in_tool = True
                name = serialized.get("name", "tool") if isinstance(serialized, dict) else str(serialized)
                self.q.put({
                    "event": "step_start",
                    "data": {
                        "tool": name,
                        "input": str(input_str)[:200]
                    }
                })

            def on_tool_end(self, output, **kwargs) -> None:
                self.in_tool = False
                obs_str = str(output)
                self.q.put({
                    "event": "step_end",
                    "data": {
                        "observation_preview": (obs_str[:300] + "...") if len(obs_str) > 300 else obs_str
                    }
                })

        formatted_history = []
        if chat_history:
            for msg in chat_history[-10:]:
                role = msg.get("role")
                content = msg.get("content", "")
                if role == "user":
                    formatted_history.append(HumanMessage(content=content))
                elif role == "assistant":
                    formatted_history.append(AIMessage(content=content))

        handler = StreamCallbackHandler(event_queue)

        def worker():
            try:
                executor, actual_model = self._get_executor(model_name)
                result = executor.invoke(
                    {"input": user_query, "chat_history": formatted_history},
                    config={"callbacks": [handler]}
                )
                answer_text = result.get("output", "")
                intermediate_steps = result.get("intermediate_steps", [])

                steps_trace = []
                referenced_docs = set()
                for step in intermediate_steps:
                    action, observation = step
                    tool_name = action.tool
                    tool_input = action.tool_input
                    log_text = action.log

                    if isinstance(tool_input, dict) and "doc_name" in tool_input:
                        referenced_docs.add(tool_input["doc_name"])
                    if isinstance(tool_input, dict) and "doc_name_filter" in tool_input and tool_input["doc_name_filter"]:
                        referenced_docs.add(tool_input["doc_name_filter"])

                    # 仅精确提取检索来源中的文档名（格式为：【来源 x】文档: 《xxx》）
                    obs_str = str(observation)
                    import re
                    matches = re.findall(r"【来源\s*\d+】文档:\s*《(.*?)》", obs_str)
                    for m in matches:
                        referenced_docs.add(m)

                    steps_trace.append({
                        "tool": tool_name,
                        "input": tool_input,
                        "thought": log_text.strip() if log_text else f"调用工具: {tool_name}",
                        "observation_preview": (obs_str[:300] + "...") if len(obs_str) > 300 else obs_str
                    })

                # 双重校验：确保最终引用的文档必须是知识库中真实存在的文件，坚决过滤文档正文中的非文件名泛读书名号
                vs = get_vector_store()
                valid_existing_docs = {d["doc_name"] for d in vs.list_all_documents()}
                filtered_docs = {d for d in referenced_docs if d in valid_existing_docs}
                contexts_collected = collect_agent_contexts(intermediate_steps)

                degraded = False
                recovered_from = None
                if is_agent_stop_output(answer_text):
                    recovered = self._recover_answer_from_contexts(
                        user_query=user_query,
                        contexts=contexts_collected,
                        actual_model=actual_model
                    )
                    answer_text = recovered or build_agent_failure_answer(contexts_collected)
                    degraded = True
                    recovered_from = "agent_max_iterations"

                event_queue.put({
                    "event": "done",
                    "data": {
                        "answer": answer_text,
                        "steps": steps_trace,
                        "referenced_docs": sorted(list(filtered_docs)),
                        "contexts": contexts_collected,
                        "model_name": actual_model,
                        "success": True,
                        "degraded": degraded,
                        "recovered_from": recovered_from
                    }
                })
            except Exception as e:
                import traceback
                traceback.print_exc()
                event_queue.put({
                    "event": "error",
                    "data": {
                        "message": f"Agent 执行异常: {str(e)}",
                        "error_stage": "agent",
                        "error_type": "agent_exception",
                        "retry_recommended": True
                    }
                })
            finally:
                event_queue.put(None)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        while True:
            item = event_queue.get()
            if item is None:
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
