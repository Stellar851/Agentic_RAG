"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
自动化测试集生成与人工审核导出模块 (dataset_generator.py)

【模块核心功能】
1. 模型 A (qwen3.8-max-0902) 驱动：基于知识库文本切片，利用批量派生 Prompt + 多并发线程池，
   快速生成高覆盖度的“问题 - 黄金标准答案 - 来源切片依据”测试三元组。
2. 现有基准指南解析：支持一键将《08_Agentic_RAG测试题库与多跳推理评测指南.md》解析为结构化用例。
3. 人工审核 Excel 导出：利用 openpyxl 生成具有专业商务视觉样式的待审核表格 (.xlsx)，
   包含审核状态、人工修正答案、备注列，支持人工在 Excel 中快速审阅把关。
================================================================================
"""

import os
import sys
import json
import re
import time
import random
import logging
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Tuple, Callable

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from models import get_deepseek_llm, ALI_DEFAULT_GENERATOR_MODEL

DEFAULT_GEN_WORKERS = max(1, int(os.getenv("EVAL_GEN_WORKERS", "8")))

logger = logging.getLogger(__name__)

BENCHMARK_DIR = os.path.join(CURRENT_DIR, "benchmarks")
os.makedirs(BENCHMARK_DIR, exist_ok=True)

# 路径配置：出题严格基于数据中台 (Port 8001) 实际收敛的知识库 uploads 目录。
# 兼容旧版 eval_platform/uploads 与项目根目录兜底路径。
_PROJECT_TOP = os.path.dirname(PROJECT_ROOT)
_UPLOAD_CANDIDATES = [
    os.path.join(PROJECT_ROOT, "data_platform", "uploads"),
    os.path.join(CURRENT_DIR, "uploads"),
    os.path.join(_PROJECT_TOP, "Agentic_RAG", "uploads"),
    os.path.join(_PROJECT_TOP, "知识库测试数据集"),
]
DOCS_DIR = next((d for d in _UPLOAD_CANDIDATES if os.path.isdir(d)), _UPLOAD_CANDIDATES[0])


# ==============================================================================
# 一、 从现有指南文档中解析基准题库 (1秒无损转换)
# ==============================================================================

def parse_guide_markdown(file_path: Optional[str] = None, case_count: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    【功能】解析《08_Agentic_RAG测试题库与多跳推理评测指南.md》，支持按 case_count 动态截取题数
    """
    path = file_path or os.path.join(DOCS_DIR, "08_Agentic_RAG测试题库与多跳推理评测指南.md")
    if not os.path.exists(path):
        print(f"  ✗ 指南文件不存在 | {path}")
        return []

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    cases = []
    # 提取所有组与测试用例
    groups = re.split(r"##\s+第[一二三四五]组：", content)
    group_names = [
        "单跳细节检索",
        "跨文档多跳关联推理",
        "多规则边界推断",
        "跨方案对比选型",
        "未知领域负向拒答"
    ]

    case_id = 1
    # 遍历每个分组
    for g_idx, group_text in enumerate(groups[1:], start=0):
        category = group_names[g_idx] if g_idx < len(group_names) else "综合业务推理"
        
        # 拆分具体用例 ### 测试用例 x.x
        case_blocks = re.split(r"###\s+测试用例\s+[\d\.]+[：:]", group_text)
        for block in case_blocks[1:]:
            # 提取测试提问
            q_match = re.search(r"-\s+\*\*测试提问\*\*：\s*\n\s*>\s*“(.*?)”", block, re.DOTALL)
            if not q_match:
                q_match = re.search(r"-\s+\*\*测试提问\*\*：\s*\n\s*>\s*\"(.*?)\"", block, re.DOTALL)
            query = q_match.group(1).strip() if q_match else ""

            # 提取标准参考答案
            ans_match = re.search(r"-\s+\*\*标准参考答案\*\*：(.*?)(?=-\s+\*\*验证链条|\Z)", block, re.DOTALL)
            ground_truth = ans_match.group(1).strip() if ans_match else ""
            ground_truth = re.sub(r"^\s*-\s*", "", ground_truth, flags=re.MULTILINE).strip()

            # 提取验证出处与文档
            src_match = re.search(r"-\s+\*\*验证链条与知识出处\*\*：(.*?)(?=---|\Z)", block, re.DOTALL)
            source_info = src_match.group(1).strip() if src_match else ""

            doc_match = re.search(r"来源文档：`(.*?)`", source_info)
            source_doc = doc_match.group(1) if doc_match else "知识库多文档"

            if query:
                cases.append({
                    "id": case_id,
                    "category": category,
                    "query": query,
                    "ground_truth": ground_truth,
                    "source_chunks": source_info,
                    "source_doc": source_doc,
                    "review_status": "通过",
                    "reviewed_ground_truth": "",
                    "review_notes": ""
                })
                case_id += 1

    if case_count and case_count > 0:
        cases = cases[:case_count]
        for idx, c in enumerate(cases, start=1):
            c["id"] = idx

    return cases


# ==============================================================================
# 二、 模型 A (qwen3.8-max-0902) 批量自动化派生题目
# ==============================================================================

PROMPT_GENERATE_CASES = """你是一名严格的企业级知识库评测专家架构师。
请仔细阅读以下给定的知识文档片段，为该文档派生出 4 道高质量且针对 RAG 关键能力的【定向指标压力测试题】。

【待考查切片内容】：
--------------------------------------------------
文档名称：{doc_name}
{doc_content}
--------------------------------------------------

【四大定向指标考题设计规范】：
1. 考题 1【🔍 精度-高噪辨析题】：针对切片中具体的数值、超时时间、端口协议、错误码或与相似技术容易混淆的关键细节，答案必须唯一且精准，考查检索切片能否排在首位（Top-1 精确度）。
2. 考题 2【📚 召回率-多跳聚合题】：考查跨多个章节、前后依赖条件或多属性综合比对的复杂问题，要求必须同时召回多个事实要点方可完整解答（考查上下文召回率）。
3. 考题 3【🎯 相关性-指令约束题】：提问中包含明确的形式或格式约束（例如“请直接列出核心三要素并说明各自影响，不含多余客套”），考查回答是否直击核心要点、无冗余废话（考查答案相关性）。
4. 考题 4【🛡️ 忠实度-边界拒答题】：提出一个与该文档主题密切相关但文档中“明确未提及、未开放或未支持”的负向伪命题或陷阱问题，黄金标准答案必须明确指出文档未提及或应当安全拒答（考查模型抗幻觉与诚实拒答能力）。

【输出格式要求】：
请严格仅输出如下 JSON 数组格式，严禁包含任何 Markdown 格式前缀或额外文字：
[
  {{
    "category": "🔍 精度-高噪辨析题",
    "query": "问题描述...",
    "ground_truth": "标准参考答案...",
    "source_chunks": "引用的原文关键依据句子...",
    "source_doc": "{doc_name}"
  }},
  {{
    "category": "📚 召回率-多跳聚合题",
    "query": "问题描述...",
    "ground_truth": "标准参考答案...",
    "source_chunks": "引用的原文关键依据句子...",
    "source_doc": "{doc_name}"
  }},
  {{
    "category": "🎯 相关性-指令约束题",
    "query": "问题描述...",
    "ground_truth": "标准参考答案...",
    "source_chunks": "引用的原文关键依据句子...",
    "source_doc": "{doc_name}"
  }},
  {{
    "category": "🛡️ 忠实度-边界拒答题",
    "query": "问题描述...",
    "ground_truth": "知识库当前文档未规定该内容，系统应明确指出未检索到并如实拒答...",
    "source_chunks": "原文中未定义此项特性或明确未开放...",
    "source_doc": "{doc_name}"
  }}
]
"""


CATEGORIES_CYCLE = [
    "🔍 精度-高噪辨析题",
    "📚 召回率-多跳聚合题",
    "🎯 相关性-指令约束题",
    "🛡️ 忠实度-边界拒答题"
]

CATEGORY_SHORT_NAMES = {
    "🔍 精度-高噪辨析题": "精度",
    "📚 召回率-多跳聚合题": "多跳",
    "🎯 相关性-指令约束题": "约束",
    "🛡️ 忠实度-边界拒答题": "拒答",
}


def _shorten(text: Any, limit: int = 46) -> str:
    """将日志中的长文本压缩为单行，避免控制台换行刷屏。"""
    value = " ".join(str(text).split())
    return value if len(value) <= limit else value[: limit - 1] + "…"

CATEGORY_REQUIREMENTS = {
    "🔍 精度-高噪辨析题": "针对切片中具体的数值、超时时间、端口协议、错误码或与相似技术容易混淆的关键细节，提问必须严谨，答案必须唯一且精准（考查检索切片能否排在首位）。",
    "📚 召回率-多跳聚合题": "考查跨多个章节、前后依赖条件或多属性综合比对的复杂问题，要求回答必须同时综合多个事实要点方可完整解答（考查上下文召回率）。",
    "🎯 相关性-指令约束题": "提问中包含明确的形式或格式约束（例如“请直接列出核心三要素并说明各自影响，不含多余客套”），考查回答是否直击核心要点、无冗余废话（考查答案相关性）。",
    "🛡️ 忠实度-边界拒答题": "提出一个与该文档主题密切相关但文档中“明确未提及、未开放或未支持”的负向伪命题或陷阱问题，黄金标准答案必须明确指出文档未提及或应当安全拒答（考查模型抗幻觉与诚实拒答能力）。"
}

PROMPT_GENERATE_TARGET_CASE = """你是一名严格的企业级知识库评测专家架构师。
请仔细阅读以下给定的知识文档片段，为该文档精准派生出 1 道高质量且针对 RAG 关键能力的【{target_category}】。

【待考查切片内容】：
--------------------------------------------------
文档名称：{doc_name}
{doc_content}
--------------------------------------------------

【当前专项考题设计规范】：
{category_requirement}

【输出格式要求】：
请严格仅输出如下 JSON 数组格式（包含 1 个对象），严禁包含任何 Markdown 格式前缀或额外文字：
[
  {{
    "category": "{target_category}",
    "query": "针对该文档业务事实的明确提问...",
    "ground_truth": "基于切片事实的标准参考答案（若为边界拒答题，答案必须明确指出文档未提及或未开放该功能）...",
    "source_chunks": "引用的原文关键依据句子...",
    "source_doc": "{doc_name}"
  }}
]
"""

def _extract_doc_text_for_generation(doc_path: str) -> str:
    """安全提取多格式文档纯文本供出题使用，避免耦合已迁移的 vector_store。"""
    ext = os.path.splitext(doc_path)[1].lower()
    if ext in [".md", ".txt", ".json", ".csv", ".py"]:
        try:
            with open(doc_path, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError:
            with open(doc_path, "r", encoding="gbk", errors="ignore") as f:
                return f.read()

    if ext == ".docx":
        from docx import Document as DocxDocument
        doc = DocxDocument(doc_path)
        return "\n\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())

    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(doc_path)
        return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages if (page.extract_text() or "").strip())

    if ext == ".pptx":
        from pptx import Presentation
        prs = Presentation(doc_path)
        slides = []
        for slide in prs.slides:
            parts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for p in shape.text_frame.paragraphs:
                        if p.text.strip():
                            parts.append(p.text.strip())
                elif shape.has_table:
                    for row in shape.table.rows:
                        cells = [c.text.strip() for c in row.cells if c.text.strip()]
                        if cells:
                            parts.append(" | ".join(cells))
            if parts:
                slides.append("\n".join(parts))
        return "\n\n".join(slides)

    with open(doc_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


PROMPT_GENERATE_DUAL_CASES = """你是一名严格的企业级知识库评测专家架构师。
请仔细阅读以下给定知识文档片段（长文档可能包含前段与中后段不同章节），为该文档一次性精准派生出 2 道不同专项的高质量考题。

【待考查文档】：{doc_name}
--------------------------------------------------
{doc_content}
--------------------------------------------------

【考题 1 专项要求 ({cat_1})】：
{req_1}

【考题 2 专项要求 ({cat_2})】：
{req_2}

【输出格式要求】：
请严格仅输出如下包含 2 个对象的 JSON 数组，严禁包含任何 Markdown 格式前缀或额外解释文字：
[
  {{
    "category": "{cat_1}",
    "query": "第1题明确提问...",
    "ground_truth": "第1题基于切片事实的标准参考答案（边界拒答题须明确指出文档未提及）...",
    "source_chunks": "引用的原文关键依据句子...",
    "source_doc": "{doc_name}"
  }},
  {{
    "category": "{cat_2}",
    "query": "第2题明确提问...",
    "ground_truth": "第2题基于切片事实的标准参考答案（边界拒答题须明确指出文档未提及）...",
    "source_chunks": "引用的原文关键依据句子...",
    "source_doc": "{doc_name}"
  }}
]
"""

PROMPT_GENERATE_CROSS_DOC_CASES = """你是一名严格的企业级知识库评测专家架构师。
以下给出两篇存在业务关联或同领域对照关系的知识库文档片段，请基于这两篇文档的交叉点，设计 2 道必须**同时检索结合文档 A 与文档 B 才能完整解答**的【📚 召回率-多跳聚合题】（禁止出只看单篇就能答出的题）。

【文档 A：{doc_a}】：
--------------------------------------------------
{content_a}
--------------------------------------------------

【文档 B：{doc_b}】：
--------------------------------------------------
{content_b}
--------------------------------------------------

【输出格式要求】：
请严格仅输出如下包含 2 个对象的 JSON 数组，严禁包含任何 Markdown 格式前缀或额外解释文字：
[
  {{
    "category": "📚 召回率-多跳聚合题",
    "query": "必须综合文档A与文档B才能解答的跨文档关联对比或约束推理题1...",
    "ground_truth": "完整融合文档A与文档B核心依据的标准答案1...",
    "source_chunks": "分别摘录文档A与文档B的关键依据...",
    "source_doc": "{doc_a} + {doc_b}"
  }},
  {{
    "category": "📚 召回率-多跳聚合题",
    "query": "必须综合文档A与文档B才能解答的跨文档关联对比或约束推理题2...",
    "ground_truth": "完整融合文档A与文档B核心依据的标准答案2...",
    "source_chunks": "分别摘录文档A与文档B的关键依据...",
    "source_doc": "{doc_a} + {doc_b}"
  }}
]
"""

_DOMAIN_GROUPS: List[Tuple[str, Tuple[str, ...]]] = [
    ("compliance_security", ("法律", "安全", "合规", "脱敏", "隐私", "个人信息", "反洗钱", "审计", "开源", "gbt22239")),
    ("database_tx", ("tidb", "mysql", "事务", "容灾", "湖仓", "存储", "微服务", "rocketmq", "kafka")),
    ("k8s_infra", ("k8s", "kubernetes", "pod", "service", "scheduler", "rbac", "算力", "显存", "推理", "devops", "ci-cd")),
    ("api_ecommerce", ("fastapi", "api", "支付", "订单", "电商", "供应链", "冷链", "清算", "发票", "faq")),
    ("hr_business", ("人力资源", "考勤", "休假", "出差", "外派", "财报", "医药", "客服", "iot")),
]


def _sample_doc_content(content: str, window_index: int = 0, max_chars: int = 3800) -> str:
    """短文档返回全文；长文档同时拼接“前段核心 + 中/后段偏移章节”，避免重复只看开头。"""
    text = (content or "").strip()
    if len(text) <= max_chars:
        return text
    head_len = max_chars // 2
    tail_len = max_chars - head_len - 40
    tail_span = max(1, len(text) - head_len - tail_len)
    # window_index=0 优先看中段，后续轮次逐步向后段滑动
    offset_ratio = ((window_index + 1) * 0.37) % 1.0
    second_start = head_len + int(tail_span * offset_ratio)
    second_part = text[second_start: second_start + tail_len]
    return (
        text[:head_len]
        + "\n\n[...中间略，以下为该文档后续章节片段...]\n\n"
        + second_part
    )


def _extract_json_list_safely(raw_text: str) -> List[Dict[str, Any]]:
    text = (raw_text or "").strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", text)
    candidate = match.group(1) if match else text
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return [parsed]
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError as orig_exc:
        # 若 JSON 数组在中途截断（例如第 2 题未闭合），逐个抢救已完整生成的对象
        decoder = json.JSONDecoder()
        salvaged: List[Dict[str, Any]] = []
        idx = 0
        length = len(text)
        while idx < length:
            brace_idx = text.find("{", idx)
            if brace_idx < 0:
                break
            try:
                obj, end_idx = decoder.raw_decode(text, brace_idx)
                if isinstance(obj, dict) and str(obj.get("query") or "").strip():
                    salvaged.append(obj)
                idx = max(brace_idx + 1, end_idx)
            except json.JSONDecodeError:
                idx = brace_idx + 1
        if salvaged:
            return salvaged
        raise orig_exc


def _invoke_generator_json(
    prompt: str,
    model_a: str,
    rate_limit_retries: int = 3,
    max_tokens: int = 4096,
    disable_thinking: bool = True,
) -> List[Dict[str, Any]]:
    """调用命题大模型并安全解析 JSON 数组，默认关闭思考链消耗并内置 429 退避与参数回退。"""
    use_extra_body = bool(disable_thinking)
    rl_attempts = 0
    while True:
        llm_kwargs: Dict[str, Any] = {
            "model": model_a,
            "temperature": 0.3,
            "streaming": False,
            "max_tokens": max_tokens,
        }
        if use_extra_body:
            llm_kwargs["extra_body"] = {"enable_thinking": False}
        llm = get_deepseek_llm(**llm_kwargs)
        try:
            resp = llm.invoke(prompt)
            content = resp.content.strip() if hasattr(resp, "content") else str(resp).strip()
            return _extract_json_list_safely(content)
        except Exception as exc:
            err_low = str(exc).lower()
            is_unsupported_extra = use_extra_body and any(
                tok in err_low
                for tok in ("enable_thinking", "extra_body", "unknown parameter", "unrecognized", "invalid_request", "400")
            )
            if is_unsupported_extra:
                logger.info("模型 %s 不支持 enable_thinking=False，已自动回退普通模式重试", model_a)
                use_extra_body = False
                continue
            is_rl = any(tok in err_low for tok in ("429", "rate limit", "too many requests", "burst", "throttl"))
            if is_rl and rl_attempts < rate_limit_retries:
                time.sleep(min(12.0, 2.0 ** (rl_attempts + 1)) + random.uniform(0.0, 0.6))
                rl_attempts += 1
                continue
            raise


def _build_cross_doc_pairs(
    doc_files: List[str],
    pair_count: int,
    exclude_pairs: Optional[set] = None,
) -> List[Tuple[str, str]]:
    """按主题域智能配对同领域文档，构建真实跨文档多跳任务。"""
    if len(doc_files) < 2 or pair_count <= 0:
        return []

    buckets: Dict[str, List[str]] = {name: [] for name, _ in _DOMAIN_GROUPS}
    buckets["other"] = []
    for fname in doc_files:
        low = fname.lower()
        placed = False
        for gname, kws in _DOMAIN_GROUPS:
            if any(kw in low for kw in kws):
                buckets[gname].append(fname)
                placed = True
                break
        if not placed:
            buckets["other"].append(fname)

    candidate_pairs: List[Tuple[str, str]] = []
    seen_pairs = set(exclude_pairs or set())

    def _add_pair(a: str, b: str):
        if a == b:
            return
        key = tuple(sorted((a, b)))
        if key not in seen_pairs:
            seen_pairs.add(key)
            candidate_pairs.append((a, b))

    # 1. 优先同领域内邻接配对与跨步配对
    for _, items in buckets.items():
        if len(items) < 2:
            continue
        for idx in range(0, len(items) - 1, 2):
            _add_pair(items[idx], items[idx + 1])
        for idx in range(1, len(items) - 1, 2):
            _add_pair(items[idx], items[idx + 1])

    # 2. 若组内配对不足，再按全库跨步顺序补充跨界关联对
    if len(candidate_pairs) < pair_count:
        n = len(doc_files)
        for gap in range(1, n):
            for i in range(n):
                _add_pair(doc_files[i], doc_files[(i + gap) % n])
                if len(candidate_pairs) >= pair_count * 2:
                    break
            if len(candidate_pairs) >= pair_count * 2:
                break

    return candidate_pairs[:pair_count]


def _format_generated_cases(raw_items: List[Dict[str, Any]], default_doc: str, default_cat: str) -> List[Dict[str, Any]]:
    formatted: List[Dict[str, Any]] = []
    for c in raw_items or []:
        if not isinstance(c, dict):
            continue
        q = str(c.get("query") or "").strip()
        if not q:
            continue
        formatted.append({
            "category": str(c.get("category") or default_cat).strip(),
            "query": q,
            "ground_truth": str(c.get("ground_truth") or "").strip(),
            "source_chunks": str(c.get("source_chunks") or "").strip(),
            "source_doc": str(c.get("source_doc") or default_doc).strip(),
            "review_status": "待确认",
            "reviewed_ground_truth": "",
            "review_notes": "",
        })
    return formatted


def generate_questions_for_doc(
    doc_name: str,
    doc_path: str,
    model_a: str = ALI_DEFAULT_GENERATOR_MODEL,
    target_category: Optional[str] = None,
    *,
    window_index: int = 0,
    dual_categories: Optional[Tuple[str, str]] = None,
) -> List[Dict[str, Any]]:
    """调用模型 A 对单个文档片段派生考题（支持单题或双题同出+长文档滑动采样）。"""
    try:
        content = _extract_doc_text_for_generation(doc_path)
        if not content.strip():
            return []

        trimmed_content = _sample_doc_content(content, window_index=window_index, max_chars=3800)
        if dual_categories:
            cat_1, cat_2 = dual_categories
            prompt = PROMPT_GENERATE_DUAL_CASES.format(
                doc_name=doc_name,
                doc_content=trimmed_content,
                cat_1=cat_1,
                req_1=CATEGORY_REQUIREMENTS[cat_1],
                cat_2=cat_2,
                req_2=CATEGORY_REQUIREMENTS[cat_2],
            )
            cases = _invoke_generator_json(prompt, model_a=model_a)
            return _format_generated_cases(cases, default_doc=doc_name, default_cat=cat_1)[:2]

        if target_category and target_category in CATEGORY_REQUIREMENTS:
            prompt = PROMPT_GENERATE_TARGET_CASE.format(
                doc_name=doc_name,
                doc_content=trimmed_content,
                target_category=target_category,
                category_requirement=CATEGORY_REQUIREMENTS[target_category]
            )
        else:
            prompt = PROMPT_GENERATE_CASES.format(doc_name=doc_name, doc_content=trimmed_content)

        cases = _invoke_generator_json(prompt, model_a=model_a)
        return _format_generated_cases(cases, default_doc=doc_name, default_cat=target_category or "综合业务推理")
    except Exception:
        logger.debug("生成文档 %s 的题目失败", doc_name, exc_info=True)
        return []


def generate_cross_doc_questions(
    doc_a: str,
    doc_b: str,
    docs_dir: str,
    model_a: str = ALI_DEFAULT_GENERATOR_MODEL,
) -> List[Dict[str, Any]]:
    """基于两篇关联文档片段联合生成 2 道真实跨文档多跳聚合题。"""
    try:
        text_a = _extract_doc_text_for_generation(os.path.join(docs_dir, doc_a))
        text_b = _extract_doc_text_for_generation(os.path.join(docs_dir, doc_b))
        if not text_a.strip() or not text_b.strip():
            return []
        prompt = PROMPT_GENERATE_CROSS_DOC_CASES.format(
            doc_a=doc_a,
            doc_b=doc_b,
            content_a=_sample_doc_content(text_a, window_index=0, max_chars=2100),
            content_b=_sample_doc_content(text_b, window_index=1, max_chars=2100),
        )
        cases = _invoke_generator_json(prompt, model_a=model_a)
        formatted = _format_generated_cases(
            cases,
            default_doc=f"{doc_a} + {doc_b}",
            default_cat="📚 召回率-多跳聚合题",
        )
        for item in formatted:
            item["category"] = "📚 召回率-多跳聚合题"
            if "+" not in item["source_doc"]:
                item["source_doc"] = f"{doc_a} + {doc_b}"
        return formatted[:2]
    except Exception:
        logger.debug("生成跨文档多跳题目失败 (%s + %s)", doc_a, doc_b, exc_info=True)
        return []


def generate_benchmark_from_docs(
    docs_dir: str = DOCS_DIR,
    model_a: str = ALI_DEFAULT_GENERATOR_MODEL,
    max_workers: int = DEFAULT_GEN_WORKERS,
    case_count: int = 10,
    existing_cases: Optional[List[Dict[str, Any]]] = None,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    """
    【混合黄金出题引擎】全库 100% 广度前后段覆盖 (76%) + 真实双文档跨跳多跳题 (24%)，
    支持保留已有题库增量补齐、自动缺口补齐与实时批次进度回调。
    """
    if not os.path.exists(docs_dir):
        print(f"  ✗ 知识库目录不存在 | {docs_dir}")
        return []

    doc_files = sorted(
        f for f in os.listdir(docs_dir)
        if f.endswith((".md", ".txt", ".docx", ".pdf", ".pptx"))
        and not f.startswith("08_") and not f.startswith("README")
    )

    total_available_docs = len(doc_files)
    if total_available_docs == 0:
        print("  ✗ 未发现可用业务文档")
        return []

    target_count = max(1, case_count) if case_count else 10
    effective_workers = max(1, int(max_workers or DEFAULT_GEN_WORKERS))
    started_at = time.perf_counter()

    def _emit(payload: Dict[str, Any]) -> None:
        if on_progress is None:
            return
        try:
            on_progress(payload)
        except Exception:
            logger.debug("on_progress 回调执行异常", exc_info=True)

    all_cases: List[Dict[str, Any]] = []
    seen_queries = set()
    covered_docs_so_far = set()
    used_cross_pairs = set()
    current_cross_cnt = 0

    for item in (existing_cases or []):
        if not isinstance(item, dict):
            continue
        q = str(item.get("query") or "").strip()
        if not q:
            continue
        q_norm = " ".join(q.split()).lower()
        if q_norm in seen_queries:
            continue
        seen_queries.add(q_norm)
        copied = dict(item)
        all_cases.append(copied)
        parts = [p.strip() for p in str(copied.get("source_doc") or "").split("+") if p.strip()]
        for p in parts:
            covered_docs_so_far.add(p)
        if len(parts) >= 2:
            current_cross_cnt += 1
            used_cross_pairs.add(tuple(sorted((parts[0], parts[1]))))

    initial_existing_count = len(all_cases)
    if initial_existing_count >= target_count:
        _emit({
            "event": "meta",
            "model_a": model_a,
            "available_docs": total_available_docs,
            "total_batches": 0,
            "target_count": target_count,
            "existing_count": initial_existing_count,
            "workers": effective_workers,
            "round": 1,
            "is_backfill": False,
        })
        all_cases = all_cases[:target_count]
        for idx, c in enumerate(all_cases, start=1):
            c["id"] = idx
        return all_cases

    single_cat_pool = [
        "🔍 精度-高噪辨析题",
        "🎯 相关性-指令约束题",
        "🛡️ 忠实度-边界拒答题",
    ]
    doc_window_tracker: Dict[str, int] = {}

    def _plan_tasks(need_count: int, is_initial_full: bool) -> List[Dict[str, Any]]:
        planned: List[Dict[str, Any]] = []
        if is_initial_full:
            if need_count < 8:
                for i in range(need_count):
                    doc_name = doc_files[i % total_available_docs]
                    cat = CATEGORIES_CYCLE[i % len(CATEGORIES_CYCLE)]
                    win_idx = doc_window_tracker.get(doc_name, 0)
                    doc_window_tracker[doc_name] = win_idx + 1
                    planned.append({"type": "single", "doc": doc_name, "cat": cat, "window": win_idx})
                return planned

            cross_target = max(2, int(round(need_count * 0.24)))
            if cross_target % 2 == 1:
                cross_target = max(2, cross_target - 1)
            cross_target = min(cross_target, need_count - 2) if len(doc_files) >= 2 else 0
            single_target = need_count - cross_target

            cross_pairs = _build_cross_doc_pairs(doc_files, cross_target // 2, exclude_pairs=used_cross_pairs) if cross_target > 0 else []
            for a, b in cross_pairs:
                used_cross_pairs.add(tuple(sorted((a, b))))
            cross_used_docs = {d for pair in cross_pairs for d in pair}
            ordered_single_docs = [d for d in doc_files if d not in cross_used_docs] + [
                d for d in doc_files if d in cross_used_docs
            ]
            single_batch_cnt = (single_target + 1) // 2
            for i in range(single_batch_cnt):
                doc_name = ordered_single_docs[i % len(ordered_single_docs)]
                win_idx = doc_window_tracker.get(doc_name, 0)
                doc_window_tracker[doc_name] = win_idx + 1
                cat1 = single_cat_pool[(i * 2) % len(single_cat_pool)]
                cat2 = single_cat_pool[(i * 2 + 1) % len(single_cat_pool)]
                planned.append({"type": "dual", "doc": doc_name, "cats": (cat1, cat2), "window": win_idx})
            for doc_a, doc_b in cross_pairs:
                planned.append({"type": "cross", "doc_a": doc_a, "doc_b": doc_b})
            return planned

        # 增量补齐或缺口重试模式：优先覆盖尚未出现过的文档与未配对的跨文档组合
        if target_count >= 8 and len(doc_files) >= 2:
            desired_cross = max(2, int(round(target_count * 0.24)))
            if desired_cross % 2 == 1:
                desired_cross = max(2, desired_cross - 1)
            desired_cross = min(desired_cross, target_count - 2)
        else:
            desired_cross = 0

        raw_cross_need = min(need_count, max(0, desired_cross - current_cross_cnt))
        if raw_cross_need == 1:
            cross_need = 2 if need_count >= 2 else 0
        elif raw_cross_need % 2 == 1:
            cross_need = max(0, raw_cross_need - 1)
        else:
            cross_need = raw_cross_need

        cross_pairs = (
            _build_cross_doc_pairs(doc_files, cross_need // 2, exclude_pairs=used_cross_pairs)
            if cross_need > 0 else []
        )
        for a, b in cross_pairs:
            used_cross_pairs.add(tuple(sorted((a, b))))
        actual_cross_planned = len(cross_pairs) * 2
        single_need = max(0, need_count - actual_cross_planned)

        cross_used_docs = {d for pair in cross_pairs for d in pair}
        uncovered = [d for d in doc_files if d not in covered_docs_so_far and d not in cross_used_docs]
        covered_non_cross = [d for d in doc_files if d in covered_docs_so_far and d not in cross_used_docs]
        covered_in_cross = [d for d in doc_files if d in cross_used_docs]
        prioritized_docs = uncovered + covered_non_cross + covered_in_cross
        if not prioritized_docs:
            prioritized_docs = list(doc_files)

        if single_need == 1:
            doc_name = prioritized_docs[0]
            win_idx = doc_window_tracker.get(doc_name, 0)
            doc_window_tracker[doc_name] = win_idx + 1
            cat = CATEGORIES_CYCLE[len(all_cases) % len(CATEGORIES_CYCLE)]
            planned.append({"type": "single", "doc": doc_name, "cat": cat, "window": win_idx})
        elif single_need > 1:
            single_batch_cnt = (single_need + 1) // 2
            for i in range(single_batch_cnt):
                doc_name = prioritized_docs[i % len(prioritized_docs)]
                win_idx = doc_window_tracker.get(doc_name, 0)
                doc_window_tracker[doc_name] = win_idx + 1
                cat1 = single_cat_pool[(i * 2) % len(single_cat_pool)]
                cat2 = single_cat_pool[(i * 2 + 1) % len(single_cat_pool)]
                planned.append({"type": "dual", "doc": doc_name, "cats": (cat1, cat2), "window": win_idx})

        for doc_a, doc_b in cross_pairs:
            planned.append({"type": "cross", "doc_a": doc_a, "doc_b": doc_b})
        return planned

    def _run_task(spec: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
        t = spec["type"]
        if t == "cross":
            label = f"{_shorten(spec['doc_a'], 18)} + {_shorten(spec['doc_b'], 18)} [跨文档]"
            return label, generate_cross_doc_questions(spec["doc_a"], spec["doc_b"], docs_dir, model_a=model_a)
        if t == "dual":
            label = f"{_shorten(spec['doc'], 34)} [双题]"
            return label, generate_questions_for_doc(
                spec["doc"],
                os.path.join(docs_dir, spec["doc"]),
                model_a=model_a,
                window_index=spec["window"],
                dual_categories=spec["cats"],
            )
        cat_label = CATEGORY_SHORT_NAMES.get(spec["cat"], spec["cat"])
        label = f"{_shorten(spec['doc'], 34)} [{cat_label}]"
        return label, generate_questions_for_doc(
            spec["doc"],
            os.path.join(docs_dir, spec["doc"]),
            model_a=model_a,
            target_category=spec["cat"],
            window_index=spec["window"],
        )

    global_completed = 0
    global_total_batches = 0
    success_batches = 0

    for round_no in range(1, 4):
        need_count = target_count - len(all_cases)
        if need_count <= 0:
            break
        is_initial_full = (round_no == 1 and initial_existing_count == 0)
        tasks = _plan_tasks(need_count, is_initial_full=is_initial_full)
        if not tasks:
            break

        global_total_batches += len(tasks)
        is_backfill = round_no > 1
        index_width = len(str(global_total_batches))
        if round_no == 1:
            print(
                f"  出题模型: {model_a} | 可用文档: {total_available_docs} 篇 | "
                f"请求批次: {len(tasks)} 次 (目标 {target_count} 题, 已有 {initial_existing_count} 题) | 并发: {effective_workers}"
            )
        else:
            print(f"  [第 {round_no - 1} 轮缺口自动补齐] 尚缺 {need_count} 题，追加 {len(tasks)} 批次...")

        _emit({
            "event": "meta",
            "model_a": model_a,
            "available_docs": total_available_docs,
            "total_batches": global_total_batches,
            "target_count": target_count,
            "existing_count": initial_existing_count,
            "workers": effective_workers,
            "round": round_no,
            "is_backfill": is_backfill,
        })

        round_added = 0
        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            future_to_task = {}
            for idx, spec in enumerate(tasks):
                future = executor.submit(_run_task, spec)
                future_to_task[future] = spec
                if idx < len(tasks) - 1:
                    time.sleep(0.12)

            for future in as_completed(future_to_task):
                global_completed += 1
                spec = future_to_task[future]
                prefix = f"  [{global_completed:>{index_width}}/{global_total_batches}]"
                label = str(spec.get("doc") or spec.get("doc_a") or "batch")
                added_cnt = 0
                try:
                    label, batch_cases = future.result()
                    for item in batch_cases:
                        q_norm = " ".join(item["query"].split()).lower()
                        if q_norm and q_norm not in seen_queries:
                            seen_queries.add(q_norm)
                            all_cases.append(item)
                            added_cnt += 1
                            round_added += 1
                            parts = [p.strip() for p in str(item.get("source_doc") or "").split("+") if p.strip()]
                            for p in parts:
                                covered_docs_so_far.add(p)
                            if len(parts) >= 2:
                                current_cross_cnt += 1
                                used_cross_pairs.add(tuple(sorted((parts[0], parts[1]))))
                    if added_cnt > 0:
                        success_batches += 1
                        print(f"{prefix} ✓ {label} (+{added_cnt}题)")
                    else:
                        print(f"{prefix} ! {label} 无有效题目")
                except Exception as e:
                    print(f"{prefix} ✗ {label} {_shorten(e, 60)}")

                _emit({
                    "event": "batch_done",
                    "round": round_no,
                    "completed": global_completed,
                    "total_batches": global_total_batches,
                    "generated_cases": min(len(all_cases), target_count),
                    "target_count": target_count,
                    "label": label,
                    "added": added_cnt,
                    "ok": added_cnt > 0,
                })

        if is_backfill and round_added == 0:
            break

    all_cases = all_cases[:target_count]
    for idx, c in enumerate(all_cases, start=1):
        c["id"] = idx

    covered_single = set()
    for c in all_cases:
        for part in str(c.get("source_doc") or "").split("+"):
            name = part.strip()
            if name:
                covered_single.add(name)
    elapsed = time.perf_counter() - started_at
    print(
        f"  生成结果: {success_batches}/{global_total_batches} 批成功 | {len(all_cases)} 题 | "
        f"覆盖 {len(covered_single)} 篇文档 | {elapsed:.1f}s"
    )
    return all_cases


# ==============================================================================
# 三、 人工审核 Excel 专业导出 (openpyxl 样式美化)
# ==============================================================================

def export_to_excel(cases: List[Dict[str, Any]], output_path: str) -> str:
    """
    【功能】将生成的测试题库导出为标准美观的 Excel 审查表格
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "基准测试用例待审表"

    # 表头定义
    headers = [
        "序号", "问题分类", "测试问题 (Query)", "黄金标准答案 (Ground Truth)",
        "来源切片依据 (Source Chunks)", "来源文档", "审核状态 [通过/修改/剔除]",
        "人工修正答案 (若需修改请在此填入)", "审核备注"
    ]
    ws.append(headers)

    # 样式配置
    header_font = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid") # 深海军蓝
    align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    align_left = Alignment(horizontal="left", vertical="center", wrap_text=True)
    
    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9")
    )

    pass_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid") # 淡绿

    # 设置表头样式
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = align_center
        cell.border = thin_border
    ws.row_dimensions[1].height = 28

    # 填充数据行
    for row_idx, c in enumerate(cases, start=2):
        ws.cell(row=row_idx, column=1, value=c.get("id", row_idx - 1))
        ws.cell(row=row_idx, column=2, value=c.get("category", ""))
        ws.cell(row=row_idx, column=3, value=c.get("query", ""))
        ws.cell(row=row_idx, column=4, value=c.get("ground_truth", ""))
        ws.cell(row=row_idx, column=5, value=c.get("source_chunks", ""))
        ws.cell(row=row_idx, column=6, value=c.get("source_doc", ""))
        
        status_cell = ws.cell(row=row_idx, column=7, value=c.get("review_status", "通过"))
        if status_cell.value == "通过":
            status_cell.fill = pass_fill

        ws.cell(row=row_idx, column=8, value=c.get("reviewed_ground_truth", ""))
        ws.cell(row=row_idx, column=9, value=c.get("review_notes", ""))

        # 对齐与边框
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.font = Font(name="微软雅黑", size=10)
            cell.border = thin_border
            if col_idx in [1, 2, 6, 7]:
                cell.alignment = align_center
            else:
                cell.alignment = align_left

        ws.row_dimensions[row_idx].height = 55

    # 设置自适应列宽
    col_widths = {
        1: 8,   # 序号
        2: 18,  # 分类
        3: 38,  # 问题
        4: 45,  # 标准答案
        5: 40,  # 来源依据
        6: 25,  # 来源文档
        7: 18,  # 审核状态
        8: 40,  # 修正答案
        9: 20   # 备注
    }
    for col_idx, width in col_widths.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    wb.save(output_path)

    # 备份一份 JSON
    json_path = output_path.replace(".xlsx", ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=2)

    return output_path


def save_cases_to_excel(cases: List[Dict[str, Any]], output_path: Optional[str] = None) -> str:
    """【供 Web 工作台使用】将前端用户编辑审核后的用例列表持久化回写至 Excel 表格"""
    target_path = output_path or os.path.join(BENCHMARK_DIR, "待审核基准测试集.xlsx")
    return export_to_excel(cases, target_path)


# ==============================================================================
# 四、 CLI 命令行入口
# ==============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agentic RAG 自动化测试集生成与人工审核导出工具")
    parser.add_argument("--mode", choices=["from-guide", "generate"], default="from-guide",
                        help="模式：from-guide (极速导入已精选的20道题，1秒完成) 或 generate (调用 glm-5.2 混合派生全库题库)")
    parser.add_argument("--model-a", default=ALI_DEFAULT_GENERATOR_MODEL, help="模型 A 命题专家模型名称 (默认 glm-5.2)")
    parser.add_argument("--count", type=int, default=20, help="生成题数 (例如 20 / 50 / 100)")
    parser.add_argument("--output", default=os.path.join(BENCHMARK_DIR, "待审核基准测试集.xlsx"),
                        help="导出的 Excel 文件路径")
    parser.add_argument("--workers", type=int, default=DEFAULT_GEN_WORKERS, help="并发请求线程数 (默认 8)")

    args = parser.parse_args()

    print(f"\n[出题] 模式 {args.mode} | 题数 {args.count} | 输出 {args.output}")

    if args.mode == "from-guide":
        print("  来源: 精选题库指南")
        cases = parse_guide_markdown(case_count=args.count)
    else:
        print(f"  模型 {args.model_a} | 并发 {args.workers}")
        cases = generate_benchmark_from_docs(
            docs_dir=DOCS_DIR,
            model_a=args.model_a,
            max_workers=args.workers,
            case_count=args.count,
        )

    if cases:
        out_file = export_to_excel(cases, args.output)
        print(f"\n[出题] 完成 | {len(cases)} 题")
        print(f"  输出: {out_file}")
        print("  下一步: 审核题库后开始评测")
    else:
        print("  ✗ 出题失败 | 未生成题目，请检查文档目录")
