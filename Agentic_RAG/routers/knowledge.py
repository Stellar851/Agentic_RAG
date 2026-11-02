"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
子路由模块：知识库管理 (routers/knowledge.py)

【模块定位】
负责知识库文档上传、图表预检、清洗模式确认/取消、切片查询及物理/向量同步删除。
================================================================================
"""

import os
import traceback
from typing import List
from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse

from vector_store import get_vector_store, UPLOAD_DIR
from schemas import ConfirmUploadRequest, CancelUploadRequest

router = APIRouter(prefix="/api", tags=["知识库管理"])

# 允许上传的文件格式白名单
ALLOWED_EXTENSIONS = {".docx", ".pdf", ".pptx", ".txt", ".md", ".json", ".csv"}


def is_allowed_file(filename: str) -> bool:
    """检查上传的文件扩展名是否在允许列表内"""
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_EXTENSIONS


def _format_upload_response(uploaded_results: List[dict], errors: List[str]) -> JSONResponse:
    """
    统一格式化文档入库/查重的 HTTP 响应报文，清晰区分新入库与已跳过重复数量
    """
    new_docs = [r for r in uploaded_results if not r.get("is_duplicate")]
    skipped_docs = [r for r in uploaded_results if r.get("is_duplicate")]

    new_count = len(new_docs)
    skipped_count = len(skipped_docs)

    if new_count > 0 and skipped_count > 0:
        msg = f"成功新入库 {new_count} 个文档，自动跳过 {skipped_count} 个重复文档"
    elif new_count > 0:
        msg = f"成功入库 {new_count} 个文档"
    elif skipped_count > 0:
        msg = f"上传的 {skipped_count} 个文档已在知识库中存在，已自动跳过重复入库（未产生冗余切片）"
    else:
        msg = "未入库任何文档"

    if errors:
        msg += f"，{len(errors)} 个处理失败"

    status_code = 200 if uploaded_results else 400
    return JSONResponse(
        status_code=status_code,
        content={
            "code": status_code,
            "status": "completed",
            "message": msg,
            "data": {
                "success": uploaded_results,
                "errors": errors,
                "new_count": new_count,
                "skipped_count": skipped_count,
                "new_docs": [d.get("doc_name") for d in new_docs],
                "skipped_docs": [d.get("doc_name") for d in skipped_docs]
            }
        }
    )


@router.post("/upload", summary="上传一个或多个文档并预检/入库")
async def upload_documents(
    files: List[UploadFile] = File(...),
    clean_mode: str = Form("auto")
):
    """
    【API】上传一个或多个文档文件：
    1. clean_mode:
       - "auto" (默认): 自动预检文档。若检测到复杂 ASCII 架构图，返回 needs_confirmation 提示用户选择；
                        若未达图表阈值，则自动以代码普通模式 (rule) 极速秒级入库，零弹窗骚扰；
       - "llm" / "rule": 用户或前端已明确指定的清洗模式，直接执行流水线入库。
    """
    if not files or files[0].filename == "":
        return JSONResponse(status_code=400, content={"code": 400, "message": "未选择任何有效文件"})

    clean_mode = clean_mode.strip().lower()
    if clean_mode not in ["auto", "llm", "rule"]:
        clean_mode = "auto"

    vs = get_vector_store()
    saved_files_info = []
    errors = []

    # 第一阶段：保存文件流并校验扩展名白名单
    for file in files:
        original_name = file.filename
        if not original_name:
            continue

        if not is_allowed_file(original_name):
            errors.append(f"文件【{original_name}】格式不支持，仅支持 .pptx, .docx, .pdf, .txt, .md, .json, .csv")
            continue

        try:
            safe_name = os.path.basename(original_name).replace("/", "").replace("\\", "")
            save_path = os.path.join(UPLOAD_DIR, safe_name)

            # 异步读取上传文件内容并写入磁盘
            content = await file.read()
            with open(save_path, "wb") as f_out:
                f_out.write(content)

            file_size_kb = round(os.path.getsize(save_path) / 1024, 2)
            saved_files_info.append({
                "original_name": original_name,
                "safe_name": safe_name,
                "save_path": save_path,
                "file_size_kb": file_size_kb
            })
        except Exception as e:
            traceback.print_exc()
            errors.append(f"保存文件【{original_name}】失败: {str(e)}")

    if not saved_files_info:
        return JSONResponse(
            status_code=400,
            content={
                "code": 400,
                "message": "未能成功保存任何有效文件",
                "data": {"errors": errors}
            }
        )

    # 阶段 1.5：物理指纹前置极速拦截已存在文档
    pending_new_files = []
    duplicate_results = []
    for item in saved_files_info:
        dup_info = vs.check_document_duplicate(item["save_path"], doc_name=item["safe_name"])
        if dup_info:
            print(f"⚡ [前置查重拦截] {dup_info['message']} (SHA-256: {dup_info['content_hash'][:8]}...)")
            duplicate_results.append(dup_info)
        else:
            pending_new_files.append(item)

    # 若所有上传的文件均为重复文档，直接闪电返回，零弹窗、零多余清洗！
    if not pending_new_files:
        print(f"⚡ [全部重复跳过] 本次上传的 {len(duplicate_results)} 个文档均已存在于知识库中，直接秒级返回！")
        return _format_upload_response(duplicate_results, errors)

    # 第二阶段：如果为 auto 模式，自动扫描待入库新文件是否触发复杂架构图阈值
    if clean_mode == "auto":
        detected_files = []
        for item in pending_new_files:
            try:
                diag_info = vs.inspect_document(item["save_path"])
                if diag_info.get("is_real_diagram"):
                    detected_files.append({
                        "filename": item["safe_name"],
                        "file_size_kb": item["file_size_kb"],
                        "corner_count": diag_info.get("corner_count", 0),
                        "arrow_count": diag_info.get("arrow_count", 0),
                        "total_box_chars": diag_info.get("total_box_chars", 0)
                    })
            except Exception as e:
                print(f"⚠️ [文档预检异常] 预检【{item['safe_name']}】失败: {e}")

        # 如果检测到复杂架构图，返回 needs_confirmation 状态，由前端弹窗让用户针对性选择
        if detected_files:
            print(f"\n🧩 [触发架构图阈值] 本次待入库的 {len(pending_new_files)} 个新文件中，有 {len(detected_files)} 个包含复杂字符架构框图！")
            for df in detected_files:
                print(f"   - 【{df['filename']}】(闭合拐角: {df['corner_count']}, 流程箭头: {df['arrow_count']}, 拓扑符号: {df['total_box_chars']})")
            print("   ⏳ 正在等待前端用户选择清洗模式 (大模型升维清洗 或 代码普通清洗)...")
            return {
                "code": 200,
                "status": "needs_confirmation",
                "message": "检测到文档包含复杂字符架构图/流程图，请选择清洗方式",
                "detected_files": detected_files,
                "all_files": [item["safe_name"] for item in pending_new_files],
                "skipped_files": [d["doc_name"] for d in duplicate_results]
            }

        # 未检测到复杂架构图，自动直接以代码普通模式 (rule) 极速入库
        print(f"\nℹ️ [自动入库] 本批待处理的 {len(pending_new_files)} 个新文件均未达到架构图阈值，自动采用【代码普通清洗 (0 Token)】极速入库！")
        clean_mode = "rule"

    # 第三阶段：执行实际入库流程 (针对 clean_mode 为 rule 或 llm)
    uploaded_results = list(duplicate_results)
    for item in pending_new_files:
        try:
            mode_text = "大模型智能升维清洗 (Mermaid/Markdown)" if clean_mode == "llm" else "代码规则普通清洗 (正则降噪)"
            print(f"\n📥 [处理入库文件] 文件名: 【{item['safe_name']}】 | 大小: {item['file_size_kb']} KB | 清洗模式: 【{mode_text}】")
            stats = vs.process_and_add_document(item["save_path"], clean_mode=clean_mode)
            uploaded_results.append(stats)
            if stats.get("is_duplicate"):
                print(f"💡 [查重处理结果] {stats.get('message')}")
        except Exception as e:
            traceback.print_exc()
            print(f"❌ [入库处理异常] 文件【{item['safe_name']}】处理失败: {e}")
            errors.append(f"文件【{item['safe_name']}】处理失败: {str(e)}")

    return _format_upload_response(uploaded_results, errors)


@router.post("/upload/confirm", summary="用户确认清洗模式后执行向量化入库")
async def confirm_upload(req: ConfirmUploadRequest):
    """
    【API】用户在弹窗确认了清洗模式后，对已保存的文档执行向量化入库
    """
    try:
        filenames = req.filenames or []
        clean_mode = (req.clean_mode or "rule").strip().lower()
        if clean_mode not in ["llm", "rule"]:
            clean_mode = "rule"

        if not filenames:
            return JSONResponse(status_code=400, content={"code": 400, "message": "未提供待入库的文件名列表"})

        vs = get_vector_store()
        uploaded_results = []
        errors = []

        mode_text = "大模型智能升维清洗 (Mermaid/Markdown)" if clean_mode == "llm" else "代码规则普通清洗 (正则降噪)"
        print(f"\n🎯 [用户确认清洗模式] 选择模式: 【{mode_text}】 | 文件数量: {len(filenames)}")

        for filename in filenames:
            safe_name = os.path.basename(filename).replace("/", "").replace("\\", "")
            save_path = os.path.join(UPLOAD_DIR, safe_name)
            if not os.path.exists(save_path):
                errors.append(f"文件【{safe_name}】在服务器缓存中未找到")
                continue

            try:
                stats = vs.process_and_add_document(save_path, clean_mode=clean_mode)
                uploaded_results.append(stats)
                if stats.get("is_duplicate"):
                    print(f"💡 [查重处理结果] {stats.get('message')}")
            except Exception as e:
                traceback.print_exc()
                errors.append(f"文件【{safe_name}】处理失败: {str(e)}")

        return _format_upload_response(uploaded_results, errors)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"code": 500, "message": f"确认入库处理失败: {str(e)}"})


@router.post("/upload/cancel", summary="用户取消上传时清理暂存文件")
async def cancel_upload(req: CancelUploadRequest):
    """
    【API】用户取消上传时，清理未入库的暂存文件
    """
    try:
        filenames = req.filenames or []
        for filename in filenames:
            safe_name = os.path.basename(filename).replace("/", "").replace("\\", "")
            save_path = os.path.join(UPLOAD_DIR, safe_name)
            if os.path.exists(save_path):
                try:
                    os.remove(save_path)
                except Exception:
                    pass
        return {"code": 200, "message": "上传已取消，暂存文件已清理"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": str(e)})


@router.get("/documents", summary="获取知识库中全部已存储文档清单")
async def get_documents_list():
    """
    【API】获取知识库中全部已存储的文档清单及基本统计
    """
    try:
        vs = get_vector_store()
        docs = vs.list_all_documents()
        return {
            "code": 200,
            "data": docs
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": f"获取文档列表失败: {str(e)}"})


@router.get("/documents/{doc_name:path}/chunks", summary="获取指定文档的切片数据")
async def get_document_chunks(doc_name: str):
    """
    【API】获取指定文档的所有切片文本、元数据及清洗模式，供前端可视化检查与校验
    """
    try:
        vs = get_vector_store()
        res = vs.collection.get(
            where={"doc_name": doc_name},
            include=["documents", "metadatas"]
        )
        if not res or not res.get("documents"):
            return JSONResponse(status_code=404, content={"code": 404, "message": f"未找到文档【{doc_name}】的切片数据"})

        chunks = []
        for text, meta in zip(res["documents"], res["metadatas"]):
            chunks.append({
                "chunk_index": meta.get("chunk_index", 0),
                "total_chunks": meta.get("total_chunks", 0),
                "upload_time": meta.get("upload_time", ""),
                "clean_mode": meta.get("clean_mode", "rule"),
                "content_hash": meta.get("content_hash", ""),
                "char_count": len(text),
                "text": text
            })

        chunks.sort(key=lambda x: x["chunk_index"])
        total_chars = sum(c["char_count"] for c in chunks)
        clean_mode = chunks[0]["clean_mode"] if chunks else "rule"

        return {
            "code": 200,
            "data": {
                "doc_name": doc_name,
                "total_chunks": len(chunks),
                "total_chars": total_chars,
                "clean_mode": clean_mode,
                "content_hash": chunks[0].get("content_hash", "") if chunks else "",
                "chunks": chunks
            }
        }
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"code": 500, "message": f"获取文档切片失败: {str(e)}"})


@router.delete("/documents/{doc_name:path}", summary="删除指定文档")
async def delete_document(doc_name: str):
    """
    【API】删除指定文档：
    同步移除 ChromaDB 中的所有对应切片以及 uploads 目录下的原文件
    """
    try:
        print(f"\n🗑️ [API 请求删除文档] 正在清空知识库文档: 【{doc_name}】...")
        vs = get_vector_store()
        success = vs.delete_document(doc_name=doc_name, delete_file=True)
        if success:
            return {"code": 200, "message": f"文档【{doc_name}】已成功移除"}
        else:
            return JSONResponse(status_code=400, content={"code": 400, "message": f"删除文档【{doc_name}】失败"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"code": 500, "message": f"删除文档发生异常: {str(e)}"})

