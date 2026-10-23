# Agentic RAG 项目交接与 AI 极速对接文档 (AI Handoff Document)

> 📌 **本文档目的**：当用户切换新的 AI 模型或开启新会话时，新模型**只需阅读本单一文件**即可 100% 掌握本项目全貌、架构细节、接口定义与运维状态，无需检索海量代码，极大减少 Token 消耗。  
> ⚠️ **维护契约**：**后续对本项目的任何代码结构或逻辑改动，必须实时同步更新本文档！**

---

## 一、 项目核心概况与设计定位

- **项目名称**：`Agentic_RAG`
- **项目绝对路径**：`e:/RAG_project/Agentic_RAG/`
- **独立性声明**：**与同级目录下的 `L2Project03` 完全解耦、互不关联**。严禁向 `L2Project03` 引入依赖。
- **架构方案**：**方案一（Agentic RAG / 智能体多跳工具调用循环）**。专门解决“多篇文档相互依赖、概念穿透引用、跨文档多步推理求证（Multi-Hop）”。
- **运行状态**：服务入口为 `app.py`（基于 Flask），默认运行在 `http://127.0.0.1:5000`。

---

## 二、 当前模型与基础设施配置

| 组件 | 配置项 | 细节说明 |
| :--- | :--- | :--- |
| **LLM 思考大脑** | **DeepSeek 官方 API** | - **模型名 (model)**: `deepseek-flash` (支持 Tool Calling)<br>- **接口地址 (base_url)**: `https://api.deepseek.com`<br>- **密钥获取机制**: 在 `models.py` 中多级读取，优先读取 Windows 系统环境变量 `Deepseek_key`（具备 Windows 注册表 `HKLM` 与 `HKCU` 自动穿透兜底机制，**代码中绝无硬编码 Key**）。 |
| **Embedding 向量模型** | **本地 Ollama `bge-m3`** | - **服务地址**: `http://localhost:11434`<br>- **向量维度**: 1024 维<br>- **调用方式**: 基于 `langchain_community.embeddings.OllamaEmbeddings`。 |
| **向量数据库** | **ChromaDB** | - **存储模式**: 持久化客户端 `PersistentClient`<br>- **持久化路径**: `Agentic_RAG/chroma_db/`<br>- **集合名称**: `agentic_knowledge_base`<br>- **切片配置**: `chunk_size = 600`, `chunk_overlap = 100`。 |
| **对话历史数据库** | **SQLite (内置)** | - **数据库文件**: `Agentic_RAG/chat_history.db`<br>- **存储表**: `chat_sessions` (会话元数据) 与 `chat_messages` (问答、Agent 思维步骤与引用源)。 |
| **Web 界面主题** | **高雅白底 / 浅色模式** | 清爽明亮风格，包含侧边栏会话历史切换、新建对话、文件拖拽、思维链折叠展示。 |

---

## 三、 完整目录结构与职责分工

```
e:/RAG_project/Agentic_RAG/
│
├── models.py              # [模型中心] 管理 DeepSeek LLM 与 Ollama bge-m3 向量模型实例，负责密钥提取与连通自检
├── vector_store.py        # [向量引擎] 多格式文档解析 (.docx/.pdf/.txt/.md)、分块、ChromaDB 持久化与向量检索
├── agent.py               # [智能体中枢] LangChain Tool-Calling 架构，管理检索工具集、ReAct 循环、多轮指代消解
├── chat_history_db.py     # [会话记忆] 基于 SQLite 封装多会话持久化、消息流水记录、增删查改
├── app.py                 # [Web 服务] Flask RESTful API 入口，挂载文档生命周期、会话管理与智能对话接口
├── PROJECT_CONTEXT.md     # [AI 对接文档] 本文件，模型交接核心上下文备忘录 (保持实时更新)
├── requirements.txt       # [依赖文件] 纯 ASCII 格式，避免 Windows GBK 解码错误
├── README.md              # [用户文档] 包含系统设计理念与全景使用说明
│
├── chat_history.db        # [数据文件] SQLite 会话与历史消息持久化数据库 (自动维护)
├── uploads/               # [数据文件] 用户上传的原始物理文件保存目录 (自动维护)
├── chroma_db/             # [数据文件] ChromaDB 向量索引持久化目录 (自动维护)
└── templates/
    └── index.html         # [前端页面] 浅色明亮风格单页，集成会话列表、拖拽上传、思维链可视化
```

---

## 四、 核心代码接口与交互规范

### 1. `models.py`
- `get_deepseek_api_key() -> str`: 从 `os.getenv` 或注册表 `HKLM/HKCU` 动态读取 `Deepseek_key`。
- `get_deepseek_llm(model="deepseek-flash", temperature=0.2)`: 获取用于 Agent 的 LangChain `ChatOpenAI` 客户端。
- `get_ollama_embeddings(model="bge-m3", base_url="http://localhost:11434")`: 获取本地嵌入模型。
- `test_connections() -> dict`: 自检 DeepSeek 与 Ollama 状态。

### 2. `vector_store.py`
- `DocumentParser`: 支持 `.pptx`, `.docx`, `.pdf`, `.txt`, `.md`, `.json`, `.csv`；集成**双清洗模式**：
  - **`clean_mode="llm"`（大模型智能升维清洗）**：检测到 ASCII 框图时调用配置大模型 (`deepseek-flash`) 升维重构为 Markdown 说明与标准 Mermaid 流程图，无损保全拓扑关联；
  - **`clean_mode="rule"`（代码规则普通清洗）**：纯本地正则规则降噪，极速剥离多余制表符号与纯线条边框，零 Token 消耗。
- **智能框图识别阈值机制**：区分目录树与封闭框图（必须满足拐角数 `>= 4` 或流程箭头 `>= 1` 且总符号 `>= 30`），避免常规目录树误触发大模型。
- **SHA-256 内容哈希防重机制 (Content Hash Deduplication)**:
  - 提取正文后计算 `hashlib.sha256(raw_text.strip().encode("utf-8")).hexdigest()` 内容指纹；
  - ChromaDB 元数据检索 `where={"content_hash": content_hash}`；
  - **跨文件名同内容拦截**：若不同文件名内容完全一致，控制台告警并直接拦截跳过重复写入，避免知识库冗余膨胀；
  - **同名文件重传校验**：若同名文件内容未发生改变，自动复用已有索引，跳过昂贵的向量 Embedding 计算；
  - **内容变更平滑覆盖**：若同名文件内容发生了更新，自动先清空旧切片向量再写入最新切片。
- `VectorStoreManager.process_and_add_document(file_path, clean_mode="llm")`: 解析文本 (按选定清洗模式) -> SHA-256 查重 -> RecursiveCharacterTextSplitter 切块 (600字/100重叠) -> 计算 bge-m3 向量 -> 存入 ChromaDB，附带元数据 (`doc_name`, `chunk_index`, `total_chunks`, `content_hash` 等)。
- `VectorStoreManager.query_similar_chunks(query, top_k=4/5, doc_name_filter=None)`: 向量相似度检索。
- `VectorStoreManager.list_all_documents()`: 列出知识库所有文档及其切片数。
- `VectorStoreManager.delete_document(doc_name)`: 同步删除 ChromaDB 向量与 uploads 物理文件。

### 3. `agent.py`
- **三大专属工具 (Tools)**:
  1. `list_available_documents`: 宏观获知当前知识库所有文档清单；
  2. `search_knowledge_base(query, doc_name_filter)`: 执行向量检索（必须传入无代词的独立查询词）；
  3. `read_specific_document(doc_name, max_chunks)`: 顺序精读指定文档全文脉络。
- **参考文档精准溯源校验 (Referenced Docs Double Validation)**:
  - 仅抓取工具来源头（`【来源 x】文档: 《xxx》`），拒绝泛匹配正文书名号；
  - 强制与 ChromaDB 真实存储的文件名做交集过滤，坚决杜绝文档内部包含的参考书籍、示例名（如《系统架构书》《接口手册》等）被误呈现在前端参考文档栏中。
- **流式输出支持 (`ask_stream`)**:
  - `streaming=True`，基于线程安全队列与 `BaseCallbackHandler` 异步捕获思考步骤与逐字 token；
  - 实时发送 `step_start`, `step_end`, `token`, `done` 事件，打通前端打字机效果。
- **指代消解与多轮记忆**: 
  - 系统 Prompt 中明确约束：在连续追问中识别代词（如“它”、“这个”），**严禁直接用代词检索**，必须改写为包含实体全称的明确独立查询词；
  - 上下文记忆窗口保留最近 10 条消息（5 轮问答深度）。

### 4. `chat_history_db.py`
- `create_session(title) -> dict`: 新建会话，生成 UUID。
- `list_sessions() -> list`: 倒序获取所有会话列表。
- `get_session_messages(session_id) -> list`: 获取某会话下的全部历史消息（包含思考步骤与参考源）。
- `add_message(session_id, role, content, steps, referenced_docs)`: 存储单轮问答。
- `delete_session(session_id) -> bool`: 级联删除会话与消息。

### 5. `app.py` (API 路由一览与全链路终端日志)
- **终端全景可观测性日志 (Terminal Observability)**:
  - 📥 **上传文件**：打印文件名、文件大小 (KB)；
  - 🧩 **大模型数据清洗**：打印检测到的 ASCII/制表符特征数、DeepSeek-V3 升维清洗中状态、图块重构耗时与字符统计；
  - 💡 **防重处理**：打印 SHA-256 前缀与去重拦截/复用提示；
  - ✂️ **切片分块**：打印切片总数、单块尺寸与重叠窗口；
  - 🧠 **向量嵌入**：打印调用 Ollama bge-m3 批量计算进度；
  - 🔍 **向量检索**：打印检索范围、查询词、Top-K 与首条命中相关度距离；
  - 🤖 **智能对话**：打印会话 ID、运行模式 (SSE/同步)、用户问题及生成完成统计；
  - 🗑️ **删除操作**：打印文档清空与会话删除状态。
- `GET  /` : 页面渲染。
- `GET  /api/status` : 系统各组件健康检查。
- `POST /api/upload` : 多文档文件上传与自动预检。
  - 自动扫描文本特征：若未触发图表阈值，自动采用 `rule` 极速入库（零弹窗打扰）；若触发复杂架构图阈值，返回 `needs_confirmation` 供前端定向弹窗确认。
- `POST /api/upload/confirm` : 用户确认清洗模式（大模型清洗 vs 代码普通清洗）后触发入库。
- `GET  /api/documents` & `DELETE /api/documents/<doc_name>` : 文档列表与删除。
- `GET  /api/documents/<doc_name>/chunks` : 获取指定文档在 ChromaDB 中存储的全部清洗后切片文本、字数及元数据，供前端“切片详情验证器”透视校验。
- `GET  /api/sessions` & `POST /api/sessions` : 会话列表与新建。
- `GET  /api/sessions/<id>` & `DELETE /api/sessions/<id>` : 会话详情与删除。
- `POST /api/chat` : 
  - Body: `{"session_id": "...", "query": "...", "stream": true}`
  - 默认开启 SSE (`text/event-stream`)，实时推送工具调用状态与逐字回答；
  - 自动从 SQLite 提取该会话历史上下文，驱动 Agent 推理，流结束后将完整问答持久化存盘。

---

## 五、 踩坑记录与重要注意事项 (Gotchas)

1. **依赖编码冲突 (Windows GBK vs UTF-8)**:
   - `requirements.txt` 必须保持**纯 ASCII / 英文注释**，否则 Windows `pip install` 在 CP936 下会触发 `UnicodeDecodeError`。
2. **LangChain 1.4+ 导入兼容性**:
   - `agent.py` 必须使用兼容语法：
     ```python
     try:
         from langchain.agents import create_tool_calling_agent, AgentExecutor
     except ImportError:
         from langchain_classic.agents import create_tool_calling_agent, AgentExecutor
     from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
     ```
3. **短文档切片现象**:
   - 若上传文件小于 600 字符（如测试的 `RAG.md` 仅 400 字），系统正常且合理地保留为 **1 个切片**，确保段落语义不被碎片化。
4. **pip 安装超时加速**:
   - 若国内拉取依赖缓慢，建议使用清华大学镜像源：`-i https://pypi.tuna.tsinghua.edu.cn/simple --prefer-binary`。

---

## 六、 变更历史记录 (Changelog)

| 版本/时间 | 改动要点 | 涉及文件 |
| :--- | :--- | :--- |
| **v1.0 (2026-10-14)** | 初始化全新独立项目，实现方案一 Agentic RAG（DeepSeek + Ollama bge-m3 + ChromaDB） | 全量初始文件 |
| **v1.1 (2026-10-14)** | 根据用户要求将 DeepSeek 模型设置为 `deepseek-flash`，base_url 设为 `https://api.deepseek.com` | `models.py`, `README.md` |
| **v1.2 (2026-10-14)** | 修复 `requirements.txt` 编码问题，全面走通清华镜像依赖安装 | `requirements.txt` |
| **v1.3 (2026-10-14)** | 将前端 UI 从暗黑拟态重构为优雅明亮的**白色/浅色极简主题** | `templates/index.html` |
| **v1.4 (2026-10-14)** | 实装基于 SQLite 的**多会话持久化、历史消息存盘、新建/切换会话**以及 **Agent 指代消解追问** | `chat_history_db.py`, `agent.py`, `app.py`, `templates/index.html` |
| **v1.5 (2026-10-14)** | 创建本对接文档 `PROJECT_CONTEXT.md`，确立后续改动的持续更新机制 | `PROJECT_CONTEXT.md` |
| **v1.6 (2026-10-15)** | **方案 B 字符图智能升维重构** + **PPTX 解析** + **全链路 SSE 打字机流式输出** + **SHA-256 内容哈希防重** + **终端全景可观测性日志** | `vector_store.py`, `agent.py`, `app.py`, `templates/index.html`, `requirements.txt`, `AGENTIC_RAG_DEEP_DIVE.md`, `PROJECT_CONTEXT.md` |
| **v1.7 (2026-10-15)** | **数据清洗手动确认弹窗与双清洗模式**：上传时前端弹窗让用户自由选择【大模型智能清洗 (Mermaid/Markdown升维)】或【代码规则普通清洗 (零Token/极速)】，终端输出对应分支的专属提示 | `vector_store.py`, `app.py`, `templates/index.html`, `PROJECT_CONTEXT.md` |
