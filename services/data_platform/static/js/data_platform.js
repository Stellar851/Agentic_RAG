/**
 * 数据中台控制台交互脚本 (data_platform.js)
 */

document.addEventListener("DOMContentLoaded", () => {
    loadHealthAndStats();
    loadDocuments();
    setupDropzone();
});

// 加载健康状态与宏观指标
async function loadHealthAndStats() {
    try {
        const resp = await fetch("/api/v1/data/health");
        if (resp.ok) {
            const result = await resp.json();
            const data = result.data || {};
            document.getElementById("stat-total-docs").innerText = data.total_documents || 0;
            document.getElementById("stat-total-chunks").innerText = data.total_chunks || 0;
        }
    } catch (e) {
        console.error("加载健康统计失败:", e);
    }
}

// 加载知识库文档列表
async function loadDocuments() {
    const tableBody = document.getElementById("docs-table-body");
    tableBody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding:30px; color:#64748b;">正在加载知识库资产清单...</td></tr>`;

    try {
        const resp = await fetch("/api/v1/data/documents");
        if (!resp.ok) throw new Error("获取文档列表失败");
        const result = await resp.json();
        const docs = result.data?.documents || [];

        document.getElementById("stat-total-docs").innerText = docs.length;
        const totalChunks = docs.reduce((acc, d) => acc + (d.chunk_count || 0), 0);
        document.getElementById("stat-total-chunks").innerText = totalChunks;

        if (docs.length === 0) {
            tableBody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding:40px; color:#64748b;">知识库暂无文档，请在左侧拖拽上传新业务文件。</td></tr>`;
            return;
        }

        let html = "";
        docs.forEach(doc => {
            const docName = doc.doc_name || "未命名文档";
            const chunkCount = doc.chunk_count || 0;
            const fileSize = doc.file_size_formatted || "--";
            const createdAt = doc.created_at || "--";

            html += `
                <tr>
                    <td class="doc-name-cell">
                        <svg class="doc-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                            <polyline points="14 2 14 8 20 8"></polyline>
                        </svg>
                        <span>${escapeHtml(docName)}</span>
                    </td>
                    <td>
                        <span class="badge-chunks">${chunkCount} 切片</span>
                    </td>
                    <td style="color: #94a3b8; font-size: 0.8rem;">${fileSize}</td>
                    <td style="color: #64748b; font-size: 0.8rem;">${createdAt}</td>
                    <td>
                        <div class="action-btns">
                            <button class="btn-action" onclick="previewChunks('${escapeAttr(docName)}')">
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                    <circle cx="11" cy="11" r="8"></circle>
                                    <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                                </svg>
                                穿透查看
                            </button>
                            <button class="btn-action btn-delete" onclick="deleteDocument('${escapeAttr(docName)}')">
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                    <polyline points="3 6 5 6 21 6"></polyline>
                                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                                </svg>
                                删除
                            </button>
                        </div>
                    </td>
                </tr>
            `;
        });
        tableBody.innerHTML = html;

    } catch (e) {
        tableBody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding:30px; color:#f43f5e;">加载失败: ${e.message}</td></tr>`;
    }
}

// 设置文件拖拽上传
function setupDropzone() {
    const dropzone = document.getElementById("dropzone");
    const fileInput = document.getElementById("file-input");
    const statusBox = document.getElementById("upload-status");

    dropzone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropzone.classList.add("dragover");
    });

    dropzone.addEventListener("dragleave", () => {
        dropzone.classList.remove("dragover");
    });

    dropzone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropzone.classList.remove("dragover");
        if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            handleUploadFiles(e.dataTransfer.files);
        }
    });

    fileInput.addEventListener("change", (e) => {
        if (e.target.files && e.target.files.length > 0) {
            handleUploadFiles(e.target.files);
        }
    });
}

// 批量上传文档
async function handleUploadFiles(files) {
    const statusBox = document.getElementById("upload-status");
    statusBox.style.display = "block";
    statusBox.style.color = "#c7d2fe";

    for (let i = 0; i < files.length; i++) {
        const file = files[i];
        statusBox.innerText = `正在上传并向量化解析 (${i + 1}/${files.length}): ${file.name}...`;

        const formData = new FormData();
        formData.append("file", file);
        formData.append("clean_mode", "auto");

        try {
            const resp = await fetch("/api/v1/data/upload", {
                method: "POST",
                body: formData
            });
            const res = await resp.json();
            if (!resp.ok) {
                alert(`文件 ${file.name} 上传失败: ${res.detail || res.message}`);
            }
        } catch (e) {
            alert(`文件 ${file.name} 上传网络异常: ${e.message}`);
        }
    }

    statusBox.innerText = `✅ 所有文档上传与向量入库处理完成！`;
    setTimeout(() => {
        statusBox.style.display = "none";
    }, 3500);

    // 重新刷新列表和宏观指标
    loadDocuments();
    loadHealthAndStats();
}

// 穿透预览切片详情
async function previewChunks(docName) {
    const modal = document.getElementById("chunk-modal");
    const modalTitle = document.getElementById("modal-doc-title");
    const modalBody = document.getElementById("modal-chunks-container");

    modalTitle.innerText = `文档切片穿透: 《${docName}》`;
    modalBody.innerHTML = `<p style="color:#94a3b8; text-align:center; padding:30px;">正在读取中台切片与向量索引...</p>`;
    modal.style.display = "flex";

    try {
        const resp = await fetch(`/api/v1/data/document-chunks?doc_name=${encodeURIComponent(docName)}&max_chunks=8`);
        if (!resp.ok) throw new Error("获取切片失败");
        const res = await resp.json();
        const chunks = res.data?.chunks || [];

        if (chunks.length === 0) {
            modalBody.innerHTML = `<p style="color:#94a3b8; text-align:center; padding:30px;">未查找到切片。</p>`;
            return;
        }

        let html = "";
        chunks.forEach(c => {
            html += `
                <div class="chunk-card">
                    <div class="chunk-header">
                        <span><strong>切片 #${(c.chunk_index || 0) + 1}</strong> / 总共 ${c.total_chunks || 1} 块</span>
                        <span>字数: ${(c.text || "").length} 字符</span>
                    </div>
                    <div class="chunk-text">${escapeHtml(c.text || "")}</div>
                </div>
            `;
        });
        modalBody.innerHTML = html;
    } catch (e) {
        modalBody.innerHTML = `<p style="color:#f43f5e; text-align:center; padding:30px;">读取切片失败: ${e.message}</p>`;
    }
}

function closeModal() {
    document.getElementById("chunk-modal").style.display = "none";
}

// 删除指定文档
async function deleteDocument(docName) {
    if (!confirm(`确定要从数据中台彻底下架并删除《${docName}》吗？\n该操作将同时清理磁盘文件与 ChromaDB 向量集合。`)) {
        return;
    }

    try {
        const resp = await fetch(`/api/v1/data/documents/${encodeURIComponent(docName)}`, {
            method: "DELETE"
        });
        const res = await resp.json();
        if (resp.ok) {
            alert(res.message || "删除成功");
            loadDocuments();
            loadHealthAndStats();
        } else {
            alert("删除失败: " + (res.message || res.detail));
        }
    } catch (e) {
        alert("删除请求异常: " + e.message);
    }
}

// 辅助转义函数
function escapeHtml(str) {
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function escapeAttr(str) {
    return String(str).replace(/'/g, "\\'").replace(/"/g, "&quot;");
}
