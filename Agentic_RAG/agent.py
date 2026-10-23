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
from typing import List, Dict, Any, Tuple, Optional

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
from vector_store import get_vector_store


# ==============================================================================
# 一、 智能体工具库 (Agent Tools)
# ==============================================================================

@tool
def list_available_documents() -> str:
    """
    【工具功能】获取知识库中当前已上传的所有文档列表及简要统计信息。
    
    【何时使用】
    当用户的问题比较宏观、涉及多个文档的对比、或者你想了解知识库涵盖哪些具体文档时，
    优先调用此工具，获取全部候选文档名称，以便制定精准的检索计划。
    
    【返回值】
    JSON 格式的文档名称列表与切片数说明。
    """
    print("\n📚 [Agent 触发工具] list_available_documents: 正在读取知识库文档目录...")
    vs = get_vector_store()
    docs = vs.list_all_documents()
    if not docs:
        return "当前知识库为空，尚未上传任何文档。"

    summary_list = []
    for d in docs:
        summary_list.append({
            "文档名称": d["doc_name"],
            "切片总数": d["total_chunks"],
            "文件大小": d["file_size_formatted"]
        })
    return json.dumps(summary_list, ensure_ascii=False, indent=2)


@tool
def search_knowledge_base(query: str, doc_name_filter: Optional[str] = None) -> str:
    """
    【工具功能】在知识库中进行语义相似度向量检索，查找最相关的段落切片。
    
    【参数说明】
    - query: 检索查询词或自然语言问题。
    - doc_name_filter (可选): 如果你知道目标在某篇特定文档中，可传入文档名称（如 "用户手册.docx"），
      进行精确过滤检索；如果不传或为空，则在所有文档中跨文档全局检索。
      
    【何时使用】
    寻找具体概念、段落、参数、关联依据或上下文事实时调用。支持多次针对不同关键词发起检索。
    """
    filter_hint = f" | 限定文档='{doc_name_filter}'" if doc_name_filter else " | 全库检索"
    print(f"\n🔎 [Agent 触发工具] search_knowledge_base: 检索词='{query}'{filter_hint}")
    vs = get_vector_store()
    top_k = 4 if doc_name_filter else 5
    results = vs.query_similar_chunks(query=query, top_k=top_k, doc_name_filter=doc_name_filter)

    if not results:
        target_desc = f"在文档【{doc_name_filter}】中" if doc_name_filter else "在全库中"
        return f"{target_desc}未能检索到与 '{query}' 相关的片段，请尝试更换检索关键词或扩大检索范围。"

    formatted_snippets = []
    for i, r in enumerate(results, 1):
        formatted_snippets.append(
            f"【来源 {i}】文档: 《{r['doc_name']}》 (切片 {r['chunk_index'] + 1}/{r['total_chunks']})\n"
            f"相关度距离: {r['distance']}\n"
            f"内容片段:\n{r['text']}\n"
            f"----------------------------------------"
        )
    return "\n\n".join(formatted_snippets)


@tool
def read_specific_document(doc_name: str, max_chunks: int = 6) -> str:
    """
    【工具功能】连续精读指定文档的上下文片段。
    
    【参数说明】
    - doc_name: 文档的完整名称 (例如 '系统架构设计说明书.docx')。
    - max_chunks: 顺序读取的切片数，默认 6 个切片。
    
    【何时使用】
    当向量检索到的片段太碎片化，或者文档结构严密（如章节条款连续叙述）、
    需要通读某篇核心文档的整体脉络时调用。
    """
    print(f"\n📖 [Agent 触发工具] read_specific_document: 正在深度连续精读文档《{doc_name}》(前 {max_chunks} 切片)...")
    vs = get_vector_store()
    return vs.get_document_full_content(doc_name=doc_name, max_chunks=max_chunks)


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
5. **规范的回答结构**：
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
        # 缓存不同模型名称对应的 AgentExecutor 实例
        self._executors = {}

        # 构造对话 Prompt 模板
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", AGENT_SYSTEM_PROMPT),
            MessagesPlaceholder(variable_name="chat_history"),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

    def _get_executor(self, model_name: Optional[str] = None) -> Tuple[AgentExecutor, str]:
        """
        根据指定模型名称获取或创建 AgentExecutor
        """
        from models import ACTIVE_LLM_MODEL
        actual_model = model_name.strip() if model_name and model_name.strip() else ACTIVE_LLM_MODEL
        
        if actual_model not in self._executors:
            print(f"\n🔄 [Agent 初始化执行器] 正在为模型【{actual_model}】构建专属 Tool-Calling AgentExecutor...")
            llm = get_deepseek_llm(model=actual_model, temperature=0.2, streaming=True)
            agent = create_tool_calling_agent(
                llm=llm,
                tools=self.tools,
                prompt=self.prompt
            )
            executor = AgentExecutor(
                agent=agent,
                tools=self.tools,
                verbose=True,
                return_intermediate_steps=True,
                max_iterations=8,  # 最多允许 8 次工具调用循环，足以完成深度多跳关联
                early_stopping_method="generate"
            )
            self._executors[actual_model] = executor
            print(f"✅ [Agent 执行器就绪] 模型【{actual_model}】执行器已缓存构建完成。")

        return self._executors[actual_model], actual_model

    def ask(self, user_query: str, chat_history: Optional[List[Dict[str, str]]] = None, model_name: Optional[str] = None) -> Dict[str, Any]:
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
            executor, actual_model = self._get_executor(model_name)
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
            vs = get_vector_store()
            valid_existing_docs = {d["doc_name"] for d in vs.list_all_documents()}
            filtered_docs = {d for d in referenced_docs if d in valid_existing_docs}

            return {
                "answer": answer_text,
                "steps": steps_trace,
                "referenced_docs": sorted(list(filtered_docs)),
                "model_name": actual_model,
                "success": True
            }

        except Exception as e:
            import traceback
            error_msg = f"Agent 推理执行异常: {str(e)}"
            print(error_msg)
            traceback.print_exc()
            return {
                "answer": f"抱歉，在调用智能体检索知识库时发生错误：{str(e)}。请确保大模型 API 密钥配置正确，且 Ollama 本地服务处于运行中。",
                "steps": [],
                "referenced_docs": [],
                "model_name": model_name or "unknown",
                "success": False
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

                event_queue.put({
                    "event": "done",
                    "data": {
                        "answer": answer_text,
                        "steps": steps_trace,
                        "referenced_docs": sorted(list(filtered_docs)),
                        "model_name": actual_model,
                        "success": True
                    }
                })
            except Exception as e:
                import traceback
                traceback.print_exc()
                event_queue.put({
                    "event": "error",
                    "data": {"message": f"Agent 执行异常: {str(e)}"}
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
