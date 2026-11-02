"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
自动化测试集生成与人工审核导出模块 (dataset_generator.py)

【模块核心功能】
1. 模型 A (qwen3.8-max) 驱动：基于知识库文本切片，利用批量派生 Prompt + 多并发线程池，
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
import logging
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional

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

from models import get_deepseek_llm

logger = logging.getLogger(__name__)

BENCHMARK_DIR = os.path.join(CURRENT_DIR, "benchmarks")
os.makedirs(BENCHMARK_DIR, exist_ok=True)
# 路径配置：默认出题严格基于知识库实际已入库的文档目录 (Agentic_RAG/uploads)
UPLOAD_DOCS_DIR = os.path.join(CURRENT_DIR, "uploads")
FALLBACK_DOCS_DIR = os.path.join(PROJECT_ROOT, "知识库测试数据集")
DOCS_DIR = UPLOAD_DOCS_DIR if os.path.exists(UPLOAD_DOCS_DIR) else FALLBACK_DOCS_DIR


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
# 二、 模型 A (qwen3.8-max) 批量自动化派生题目
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

def generate_questions_for_doc(
    doc_name: str,
    doc_path: str,
    model_a: str = "qwen3.8-max",
    target_category: Optional[str] = None
) -> List[Dict[str, Any]]:
    """调用模型 A 对单个文档片段派生考题（支持指定单一专项题型以实现全库广度覆盖）"""
    try:
        content = ""
        ext = os.path.splitext(doc_name)[1].lower()
        if ext in [".md", ".txt"]:
            with open(doc_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        else:
            try:
                from vector_store import DocumentParser
                content = DocumentParser.extract_raw_text(doc_path)
            except Exception:
                with open(doc_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()

        if not content.strip():
            return []

        # 截取最具价值的业务核心章节
        trimmed_content = content[:3500]
        llm = get_deepseek_llm(model=model_a, temperature=0.3, streaming=False)

        if target_category and target_category in CATEGORY_REQUIREMENTS:
            prompt = PROMPT_GENERATE_TARGET_CASE.format(
                doc_name=doc_name,
                doc_content=trimmed_content,
                target_category=target_category,
                category_requirement=CATEGORY_REQUIREMENTS[target_category]
            )
        else:
            prompt = PROMPT_GENERATE_CASES.format(doc_name=doc_name, doc_content=trimmed_content)

        resp = llm.invoke(prompt)
        text = resp.content.strip()

        # 清洗可能存在的 Markdown 标签
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        cases = json.loads(text)
        if isinstance(cases, dict):
            cases = [cases]

        formatted = []
        for c in cases:
            formatted.append({
                "category": c.get("category", target_category or "综合业务推理"),
                "query": c.get("query", ""),
                "ground_truth": c.get("ground_truth", ""),
                "source_chunks": c.get("source_chunks", ""),
                "source_doc": doc_name,
                "review_status": "待确认",
                "reviewed_ground_truth": "",
                "review_notes": ""
            })
        return formatted
    except Exception as e:
        logger.debug("生成文档 %s 的题目失败", doc_name, exc_info=True)
        return []


def generate_benchmark_from_docs(
    docs_dir: str = DOCS_DIR,
    model_a: str = "qwen3.8-max",
    max_workers: int = 4,
    case_count: int = 10
) -> List[Dict[str, Any]]:
    """
    【广度优先·全库均衡出题】根据目标 case_count，将考题均匀分散到全库不同文档中（实现一文一题，全库覆盖）
    """
    if not os.path.exists(docs_dir):
        print(f"  ✗ 知识库目录不存在 | {docs_dir}")
        return []

    # 过滤出知识库业务文档（同时支持 .md 与 .txt 业务文档，排除非业务说明文档）
    doc_files = sorted(
        f for f in os.listdir(docs_dir)
        if (f.endswith(".md") or f.endswith(".txt") or f.endswith(".docx") or f.endswith(".pdf"))
        and not f.startswith("08_") and not f.startswith("README")
    )

    total_available_docs = len(doc_files)
    if total_available_docs == 0:
        print("  ✗ 未发现可用业务文档")
        return []

    target_count = max(1, case_count) if case_count else 10

    # 核心调度算法：广度优先文档轮询调度
    # 若目标题数 <= 总文档数，则直接选取 target_count 篇完全不同的文档（100% 覆盖不同文档）
    # 若目标题数 > 总文档数，则循环轮转分配，确保所有文档至少被覆盖一次
    tasks = []
    for i in range(target_count):
        doc_idx = i % total_available_docs
        assigned_doc = doc_files[doc_idx]
        assigned_cat = CATEGORIES_CYCLE[i % len(CATEGORIES_CYCLE)]
        tasks.append((assigned_doc, assigned_cat))

    unique_docs_count = len(set(t[0] for t in tasks))
    total_tasks = len(tasks)
    index_width = len(str(total_tasks))
    started_at = time.perf_counter()

    print(f"  文档扫描: {total_available_docs} 篇可用 | 本轮覆盖 {unique_docs_count} 篇 | 并发 {max_workers}")

    all_cases = []
    success_docs = 0
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {}
        for doc_name, target_cat in tasks:
            future = executor.submit(
                generate_questions_for_doc,
                doc_name,
                os.path.join(docs_dir, doc_name),
                model_a,
                target_cat
            )
            future_to_task[future] = (doc_name, target_cat)

        for future in as_completed(future_to_task):
            completed += 1
            doc_name, target_cat = future_to_task[future]
            category_label = CATEGORY_SHORT_NAMES.get(target_cat, target_cat)
            prefix = f"  [{completed:>{index_width}}/{total_tasks}]"
            try:
                doc_cases = future.result()
                if doc_cases:
                    all_cases.extend(doc_cases)
                    success_docs += 1
                    print(f"{prefix} ✓ {_shorten(doc_name)} [{category_label}]")
                else:
                    print(f"{prefix} ! {_shorten(doc_name)} [{category_label}] 无有效题目")
            except Exception as e:
                print(f"{prefix} ✗ {_shorten(doc_name)} [{category_label}] {_shorten(e, 60)}")

    # 精准截取与全局连续编号
    all_cases = all_cases[:target_count]
    for idx, c in enumerate(all_cases, start=1):
        c["id"] = idx

    covered_docs = len(set(c["source_doc"] for c in all_cases))
    elapsed = time.perf_counter() - started_at
    print(f"  生成结果: {success_docs}/{total_tasks} 篇成功 | {len(all_cases)} 题 | 覆盖 {covered_docs} 篇 | {elapsed:.1f}s")
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
                        help="模式：from-guide (极速导入已精选的20道题，1秒完成) 或 generate (调用 qwen3.8-max 重新从文档派生)")
    parser.add_argument("--model-a", default="qwen3.8-max", help="模型 A 命题专家模型名称")
    parser.add_argument("--output", default=os.path.join(BENCHMARK_DIR, "待审核基准测试集.xlsx"),
                        help="导出的 Excel 文件路径")
    parser.add_argument("--workers", type=int, default=3, help="并发请求线程数")

    args = parser.parse_args()

    print(f"\n[出题] 模式 {args.mode} | 输出 {args.output}")

    if args.mode == "from-guide":
        print("  来源: 精选题库指南")
        cases = parse_guide_markdown()
    else:
        print(f"  模型 {args.model_a} | 并发 {args.workers}")
        cases = generate_benchmark_from_docs(docs_dir=DOCS_DIR, model_a=args.model_a, max_workers=args.workers)

    if cases:
        out_file = export_to_excel(cases, args.output)
        print(f"\n[出题] 完成 | {len(cases)} 题")
        print(f"  输出: {out_file}")
        print("  下一步: 审核题库后开始评测")
    else:
        print("  ✗ 出题失败 | 未生成题目，请检查文档目录")
