"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
向量数据库与文档处理模块 (vector_store.py)

【模块定位】
本文件负责知识库的存储引擎和数据预处理流水线：
1. 文档解析与读取：支持 .docx、.pdf、.txt、.md 等格式文件的纯文本提取。
2. 文本分块 (Chunking)：基于 LangChain 的 RecursiveCharacterTextSplitter，保留语义连贯性。
3. 结构化元数据增强：为每个切片注入文档名、切片编号、全局切片总数、入库时间戳等元数据，
   支持 Agent 在多文档间追溯关联与精确定位。
4. 向量库持久化 (ChromaDB)：结合 Ollama bge-m3 向量模型进行持久化存储与多维度相似度检索。
================================================================================
"""

import os
import sys
import shutil
import datetime
import threading
from typing import List, Dict, Any, Optional

# 确保 Windows 终端输出 UTF-8 编码，防止 emoji 或特殊字符在 GBK 环境下抛出 UnicodeEncodeError
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import chromadb
from chromadb.config import Settings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from docx import Document as DocxDocument
from pypdf import PdfReader
from pptx import Presentation

# 导入内部模型工厂
from models import get_ollama_embeddings, get_deepseek_llm, DEEPSEEK_DEFAULT_MODEL


# ==============================================================================
# 一、 目录与常量定义
# ==============================================================================

# 当前项目根目录
BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))

# 上传原始文件存储路径
UPLOAD_DIR: str = os.path.join(BASE_DIR, "uploads")

# ChromaDB 向量数据持久化路径
CHROMA_PERSIST_DIR: str = os.path.join(BASE_DIR, "chroma_db")

# ChromaDB 统一知识库 Collection 名称
COLLECTION_NAME: str = "agentic_knowledge_base"

# 自动创建必要文件夹
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CHROMA_PERSIST_DIR, exist_ok=True)


# ==============================================================================
# 二、 文档解析器类 (DocumentParser)
# ==============================================================================

class DocumentParser:
    """
    【类名】DocumentParser
    【功能】负责多格式文件的文本解析与清洗，支持 docx、pdf、txt、markdown 等
    """

    @staticmethod
    def extract_raw_text(file_path: str) -> str:
        """
        【功能】根据文件扩展名自动选择对应的解析逻辑提取全部原始文本（未经清洗）
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".docx":
            return DocumentParser._extract_docx(file_path)
        elif ext == ".pdf":
            return DocumentParser._extract_pdf(file_path)
        elif ext == ".pptx":
            return DocumentParser._extract_pptx(file_path)
        elif ext in [".txt", ".md", ".json", ".csv", ".py"]:
            return DocumentParser._extract_text_plain(file_path)
        else:
            raise ValueError(f"暂不支持的文件格式: {ext}，当前支持 .pptx, .docx, .pdf, .txt, .md, .json, .csv")

    @staticmethod
    def inspect_diagram(raw_text: str) -> Dict[str, Any]:
        """
        【功能】检测文本中是否包含复杂字符架构框图或流程图
        【阈值】闭合拐角数 >= 4 或流程箭头 >= 1，且特征符号总数 >= 30
        """
        box_corners = set("┌┐└┘╔╗╚╝")
        box_arrows = set("▲▼►◄")
        corner_count = sum(1 for ch in raw_text if ch in box_corners)
        arrow_count = sum(1 for ch in raw_text if ch in box_arrows)

        all_box_chars = set("┌─┐└┘├┤┬┴┼│║═╔╗╚╝╠╣╦╩╬▲▼►◄")
        total_box_chars = sum(1 for ch in raw_text if ch in all_box_chars)

        is_real_diagram = (corner_count >= 4 or arrow_count >= 1) and total_box_chars >= 30
        return {
            "is_real_diagram": is_real_diagram,
            "corner_count": corner_count,
            "arrow_count": arrow_count,
            "total_box_chars": total_box_chars
        }

    @staticmethod
    def extract_text(file_path: str, clean_mode: str = "rule") -> str:
        """
        【功能】提取文档原始文本并执行选定的清洗模式
        """
        raw_text = DocumentParser.extract_raw_text(file_path)
        return DocumentParser.reform_complex_diagrams(raw_text, clean_mode=clean_mode)

    @staticmethod
    def reform_complex_diagrams(raw_text: str, clean_mode: str = "rule") -> str:
        """
        【数据清洗模块：支持大模型升维清洗 与 代码规则普通清洗】
        - clean_mode="rule" (默认): 纯本地代码正则降噪，零 Token 消耗、极速入库；
        - clean_mode="llm": 方案 B，调用当前配置的大模型 (DEEPSEEK_DEFAULT_MODEL) 自动转写为结构化 Markdown 说明与 Mermaid 流程图。

        【智能阈值与图表识别机制】
        - 必须包含真正的封闭框图特征（拐角字符 ┌┐ 或 ╔╗）或流程指向箭头（▲▼►◄），且特征符号数 >= 30；
        - 常规的文件目录树（如 ├── 、└── 、│ ）无封闭框图拐角，判定为普通文本，绝不盲目触发大模型重构。
        """
        import re
        import time

        diag_info = DocumentParser.inspect_diagram(raw_text)
        is_real_diagram = diag_info["is_real_diagram"]
        corner_count = diag_info["corner_count"]
        arrow_count = diag_info["arrow_count"]
        total_box_chars = diag_info["total_box_chars"]
        all_box_chars = set("┌─┐└┘├┤┬┴┼│║═╔╗╚╝╠╣╦╩╬▲▼►◄")

        if not is_real_diagram:
            if total_box_chars > 0:
                print(f"ℹ️ [无需大模型清洗] 检测到文本为常规目录树或特征符号未达复杂架构图阈值 (拐角数: {corner_count}, 符号数: {total_box_chars})，保持原始文本，跳过大模型。")
            return raw_text

        # ----------------------------------------------------------------------
        # 分支 1: 用户手动确认选择【代码规则普通清洗】
        # ----------------------------------------------------------------------
        if clean_mode == "rule":
            print(f"\n⚡ [执行模式: 代码普通清洗] 检测到复杂框图特征 (拐角: {corner_count}, 符号: {total_box_chars})，当前为【代码普通清洗】模式")
            print("🧹 [代码规则清洗] 正在执行本地正则纯代码降噪（过滤制表符号与纯线条边框，零 Token 消耗）...")
            cleaned_lines = []
            for line in raw_text.splitlines():
                stripped = line.strip()
                # 剔除整行全为边框线或制表符的无效行
                if re.match(r"^[┌─┐└┘├┤┬┴┼│║═╔╗╚╝╠╣╦╩╬\-\|\+\s]+$", stripped):
                    continue
                # 去除两端包裹的制表线符号
                clean_line = re.sub(r"^[│║\|]\s*", "", line)
                clean_line = re.sub(r"\s*[│║\|]$", "", clean_line)
                if clean_line.strip():
                    cleaned_lines.append(clean_line)
            print("✨ [代码普通清洗完毕] 本地正则规则降噪完成，未消耗任何大模型 Token。\n")
            return "\n".join(cleaned_lines)

        # ----------------------------------------------------------------------
        # 分支 2: 用户确认选择【大模型智能清洗 (方案 B)】
        # ----------------------------------------------------------------------
        print(f"\n🧩 [执行模式: 大模型智能清洗] 检测到复杂 ASCII/制表符架构框图 (拐角: {corner_count}, 符号: {total_box_chars})")
        print(f"🤖 [大模型数据清洗] 正在启动配置大模型【{DEEPSEEK_DEFAULT_MODEL}】执行智能升维清洗（无损消除制表符边框噪音、转写 Markdown 层级与标准 Mermaid 拓扑图）...")

        code_block_pattern = re.compile(r"```[^\n]*\n([\s\S]*?[┌─┐└┘├┤┬┴┼│║═╔╗╚╝╠╣╦╩╬][\s\S]*?)```", re.MULTILINE)

        def _reconstruct_block(diagram_str: str) -> str:
            start_t = time.time()
            try:
                print(f"   ⏳ [清洗处理中] 正在对字符拓扑图块 ({len(diagram_str)} 字符) 进行理解与重构...")
                llm = get_deepseek_llm(temperature=0.0)
                reconstruct_prompt = (
                    "你是一个资深技术文档预处理与重构专家。请将以下 ASCII/字符框图转换为结构化的 Markdown 层级说明与标准 Mermaid 流程图。\n"
                    "严格要求：\n"
                    "1. 彻底去除无意义的制表符、纯线条边框（如 ┌ ─ ┐ │ └ ┘ 等）；\n"
                    "2. 严格保全图中的每一个系统名称、组件、参数、技术选型和调用关系，绝不遗漏或擅自篡改信息；\n"
                    "3. 消除多栏横向并排导致的文本交错，梳理为清晰的上下游或从属层级；\n"
                    "4. 输出格式：直接输出重构后的清晰 Markdown 结构化说明，并在文末附带一个 ```mermaid 流程图，不要添加任何额外寒暄。\n\n"
                    f"待处理字符图：\n{diagram_str}"
                )
                response = llm.invoke(reconstruct_prompt)
                reformed = response.content.strip() if hasattr(response, "content") else str(response).strip()
                cost = time.time() - start_t
                if reformed and len(reformed) > 20:
                    print(f"   ✅ [大模型清洗完成] 架构图块升维重构成功！(耗时: {cost:.2f}s, 重构后长度: {len(reformed)} 字符)")
                    return f"\n\n<!-- [已由智能解析器自动重构字符架构图] -->\n{reformed}\n\n"
            except Exception as e:
                print(f"   ⚠️ [大模型清洗异常] 字符架构图重构失败 ({e})，平滑回退至规则降噪模式。")

            # 降级备选方案：规则清洗，剔除纯边框符号
            cleaned_lines = []
            for line in diagram_str.splitlines():
                stripped = line.strip()
                if re.match(r"^[┌─┐└┘├┤┬┴┼│║═╔╗╚╝╠╣╦╩╬\-\|\+\s]+$", stripped):
                    continue
                clean_line = re.sub(r"^[│║\|]\s*", "", line)
                clean_line = re.sub(r"\s*[│║\|]$", "", clean_line)
                if clean_line.strip():
                    cleaned_lines.append(clean_line)
            return "\n".join(cleaned_lines)

        if code_block_pattern.search(raw_text):
            result_text = code_block_pattern.sub(lambda m: _reconstruct_block(m.group(0)), raw_text)
        else:
            lines = raw_text.splitlines()
            diagram_start = None
            diagram_end = None
            for idx, line in enumerate(lines):
                if any(c in all_box_chars for c in line):
                    if diagram_start is None:
                        diagram_start = idx
                    diagram_end = idx
                elif diagram_start is not None and (idx - diagram_end) > 3:
                    break

            if diagram_start is not None and diagram_end is not None and (diagram_end - diagram_start >= 3):
                diag_text = "\n".join(lines[diagram_start:diagram_end + 1])
                reformed_block = _reconstruct_block(diag_text)
                new_lines = lines[:diagram_start] + [reformed_block] + lines[diagram_end + 1:]
                result_text = "\n".join(new_lines)
            else:
                result_text = raw_text

        print("✨ [大模型数据清洗完毕] 文档文本规范化完成，彻底消除字符边框噪声并保留完整拓扑语义。\n")
        return result_text

    @staticmethod
    def _extract_pptx(file_path: str) -> str:
        """提取 PowerPoint (.pptx) 文件中的所有幻灯片文本、表格及备注"""
        prs = Presentation(file_path)
        slides_text = []

        for i, slide in enumerate(prs.slides):
            slide_content = []

            # 1. 遍历幻灯片中的所有形状
            for shape in slide.shapes:
                # 提取文本框与占位符
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        text = paragraph.text.strip()
                        if text:
                            slide_content.append(text)
                # 提取表格内容
                elif shape.has_table:
                    for row in shape.table.rows:
                        row_text = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                        if row_text:
                            slide_content.append(" | ".join(row_text))

            # 2. 提取演讲备注 (Notes)
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                notes_text = slide.notes_slide.notes_text_frame.text.strip()
                if notes_text:
                    slide_content.append(f"[幻灯片备注: {notes_text}]")

            if slide_content:
                slides_text.append(f"--- 幻灯片 第 {i + 1} 页 ---\n" + "\n".join(slide_content))

        return "\n\n".join(slides_text)

    @staticmethod
    def _extract_docx(file_path: str) -> str:
        """提取 Word (.docx) 文件中的所有段落文本"""
        doc = DocxDocument(file_path)
        paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)

    @staticmethod
    def _extract_pdf(file_path: str) -> str:
        """提取 PDF (.pdf) 文件中的所有页面文本"""
        reader = PdfReader(file_path)
        pages_text = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                pages_text.append(f"--- 第 {i+1} 页 ---\n{text.strip()}")
        return "\n\n".join(pages_text)

    @staticmethod
    def _extract_text_plain(file_path: str) -> str:
        """提取纯文本 (.txt / .md 等)，兼容 UTF-8 和 GBK 编码"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError:
            with open(file_path, "r", encoding="gbk", errors="ignore") as f:
                return f.read()


# ==============================================================================
# 三、 知识库向量管理器 (VectorStoreManager)
# ==============================================================================

class VectorStoreManager:
    """
    【类名】VectorStoreManager
    【功能】封装对 ChromaDB 向量数据库的所有增删查改操作，配合 Ollama bge-m3 向量模型
    """

    def __init__(self, persist_dir: str = CHROMA_PERSIST_DIR):
        """
        初始化向量数据库连接和向量模型
        """
        self.persist_dir = persist_dir
        # 初始化持久化 Chroma 客户端
        self.chroma_client = chromadb.PersistentClient(
            path=self.persist_dir,
            settings=Settings(allow_reset=True, anonymized_telemetry=False)
        )
        # 获取集合（若不存在则自动新建）
        self.collection = self.chroma_client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"description": "Agentic RAG 多文档知识库集合"}
        )
        # 懒加载 / 获取 Ollama 向量模型实例
        self.embeddings = get_ollama_embeddings()

    def inspect_document(self, file_path: str) -> Dict[str, Any]:
        """
        【功能】预检查单个文档是否包含复杂架构图/流程图特征，供判断是否需要弹窗提示用户确认清洗模式
        """
        doc_name = os.path.basename(file_path)
        raw_text = DocumentParser.extract_raw_text(file_path)
        diag_info = DocumentParser.inspect_diagram(raw_text)
        diag_info["doc_name"] = doc_name
        diag_info["file_size"] = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        diag_info["char_count"] = len(raw_text)
        return diag_info

    def check_document_duplicate(self, file_path: str, doc_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        【功能】极速前置查重：在不触发大模型清洗与架构图扫描前，快速判定文件是否已在知识库中存在
        【算法】双指纹比对机制 (Dual-Fingerprint):
               1. 计算原始正文指纹 raw_hash
               2. 计算代码规则降噪指纹 cleaned_hash
               3. 检索 ChromaDB 中是否存在任一匹配指纹 (100% 覆盖全部普通文档与包含框图文档)
               4. 结合 doc_name 元数据快速反查，杜绝指纹漂移

        【返回值】
            若为重复文档：返回 dict（包含 is_duplicate=True, doc_name, duplicate_of, total_chunks, message 等）
            若非重复文档：返回 None
        """
        if not os.path.exists(file_path):
            return None

        if not doc_name:
            doc_name = os.path.basename(file_path)

        try:
            raw_text = DocumentParser.extract_raw_text(file_path)
            if not raw_text or not raw_text.strip():
                return None

            import hashlib
            raw_hash = hashlib.sha256(raw_text.strip().encode("utf-8")).hexdigest()
            cleaned_text = DocumentParser.reform_complex_diagrams(raw_text, clean_mode="rule")
            cleaned_hash = hashlib.sha256(cleaned_text.strip().encode("utf-8")).hexdigest()

            # 1. 优先检索原始正文指纹 raw_hash
            existing_matches = self.collection.get(
                where={"content_hash": raw_hash},
                include=["metadatas"]
            )
            matched_hash = raw_hash

            # 2. 若未命中且存在规则清洗差异，检索规则降噪指纹 cleaned_hash
            if (not existing_matches or not existing_matches.get("metadatas") or len(existing_matches["metadatas"]) == 0) and cleaned_hash != raw_hash:
                existing_matches = self.collection.get(
                    where={"content_hash": cleaned_hash},
                    include=["metadatas"]
                )
                matched_hash = cleaned_hash

            # 3. 亦可通过同名 doc_name 直接查询并核验库内指纹是否与当前文件的双指纹匹配
            if not existing_matches or not existing_matches.get("metadatas") or len(existing_matches["metadatas"]) == 0:
                name_matches = self.collection.get(
                    where={"doc_name": doc_name},
                    include=["metadatas"]
                )
                if name_matches and name_matches.get("metadatas") and len(name_matches["metadatas"]) > 0:
                    db_h = name_matches["metadatas"][0].get("content_hash")
                    if db_h in (raw_hash, cleaned_hash):
                        existing_matches = name_matches
                        matched_hash = db_h

            if existing_matches and existing_matches.get("metadatas") and len(existing_matches["metadatas"]) > 0:
                meta = existing_matches["metadatas"][0]
                existing_doc_name = meta.get("doc_name", "")
                total_chunks = meta.get("total_chunks", len(existing_matches["metadatas"]))
                upload_time = meta.get("upload_time", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

                if existing_doc_name == doc_name:
                    return {
                        "doc_name": doc_name,
                        "total_chunks": total_chunks,
                        "char_count": len(raw_text),
                        "upload_time": upload_time,
                        "content_hash": matched_hash,
                        "is_duplicate": True,
                        "duplicate_of": doc_name,
                        "message": f"文档【{doc_name}】在知识库中已存在且内容完全一致，已自动跳过重复入库"
                    }
                else:
                    return {
                        "doc_name": doc_name,
                        "total_chunks": total_chunks,
                        "char_count": len(raw_text),
                        "upload_time": upload_time,
                        "content_hash": matched_hash,
                        "is_duplicate": True,
                        "duplicate_of": existing_doc_name,
                        "message": f"文件【{doc_name}】与已入库文档【{existing_doc_name}】内容完全一致，已自动跳过重复入库"
                    }

            return None
        except Exception as e:
            print(f"⚠️ [前置查重异常] 检查【{doc_name}】失败: {e}，降级走正常入库流程")
            return None

    def process_and_add_document(
        self,
        file_path: str,
        chunk_size: int = 600,
        chunk_overlap: int = 100,
        clean_mode: str = "rule"
    ) -> Dict[str, Any]:
        """
        【功能】流水线处理并添加单个文档：解析文本 -> 智能切块 -> 生成元数据 -> 向量化写入库

        【参数】
            file_path: 上传文件的本地路径
            chunk_size: 每个切片的最大字符数 (推荐 500-800，保留充足段落上下文)
            chunk_overlap: 切片之间的重叠字符数 (推荐 80-120，防止截断关键句)
            clean_mode: 清洗模式，"llm" (大模型智能升维清洗) 或 "rule" (代码规则普通清洗)

        【返回值】
            dict: 包含入库成功的统计信息 (文档名, 切片数, 字符总数, 清洗模式)
        """
        doc_name = os.path.basename(file_path)

        # 1. 【极速前置查重】避免任何不必要的数据清洗与分块向量计算开销
        dup_info = self.check_document_duplicate(file_path, doc_name=doc_name)
        if dup_info:
            print(f"⚡ [极速秒级跳过] {dup_info['message']} (SHA-256: {dup_info['content_hash'][:8]}...)")
            return dup_info

        # 如果之前存在同名旧版本（说明内容已发生变更），先彻底清除旧切片再写入新版
        self.delete_document(doc_name, delete_file=False)

        # 2. 文本提取与数据清洗
        clean_desc = "大模型智能清洗 (Mermaid/Markdown升维)" if clean_mode == "llm" else "代码规则普通清洗 (正则降噪)"
        print(f"\n📂 [文档提取与清洗] 正在读取并处理文档: 【{doc_name}】 | 选定清洗模式: 【{clean_desc}】...")
        raw_text = DocumentParser.extract_text(file_path, clean_mode=clean_mode)
        if not raw_text.strip():
            raise ValueError(f"文档内容为空或未能解析出有效文本: {doc_name}")

        import hashlib
        content_hash = hashlib.sha256(DocumentParser.extract_raw_text(file_path).strip().encode("utf-8")).hexdigest()

        print(f"\n📄 [开始处理新文档] 【{doc_name}】(有效字符: {len(raw_text)}, SHA-256: {content_hash[:8]}...)")

        # 3. 递归字符分块
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", "。", "！", "？", "；", " ", ""]
        )
        chunks = text_splitter.split_text(raw_text)
        total_chunks = len(chunks)

        if total_chunks == 0:
            raise ValueError("文档分块后切片数量为 0")

        print(f"✂️ [分块切分完成] 【{doc_name}】共切出 {total_chunks} 个切片 (单块: {chunk_size}, 重叠: {chunk_overlap})")

        # 4. 生成多维元数据与唯一 ID (挂载 content_hash)
        upload_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ids = []
        metadatas = []
        documents = []

        for idx, chunk_text in enumerate(chunks):
            chunk_id = f"{doc_name}_chunk_{idx}"
            ids.append(chunk_id)
            documents.append(chunk_text)
            metadatas.append({
                "doc_name": doc_name,
                "chunk_index": idx,
                "total_chunks": total_chunks,
                "upload_time": upload_time_str,
                "char_length": len(chunk_text),
                "content_hash": content_hash  # 👈 写入内容哈希指纹
            })

        # 5. 调用 Ollama bge-m3 批量计算向量
        print(f"🧠 [计算向量] 正在调用 Ollama bge-m3 批量计算 {total_chunks} 个切片的嵌入向量...")
        vectors = self.embeddings.embed_documents(documents)

        # 6. 批量存入 ChromaDB 集合
        print(f"💾 [持久化] 正在将切片与向量写入 ChromaDB ({COLLECTION_NAME})...")
        self.collection.add(
            ids=ids,
            embeddings=vectors,
            documents=documents,
            metadatas=metadatas
        )
        print(f"✅ [入库成功] 【{doc_name}】已成功持久化至知识库！\n")

        return {
            "doc_name": doc_name,
            "total_chunks": total_chunks,
            "char_count": len(raw_text),
            "upload_time": upload_time_str,
            "clean_mode": "大模型清洗" if clean_mode == "llm" else "代码普通清洗",
            "is_duplicate": False,
            "message": f"成功切分为 {total_chunks} 个切片并完成向量化入库 (清洗模式: {'大模型智能清洗' if clean_mode == 'llm' else '代码普通清洗'})"
        }

    def query_similar_chunks(
        self,
        query: str,
        top_k: int = 4,
        doc_name_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        【功能】向量相似度语义检索

        【参数】
            query: 用户的自然语言查询或关键词
            top_k: 返回最相关的切片条数
            doc_name_filter: 可选。如果指定了文档名，则仅在该文档内检索；若为 None 则跨所有文档检索

        【返回值】
            List[dict]: 排序后的切片列表，每个元素包含:
                - text: 切片正文
                - doc_name: 所属文档
                - chunk_index: 切片序号
                - distance: 相似度距离 (越小越相关)
        """
        # 将查询文本转化为向量
        query_vector = self.embeddings.embed_query(query)

        # 构建过滤条件
        where_filter = None
        if doc_name_filter and doc_name_filter.strip():
            where_filter = {"doc_name": doc_name_filter.strip()}

        filter_desc = f"文档《{doc_name_filter.strip()}》" if (doc_name_filter and doc_name_filter.strip()) else "全库跨文档"
        print(f"🔍 [向量相似度检索] 范围: {filter_desc} | 查询词: '{query}' | Top-{top_k}")

        results = self.collection.query(
            query_embeddings=[query_vector],
            n_results=top_k,
            where=where_filter,
            include=["documents", "metadatas", "distances"]
        )

        retrieved = []
        if results and results.get("documents") and len(results["documents"]) > 0:
            docs_list = results["documents"][0]
            metas_list = results["metadatas"][0]
            dists_list = results["distances"][0] if "distances" in results else [0.0] * len(docs_list)

            for text, meta, dist in zip(docs_list, metas_list, dists_list):
                retrieved.append({
                    "text": text,
                    "doc_name": meta.get("doc_name", "未知文档"),
                    "chunk_index": meta.get("chunk_index", 0),
                    "total_chunks": meta.get("total_chunks", 1),
                    "upload_time": meta.get("upload_time", ""),
                    "distance": round(float(dist), 4)
                })

        print(f"🎯 [检索命中完成] 成功命中 {len(retrieved)} 个高相关度切片 (首条距离: {retrieved[0]['distance'] if retrieved else '无'})")
        return retrieved

    def list_all_documents(self) -> List[Dict[str, Any]]:
        """
        【功能】获取当前知识库中所有已入库的文档清单与统计信息

        【返回值】
            List[dict]: 包含每个文档的名称、切片总数、入库时间、本地文件是否存在等
        """
        # 从 ChromaDB 获取所有元数据
        all_data = self.collection.get(include=["metadatas"])
        metadatas = all_data.get("metadatas", [])

        docs_summary: Dict[str, Dict[str, Any]] = {}

        for meta in metadatas:
            name = meta.get("doc_name")
            if not name:
                continue
            if name not in docs_summary:
                local_path = os.path.join(UPLOAD_DIR, name)
                file_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
                docs_summary[name] = {
                    "doc_name": name,
                    "total_chunks": meta.get("total_chunks", 0),
                    "upload_time": meta.get("upload_time", "未知"),
                    "file_size_bytes": file_size,
                    "file_size_formatted": f"{file_size / 1024:.1f} KB" if file_size else "0 KB"
                }

        return list(docs_summary.values())

    def get_document_full_content(self, doc_name: str, max_chunks: int = 10) -> str:
        """
        【功能】按切片顺序读取指定文档的连续内容（用于 Agent 需要全面深入查看某篇核心文档时）

        【参数】
            doc_name: 文档名称
            max_chunks: 最大读取的切片数，避免超出上下文窗口

        【返回值】
            str: 拼接后的文档内容
        """
        results = self.collection.get(
            where={"doc_name": doc_name},
            include=["documents", "metadatas"]
        )

        docs = results.get("documents", [])
        metas = results.get("metadatas", [])

        if not docs:
            return f"未能找到文档【{doc_name}】的内容。"

        # 按 chunk_index 排序
        combined = sorted(zip(docs, metas), key=lambda x: x[1].get("chunk_index", 0))

        content_parts = []
        for doc_text, meta in combined[:max_chunks]:
            idx = meta.get("chunk_index", 0)
            content_parts.append(f"--- [切片 {idx + 1}] ---\n{doc_text}")

        total_actual = len(combined)
        header = f"=== 正在阅读文档【{doc_name}】(共 {total_actual} 个切片，当前展示前 {min(max_chunks, total_actual)} 个) ===\n\n"
        return header + "\n\n".join(content_parts)

    def delete_document(self, doc_name: str, delete_file: bool = True) -> bool:
        """
        【功能】从知识库删除指定文档的所有切片，并选择性删除 uploads 目录下的物理文件

        【参数】
            doc_name: 文件名
            delete_file: 是否同步删除 uploads 目录下的原始文件

        【返回值】
            bool: 是否执行成功
        """
        try:
            # 1. 从 ChromaDB 删除相关向量与元数据
            self.collection.delete(where={"doc_name": doc_name})

            # 2. 从本地磁盘删除文件
            if delete_file:
                local_path = os.path.join(UPLOAD_DIR, doc_name)
                if os.path.exists(local_path):
                    os.remove(local_path)
            print(f"🗑️ [文档删除成功] 已清空【{doc_name}】在 ChromaDB 中的所有切片" + (" 及 uploads 本地物理文件" if delete_file else ""))
            return True
        except Exception as e:
            print(f"❌ [删除异常] 删除文档【{doc_name}】时发生错误: {e}")
            return False

    def reset_database(self) -> bool:
        """
        【功能】清空知识库所有数据及上传的原始文件 (危险操作，供重置用)
        """
        try:
            self.chroma_client.delete_collection(COLLECTION_NAME)
            self.collection = self.chroma_client.get_or_create_collection(COLLECTION_NAME)
            # 清空 uploads 目录
            for item in os.listdir(UPLOAD_DIR):
                path = os.path.join(UPLOAD_DIR, item)
                if os.path.isfile(path):
                    os.remove(path)
            return True
        except Exception as e:
            print(f"重置知识库失败: {e}")
            return False


# 全局单例管理器（线程安全双重检查锁）
_global_vector_store: Optional[VectorStoreManager] = None
_vector_store_lock = threading.Lock()

def get_vector_store() -> VectorStoreManager:
    """获取向量数据库管理器的全局单例实例（线程安全）"""
    global _global_vector_store
    if _global_vector_store is None:
        with _vector_store_lock:
            if _global_vector_store is None:
                _global_vector_store = VectorStoreManager()
    return _global_vector_store
