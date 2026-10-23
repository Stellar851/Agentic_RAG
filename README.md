# Agentic RAG 企业级智能多文档关联知识库中枢

基于 **FastAPI + LangChain Tool-Calling + ChromaDB + Ollama + uv** 构建的高性能企业级多跳推理与知识分析系统。

---

## 🚀 快速上手 (使用 uv)

现代 `uv` 提供了基于锁文件（`uv.lock`）的**零心智负担一键环境还原**。在新电脑或服务器上无需手动安装 Python 或激活虚拟环境。

### 1. 一键同步环境（新机器 / 克隆代码后）
```bash
# 自动按 pyproject.toml 检查 Python 版本（缺失则自动下载）并秒级还原全部依赖
uv sync
```

### 2. 启动 Web 服务
```bash
# 在隔离环境中直接启动 FastAPI Web 服务（免手动 source/activate）
uv run python Agentic_RAG/app.py
```

* **Web UI 界面**: `http://127.0.0.1:5000`
* **Swagger 交互文档**: `http://127.0.0.1:5000/docs`
* **Redoc 规范说明**: `http://127.0.0.1:5000/redoc`

---

## 🛠️ 现代工程标准实战流程（从 0 到 1）

如果您需要从零创建新的应用项目，或者将旧的 `requirements.txt` 项目迁移至 `uv`：

### 步骤 1：初始化现代应用骨架（无多余文件）
```bash
uv init --bare --no-package
```
> **说明**：`--bare --no-package` 会生成纯净的 `pyproject.toml`，绝不会生成多余的 `src/` 或无用示例代码。

### 步骤 2：批量导入并锁定依赖
```bash
uv add -r Agentic_RAG/requirements.txt
```
> **说明**：自动创建 `.venv` 虚拟环境、极速下载安装所有三方包，并生成确定性版本锁文件 `uv.lock`。

### 步骤 3：直接运行
```bash
uv run python Agentic_RAG/app.py
```

---

## 📋 日常开发高频指令速查

| 场景 | 命令 | 说明 |
| :--- | :--- | :--- |
| **启动运行** | `uv run python Agentic_RAG/app.py` | 自动在当前项目受控环境中运行 |
| **新增依赖** | `uv add <package_name>` | 自动安装并同步写入 `pyproject.toml` 与 `uv.lock` |
| **移除依赖** | `uv remove <package_name>` | 自动卸载并从配置文件与锁文件中清理 |
| **环境对齐** | `uv sync` | 严格将本地 `.venv` 与 `uv.lock` 保持 100% 同步（多退少补） |
| **查看依赖树** | `uv tree` | 终端直观打印各库之间的层级依赖关系 |
| **扫描 Python** | `uv python list --only-installed` | 查看当前系统已安装的 Python 解释器 |
| **安装指定 Python** | `uv python install 3.12` | 一键下载官方独立的纯净 Python 版本 |

---

## 🔒 核心文件架构说明

* **`pyproject.toml`**：项目的**需求清单**（声明依赖库范围与 Python 版本限制）。
* **`uv.lock`**：项目的**精准版本快照**（锁定所有直接与间接依赖的哈希与精确版本，杜绝环境漂移）。
* **`.venv/`**：本地虚拟环境（无需提交至 Git，通过 `uv sync` 随时毫秒级还原）。
