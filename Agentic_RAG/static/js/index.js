let currentSessionId = null;
        let sessionsCache = [];

        const dropzone = document.getElementById('dropzone');
        const fileInput = document.getElementById('file-input');
        const uploadProgress = document.getElementById('upload-progress');
        const uploadProgressText = document.getElementById('upload-progress-text');
        const docsList = document.getElementById('docs-list');
        const docsTabCount = document.getElementById('docs-tab-count');
        const totalChunksSpan = document.getElementById('total-chunks-badge');
        const sessionsList = document.getElementById('sessions-list');
        const sessionsCount = document.getElementById('sessions-count');
        const messagesContainer = document.getElementById('messages-container');
        const userInput = document.getElementById('user-input');
        const btnSend = document.getElementById('btn-send');
        const btnStop = document.getElementById('btn-stop');
        const welcomeHero = document.getElementById('welcome-hero');
        const btnNewChat = document.getElementById('btn-new-chat');

        // 当前正在执行的流式请求控制器
        let currentAbortController = null;
        let currentActiveStreamReader = null;

        // 页面初始化（支持 DOMContentLoaded 与 readyState 立即就绪双通道触发）
        function initApp() {
            loadSupportedModels();
            checkSystemStatus();
            loadSessions(true);
            setupEventListeners();
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', initApp);
        } else {
            initApp();
        }

        function setupEventListeners() {
            // 新建会话
            btnNewChat.addEventListener('click', () => createNewChat());

            // 终止回答按钮
            if (btnStop) {
                btnStop.addEventListener('click', stopCurrentGeneration);
            }

            // 拖拽上传 (若存在相关 DOM)
            if (dropzone && fileInput) {
                dropzone.addEventListener('click', () => fileInput.click());
                dropzone.addEventListener('dragover', (e) => {
                    e.preventDefault();
                    dropzone.classList.add('dragover');
                });
                dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
                dropzone.addEventListener('drop', (e) => {
                    e.preventDefault();
                    dropzone.classList.remove('dragover');
                    if (e.dataTransfer.files.length > 0) {
                        handleUpload(e.dataTransfer.files);
                    }
                });

                fileInput.addEventListener('change', () => {
                    if (fileInput.files.length > 0) {
                        handleUpload(fileInput.files);
                    }
                });
            }

            // 输入框事件
            userInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    sendMessage();
                }
            });

            userInput.addEventListener('input', () => {
                userInput.style.height = 'auto';
                userInput.style.height = Math.min(userInput.scrollHeight, 160) + 'px';
            });

            btnSend.addEventListener('click', sendMessage);
        }

        // 侧边栏选项卡切换 (会话记录 vs 文档库)
        function switchSidebarTab(tabName) {
            const btnSessions = document.getElementById('tab-btn-sessions');
            const btnDocs = document.getElementById('tab-btn-docs');
            const panelSessions = document.getElementById('panel-sessions');
            const panelDocs = document.getElementById('panel-docs');

            btnSessions.classList.remove('active');
            btnDocs.classList.remove('active');
            panelSessions.classList.remove('active');
            panelDocs.classList.remove('active');

            if (tabName === 'sessions') {
                btnSessions.classList.add('active');
                panelSessions.classList.add('active');
            } else if (tabName === 'docs') {
                btnDocs.classList.add('active');
                panelDocs.classList.add('active');
            }
        }

        // 全局支持的模型缓存
        let cachedSupportedModels = [
            { model_name: 'qwen3.7-plus', short_name: '3.7 Plus', name: '通义千问 3.7 Plus', desc: '全方位主力 · 极速流利回答', is_configured: true },
            { model_name: 'qwen3.8-max-0902', short_name: '3.8 Max', name: '通义千问 3.8 Max', desc: '顶级逻辑旗舰 · 深度多跳推理', is_configured: true },
            { model_name: 'deepseek-v4-pro-0813', short_name: 'DS Pro', name: 'DeepSeek V4 Pro', desc: '高智商逻辑旗舰 · 深度推理作答', is_configured: true }
        ];

        // 0. 加载支持的大模型列表并驱动 Google Gemini 风格胶囊弹窗
        async function loadSupportedModels() {
            setupGeminiDropdownListeners();

            try {
                const res = await fetch('/api/models');
                const data = await res.json();
                if (data.code === 200 && data.data && data.data.models) {
                    cachedSupportedModels = data.data.models;
                }
            } catch (err) {
                console.warn("加载模型列表失败，使用本地预设配置", err);
            }

            // 读取之前用户记忆的模型
            const savedModel = localStorage.getItem('agentic_rag_selected_model');
            let initialModel = cachedSupportedModels[0].model_name;

            if (savedModel && cachedSupportedModels.some(m => m.model_name === savedModel || m.model_name.toLowerCase() === savedModel.toLowerCase())) {
                const matched = cachedSupportedModels.find(m => m.model_name === savedModel || m.model_name.toLowerCase() === savedModel.toLowerCase());
                if (matched) initialModel = matched.model_name;
            }

            // 应用初始选中模型
            applyGeminiSelectedModel(initialModel, false);
        }

        // 设置 Gemini 胶囊下拉框的开闭事件与点击外部关闭
        function setupGeminiDropdownListeners() {
            const wrapper = document.getElementById('gemini-dropdown-wrapper');
            const btn = document.getElementById('gemini-pill-btn');
            if (!wrapper || !btn) return;

            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const isOpen = wrapper.classList.contains('open');
                if (isOpen) {
                    closeGeminiDropdown();
                } else {
                    openGeminiDropdown();
                }
            });

            // 点击外部区域自动收起弹窗
            document.addEventListener('click', (e) => {
                if (wrapper.classList.contains('open') && !wrapper.contains(e.target)) {
                    closeGeminiDropdown();
                }
            });

            // ESC 键收起
            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape' && wrapper.classList.contains('open')) {
                    closeGeminiDropdown();
                }
            });
        }

        function openGeminiDropdown() {
            const wrapper = document.getElementById('gemini-dropdown-wrapper');
            const btn = document.getElementById('gemini-pill-btn');
            if (!wrapper || !btn) return;
            renderGeminiMenuItems();
            wrapper.classList.add('open');
            btn.setAttribute('aria-expanded', 'true');
        }

        function closeGeminiDropdown() {
            const wrapper = document.getElementById('gemini-dropdown-wrapper');
            const btn = document.getElementById('gemini-pill-btn');
            if (!wrapper || !btn) return;
            wrapper.classList.remove('open');
            btn.setAttribute('aria-expanded', 'false');
        }

        // 动态渲染 Gemini 卡片菜单项（精确还原 Google Gemini 列表布局）
        function renderGeminiMenuItems() {
            const container = document.getElementById('gemini-model-list-container');
            const hiddenInput = document.getElementById('model-select');
            if (!container) return;

            const currentVal = hiddenInput ? hiddenInput.value : '';

            container.innerHTML = cachedSupportedModels.map(m => {
                const isActive = (m.model_name === currentVal);
                const checkSvg = `
                    <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2.5" fill="none">
                        <polyline points="20 6 9 17 4 12"></polyline>
                    </svg>
                `;

                return `
                    <div class="gemini-menu-item" onclick="selectGeminiModel('${escapeHtml(m.model_name)}')">
                        <div class="gemini-item-check">
                            ${isActive ? checkSvg : ''}
                        </div>
                        <div class="gemini-item-content">
                            <div class="gemini-item-title-row">
                                <div class="gemini-item-title">${escapeHtml(m.short_name || m.name)}</div>
                                ${!m.is_configured ? '<span class="gemini-item-tag" style="background:#fee2e2; color:#ef4444;">未配Key</span>' : ''}
                            </div>
                            <div class="gemini-item-desc">${escapeHtml(m.desc || m.name)}</div>
                        </div>
                    </div>
                `;
            }).join('');
        }

        // 选中某个模型
        function selectGeminiModel(modelName) {
            applyGeminiSelectedModel(modelName, true);
            closeGeminiDropdown();
        }

        function applyGeminiSelectedModel(modelName, saveToStorage = true) {
            const hiddenInput = document.getElementById('model-select');
            const titleSpan = document.getElementById('gemini-pill-current-name');

            if (hiddenInput) {
                hiddenInput.value = modelName;
            }

            const modelObj = cachedSupportedModels.find(m => m.model_name === modelName) || {
                short_name: modelName,
                name: modelName
            };

            if (titleSpan) {
                titleSpan.textContent = modelObj.short_name || modelObj.name;
            }

            if (saveToStorage) {
                localStorage.setItem('agentic_rag_selected_model', modelName);
                console.log(`✨ [Gemini Hot-Swap] 模型已热切换为: ${modelName} (${modelObj.name})`);
            }
        }

        // 1. 系统状态自检
        async function checkSystemStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                if (data.status === 'success') {
                    const ds = data.data.deepseek;
                    const dsDot = document.getElementById('deepseek-dot');
                    if (dsDot) {
                        if (ds.connected) {
                            dsDot.className = 'status-dot online';
                            dsDot.title = `主大模型服务就绪: ${ds.provider} (${ds.model_name})`;
                        } else {
                            dsDot.className = 'status-dot offline';
                            dsDot.title = ds.message || '大模型密钥未配置';
                        }
                    }

                    const ol = data.data.ollama;
                    const olDot = document.getElementById('ollama-dot');
                    const olText = document.getElementById('ollama-text');
                    if (ol && olDot && olText) {
                        if (ol.connected) {
                            olDot.className = 'status-dot online';
                            olText.textContent = '正常就绪';
                            olText.title = ol.message;
                        } else {
                            olDot.className = 'status-dot offline';
                            olText.textContent = '未连接';
                            olText.title = ol.message;
                        }
                    }
                }
            } catch (err) {
                console.error("检测系统状态失败", err);
            }
        }

        // 2. 加载会话列表
        async function loadSessions(selectFirstIfAvailable = false) {
            try {
                const res = await fetch('/api/sessions');
                const result = await res.json();
                if (result.code === 200) {
                    sessionsCache = result.data || [];
                    if (sessionsCount) { sessionsCount.textContent = sessionsCache.length; }
                    renderSessions();

                    if (selectFirstIfAvailable && sessionsCache.length > 0) {
                        selectSession(sessionsCache[0].session_id);
                    } else if (sessionsCache.length === 0) {
                        createNewChat(false);
                    }
                }
            } catch (err) {
                console.error("加载会话列表失败", err);
            }
        }

        function renderSessions() {
            if (sessionsCache.length === 0) {
                sessionsList.innerHTML = `
                    <div style="text-align: center; color: var(--text-muted); font-size: 0.8rem; padding: 24px 0;">
                        暂无历史对话<br>点击上方新建对话
                    </div>
                `;
                return;
            }

            sessionsList.innerHTML = sessionsCache.map(s => `
                <div class="session-item ${s.session_id === currentSessionId ? 'active' : ''}" 
                     onclick="selectSession('${s.session_id}')">
                    <div class="session-info">
                        <div class="session-title" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</div>
                        <div class="session-time">${s.updated_at ? s.updated_at.substring(5, 16) : ''}</div>
                    </div>
                    <button class="btn-delete-session" onclick="deleteSession('${s.session_id}', event)" title="删除此会话">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                    </button>
                </div>
            `).join('');
        }

        // 选择/切换某个会话
        async function selectSession(sessionId) {
            currentSessionId = sessionId;
            renderSessions();

            // 清空当前消息展示区
            messagesContainer.innerHTML = '';
            if (welcomeHero) welcomeHero.style.display = 'none';

            try {
                const res = await fetch(`/api/sessions/${sessionId}`);
                const result = await res.json();
                if (result.code === 200) {
                    const messages = result.data.messages || [];
                    if (messages.length === 0) {
                        if (welcomeHero) messagesContainer.appendChild(welcomeHero);
                        if (welcomeHero) welcomeHero.style.display = 'block';
                    } else {
                        for (const msg of messages) {
                            if (msg.role === 'user') {
                                appendUserMessage(msg.content);
                            } else {
                                appendAIMessage(msg.content, msg.steps, msg.referenced_docs);
                            }
                        }
                    }
                }
            } catch (err) {
                console.error("加载会话详情异常", err);
            }
        }

        // 新建会话
        async function createNewChat(autoFocus = true) {
            currentSessionId = null;
            messagesContainer.innerHTML = '';
            if (welcomeHero) {
                messagesContainer.appendChild(welcomeHero);
                welcomeHero.style.display = 'block';
            }
            renderSessions();
            if (autoFocus) userInput.focus();
        }

        // 删除会话
        async function deleteSession(sessionId, e) {
            e.stopPropagation();
            if (!confirm("确定要删除此条对话记录吗？")) return;

            try {
                const res = await fetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
                const result = await res.json();
                if (result.code === 200) {
                    if (currentSessionId === sessionId) {
                        currentSessionId = null;
                    }
                    loadSessions(currentSessionId === null);
                }
            } catch (err) {
                alert("删除会话失败: " + err.message);
            }
        }

        // 3. 文档库管理
        async function fetchDocuments() {
            if (!docsList) return;
            try {
                const res = await fetch('/api/documents');
                const result = await res.json();
                if (result.code === 200) {
                    renderDocuments(result.data);
                }
            } catch (err) {
                console.error("获取文档失败", err);
            }
        }

        function renderDocuments(docs) {
            docsTabCount.textContent = docs.length;
            const totalChunks = docs.reduce((acc, cur) => acc + (cur.total_chunks || 0), 0);
            totalChunksSpan.textContent = `${totalChunks} 个切片`;

            if (docs.length === 0) {
                docsList.innerHTML = `
                    <div style="text-align: center; color: var(--text-muted); font-size: 0.8rem; padding: 24px 0;">
                        知识库尚无文档<br>请点击上方上传文档
                    </div>
                `;
                return;
            }

            docsList.innerHTML = docs.map(doc => `
                <div class="doc-item-card">
                    <div class="doc-item-info" style="cursor: pointer;" onclick="viewDocumentChunks('${escapeHtml(doc.doc_name)}')" title="点击查看切片与清洗详情">
                        <div class="doc-item-title" title="${escapeHtml(doc.doc_name)}">${escapeHtml(doc.doc_name)}</div>
                        <div class="doc-item-meta">
                            <span>${doc.total_chunks} 切片</span>
                            <span>•</span>
                            <span>${doc.file_size_formatted}</span>
                        </div>
                    </div>
                    <div class="doc-item-actions">
                        <button class="btn-inspect-doc" onclick="viewDocumentChunks('${escapeHtml(doc.doc_name)}')" title="查看切片与清洗详情">
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                        </button>
                        <button class="btn-delete-doc" onclick="deleteDocument('${escapeHtml(doc.doc_name)}')" title="删除文档">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                        </button>
                    </div>
                </div>
            `).join('');
        }

        // ==========================================
        // 文档切片与清洗详情验证器逻辑
        // ==========================================
        let currentInspectorDocData = null;

        async function viewDocumentChunks(docName) {
            try {
                const res = await fetch(`/api/documents/${encodeURIComponent(docName)}/chunks`);
                const result = await res.json();
                if (result.code === 200) {
                    currentInspectorDocData = result.data;
                    renderChunksInspector(result.data);
                    document.getElementById('chunks-inspector-modal').style.display = 'flex';
                } else {
                    alert('获取切片失败：' + (result.message || '未知错误'));
                }
            } catch (err) {
                alert('获取切片网络异常: ' + err.message);
            }
        }

        function closeChunksInspectorModal() {
            document.getElementById('chunks-inspector-modal').style.display = 'none';
            document.getElementById('chunks-search-input').value = '';
            currentInspectorDocData = null;
        }

        function renderChunksInspector(data, filterKeyword = '') {
            document.getElementById('inspector-doc-name').textContent = `📄 ${data.doc_name}`;

            const modeText = data.clean_mode === 'llm' ? '🤖 大模型智能清洗 (Mermaid/Markdown)' : '⚡ 代码普通清洗 (纯正则降噪)';
            const hashText = data.content_hash ? `SHA-256: ${data.content_hash.slice(0, 8)}...` : '';

            document.getElementById('inspector-badges').innerHTML = `
                <span class="inspector-badge badge-chunks">${data.total_chunks} 个切片</span>
                <span class="inspector-badge badge-chars">文本总长: ${data.total_chars.toLocaleString()} 字符</span>
                <span class="inspector-badge badge-mode">${modeText}</span>
                ${hashText ? `<span class="inspector-badge badge-chars" title="${data.content_hash}">${hashText}</span>` : ''}
            `;

            const chunksList = document.getElementById('inspector-chunks-list');
            const keyword = (filterKeyword || '').trim().toLowerCase();
            const filtered = data.chunks.filter(c => !keyword || (c.text || '').toLowerCase().includes(keyword));

            if (filtered.length === 0) {
                chunksList.innerHTML = `
                    <div style="text-align: center; color: var(--text-muted); padding: 32px 0;">
                        未找到包含关键词 “${escapeHtml(keyword)}” 的切片内容
                    </div>
                `;
                return;
            }

            chunksList.innerHTML = filtered.map(c => {
                const chunkId = `chunk-text-${c.chunk_index}`;
                return `
                    <div class="chunk-card">
                        <div class="chunk-header">
                            <div class="chunk-title-tag">
                                <span class="chunk-num">#${c.chunk_index}</span>
                                <span>切片序号 #${c.chunk_index} / ${data.total_chunks}</span>
                                <span style="color: var(--text-muted); font-size: 0.72rem;">(本块长: ${c.char_count} 字符)</span>
                            </div>
                            <button class="btn-copy-chunk" onclick="copyChunkText('${chunkId}')" title="复制该切片内容">
                                📋 复制本块
                            </button>
                        </div>
                        <div class="chunk-body" id="${chunkId}">${escapeHtml(c.text)}</div>
                    </div>
                `;
            }).join('');
        }

        function filterChunks() {
            if (!currentInspectorDocData) return;
            const keyword = document.getElementById('chunks-search-input').value;
            renderChunksInspector(currentInspectorDocData, keyword);
        }

        function copyChunkText(elementId) {
            const el = document.getElementById(elementId);
            if (!el) return;
            navigator.clipboard.writeText(el.innerText || el.textContent).then(() => {
                alert('切片内容已复制到剪贴板！');
            }).catch(e => {
                alert('复制失败: ' + e.message);
            });
        }

        let pendingUploadData = null; // 存储后端返回的 needs_confirmation 数据 (all_files, detected_files)
        let selectedCleanMode = 'llm'; // 触发图表阈值时，默认推荐大模型智能清洗

        function handleUpload(files) {
            if (!files || files.length === 0) return;
            startUploadWithPrecheck(Array.from(files));
        }

        async function startUploadWithPrecheck(files) {
            const formData = new FormData();
            formData.append('clean_mode', 'auto'); // 自动预检模式
            for (let i = 0; i < files.length; i++) {
                formData.append('files', files[i]);
            }

            uploadProgress.style.display = 'flex';
            uploadProgressText.textContent = `正在预检分析 ${files.length} 个文档的结构特征...`;

            try {
                const res = await fetch('/api/upload', {
                    method: 'POST',
                    body: formData
                });
                const result = await res.json();
                uploadProgress.style.display = 'none';

                if (result.code === 200) {
                    if (result.status === 'needs_confirmation') {
                        // 触发了复杂架构图阈值！向用户弹窗展示检测到的图表详情与清洗模式选择
                        pendingUploadData = result;
                        openDiagramConfirmModal(result.detected_files, result.all_files, result.skipped_files);
                    } else {
                        // 未检测到复杂架构图，后端已自动以极速代码模式完成入库！
                        alert(result.message || '文档已成功入库！');
                        fetchDocuments();
                    }
                } else {
                    alert('上传预检失败：' + (result.data?.errors?.join('\n') || result.message));
                }
            } catch (err) {
                uploadProgress.style.display = 'none';
                alert('上传网络请求异常: ' + err.message);
            } finally {
                fileInput.value = '';
            }
        }

        function openDiagramConfirmModal(detectedFiles, allFiles, skippedFiles = []) {
            const modal = document.getElementById('clean-mode-modal');
            const summaryDiv = document.getElementById('modal-files-summary');
            let html = detectedFiles.map(f => `
                <div class="modal-file-pill" style="border-left: 3px solid var(--accent-cyan); background: #f0fdf4;">
                    <span>📐</span>
                    <strong style="max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(f.filename)}</strong>
                    <span style="font-size: 0.72rem; color: #0369a1; background: #e0f2fe; padding: 2px 6px; border-radius: 4px; font-weight: 600;">
                        拐角: ${f.corner_count} | 箭头: ${f.arrow_count} | 拓扑符号: ${f.total_box_chars}
                    </span>
                </div>
            `).join('');

            if (skippedFiles && skippedFiles.length > 0) {
                html += `
                <div class="modal-file-pill" style="border-left: 3px solid #94a3b8; background: #f8fafc; color: #64748b;">
                    <span>⚡</span>
                    <span style="font-size: 0.78rem;">检测到 ${skippedFiles.length} 个文件已在知识库中存在，将自动跳过无需重复清洗</span>
                </div>
                `;
            }

            summaryDiv.innerHTML = html;
            selectCleanMode('llm'); // 触发阈值时，默认高亮大模型推荐选项
            modal.style.display = 'flex';
        }

        async function closeCleanModeModal(isCancel = false) {
            const modal = document.getElementById('clean-mode-modal');
            modal.style.display = 'none';
            if (isCancel && pendingUploadData && pendingUploadData.all_files) {
                try {
                    await fetch('/api/upload/cancel', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ filenames: pendingUploadData.all_files })
                    });
                } catch (e) { }
            }
            pendingUploadData = null;
            fileInput.value = '';
        }

        function selectCleanMode(mode) {
            selectedCleanMode = mode;
            const cardLlm = document.getElementById('mode-card-llm');
            const cardRule = document.getElementById('mode-card-rule');
            if (cardLlm && cardRule) {
                cardLlm.classList.toggle('active', mode === 'llm');
                cardRule.classList.toggle('active', mode === 'rule');
            }
        }

        async function confirmAndExecuteUpload() {
            if (!pendingUploadData || !pendingUploadData.all_files || pendingUploadData.all_files.length === 0) {
                closeCleanModeModal(false);
                return;
            }
            const allFiles = pendingUploadData.all_files;
            const mode = selectedCleanMode;
            closeCleanModeModal(false);

            uploadProgress.style.display = 'flex';
            const modeText = mode === 'llm' ? '大模型智能清洗 (拓扑重构)' : '代码普通清洗';
            uploadProgressText.textContent = `正在以【${modeText}】模式处理入库...`;

            try {
                const res = await fetch('/api/upload/confirm', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        filenames: allFiles,
                        clean_mode: mode
                    })
                });
                const result = await res.json();
                if (result.code === 200) {
                    alert(result.message);
                    fetchDocuments();
                } else {
                    alert('确认入库失败：' + (result.data?.errors?.join('\n') || result.message));
                }
            } catch (err) {
                alert('确认入库网络异常: ' + err.message);
            } finally {
                uploadProgress.style.display = 'none';
                pendingUploadData = null;
                fileInput.value = '';
            }
        }

        async function deleteDocument(docName) {
            if (!confirm(`确定要从知识库中删除文档【${docName}】吗？`)) return;

            try {
                const res = await fetch(`/api/documents/${encodeURIComponent(docName)}`, {
                    method: 'DELETE'
                });
                const result = await res.json();
                if (result.code === 200) {
                    fetchDocuments();
                } else {
                    alert(result.message);
                }
            } catch (err) {
                alert('删除失败: ' + err.message);
            }
        }

        function applySampleQuery(text) {
            userInput.value = text;
            userInput.focus();
            userInput.dispatchEvent(new Event('input'));
        }

        // 终止当前正在生成的问答
        function stopCurrentGeneration() {
            if (currentAbortController) {
                console.log("🛑 用户点击终止生成");
                currentAbortController.abort();
                currentAbortController = null;
            }
            if (currentActiveStreamReader) {
                try {
                    currentActiveStreamReader.cancel();
                } catch (e) {}
                currentActiveStreamReader = null;
            }
            if (btnStop) btnStop.style.display = 'none';
            if (btnSend) {
                btnSend.style.display = 'flex';
                btnSend.disabled = false;
            }
            userInput.focus();
        }

        // 4. 发送问答与多轮记忆交互 (支持打字机式渐进流式输出、实时思考追踪与主动终止)
        async function sendMessage() {
            // 动态安全获取最新 DOM 节点
            const inputEl = document.getElementById('user-input') || userInput;
            const sendBtn = document.getElementById('btn-send') || btnSend;
            const stopBtn = document.getElementById('btn-stop') || btnStop;
            const container = document.getElementById('messages-container') || messagesContainer;

            if (!inputEl) return;
            const query = inputEl.value.trim();
            if (!query) return;

            // 自愈保护：如果生成按钮未显示但控制器处于异常锁定态，立即释放
            if (currentAbortController && (!stopBtn || stopBtn.style.display !== 'flex')) {
                currentAbortController = null;
            }
            if (currentAbortController) return;

            if (welcomeHero) welcomeHero.style.display = 'none';

            appendUserMessage(query);
            userInput.value = '';
            userInput.style.height = 'auto';

            // 切换为“终止生成”按钮态 (深色带方块 ■)
            if (btnSend) btnSend.style.display = 'none';
            if (btnStop) btnStop.style.display = 'flex';

            currentAbortController = new AbortController();

            // 创建专属当前问答的流式渲染容器
            const msgDiv = document.createElement('div');
            msgDiv.className = 'message-row';
            msgDiv.innerHTML = `
                <div class="avatar avatar-ai">AI</div>
                <div class="message-content">
                    <div class="ai-bubble">
                        <!-- 动态思考过程卡片 (工具调用时自动展开，结束后收起) -->
                        <div class="agent-reasoning-card" id="stream-reasoning-card" style="display: none;">
                            <div class="agent-reasoning-header" onclick="toggleReasoning(this)">
                                <span class="reasoning-title-text">⚡ Agent 正在深度思考与多跳检索...</span>
                                <span>▼</span>
                            </div>
                            <div class="reasoning-steps-body" style="display: block;">
                                <!-- 动态插入步骤 -->
                            </div>
                        </div>

                        <!-- 状态提示文案 -->
                        <div class="stream-status-hint" style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px; font-size: 0.82rem; color: var(--primary);">
                            <div class="loading-spinner"></div>
                            <span class="stream-status-text">正在启动智能体规划分析...</span>
                        </div>

                        <!-- 正文 Markdown 打字机容器 -->
                        <div class="markdown-body"></div>

                        <!-- 引用文档来源 (完成后展示) -->
                        <div class="referenced-sources" style="display: none;"></div>
                    </div>
                </div>
            `;
            messagesContainer.appendChild(msgDiv);
            scrollToBottom();

            const reasoningCard = msgDiv.querySelector('.agent-reasoning-card');
            const reasoningTitle = msgDiv.querySelector('.reasoning-title-text');
            const stepsBody = msgDiv.querySelector('.reasoning-steps-body');
            const statusHint = msgDiv.querySelector('.stream-status-hint');
            const statusText = msgDiv.querySelector('.stream-status-text');
            const markdownBody = msgDiv.querySelector('.markdown-body');
            const sourcesDiv = msgDiv.querySelector('.referenced-sources');

            let accumulatedText = "";
            let currentStepIdx = 0;
            const selectedModel = document.getElementById('model-select') ? document.getElementById('model-select').value : '';

            try {
                const res = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        session_id: currentSessionId,
                        query: query,
                        stream: true,
                        model_name: selectedModel
                    }),
                    signal: currentAbortController.signal
                });

                if (!res.ok) {
                    throw new Error(`服务响应异常 (HTTP ${res.status})`);
                }

                const reader = res.body.getReader();
                currentActiveStreamReader = reader;
                const decoder = new TextDecoder('utf-8');
                let buffer = '';

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;

                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop(); // 保留不完整的一行

                    let currentEvent = 'message';
                    for (const line of lines) {
                        const trimmed = line.trim();
                        if (trimmed.startsWith('event:')) {
                            currentEvent = trimmed.replace('event:', '').trim();
                        } else if (trimmed.startsWith('data:')) {
                            const dataJson = trimmed.replace('data:', '').trim();
                            if (!dataJson) continue;
                            try {
                                const data = JSON.parse(dataJson);

                                if (currentEvent === 'session_init') {
                                    if (data.session_id) {
                                        currentSessionId = data.session_id;
                                    }
                                    if (data.model_name) {
                                        statusText.textContent = `[${data.model_name}] 正在启动智能体规划分析...`;
                                    }
                                } else if (currentEvent === 'step_start') {
                                    reasoningCard.style.display = 'block';
                                    statusHint.style.display = 'flex';
                                    const toolName = data.tool || '工具调用';
                                    statusText.textContent = `Agent 正在调用工具 [${toolName}] 执行检索...`;

                                    currentStepIdx++;
                                    const stepEl = document.createElement('div');
                                    stepEl.className = 'step-item';
                                    stepEl.id = `step-node-${currentStepIdx}`;
                                    stepEl.innerHTML = `
                                        <div>
                                            <strong>第 ${currentStepIdx} 步</strong>: 
                                            <span class="step-tool-badge">[${escapeHtml(toolName)}]</span> 
                                            <span>${escapeHtml(data.input || '')}</span>
                                        </div>
                                        <div class="step-obs" id="step-obs-${currentStepIdx}">⏳ 正在执行外部检索观察中...</div>
                                    `;
                                    stepsBody.appendChild(stepEl);
                                    scrollToBottom();

                                } else if (currentEvent === 'step_end') {
                                    const obsEl = stepsBody.querySelector(`#step-obs-${currentStepIdx}`);
                                    if (obsEl) {
                                        obsEl.textContent = `输出摘要: ${data.observation_preview || '已完成'}`;
                                    }
                                    statusText.textContent = `正在综合分析检索结果...`;

                                } else if (currentEvent === 'token') {
                                    // 渐进式打字机吐字
                                    statusHint.style.display = 'none';
                                    accumulatedText += data.delta;
                                    markdownBody.innerHTML = marked.parse(accumulatedText);
                                    scrollToBottom();

                                } else if (currentEvent === 'done') {
                                    statusHint.style.display = 'none';
                                    if (data.answer) {
                                        accumulatedText = data.answer;
                                        markdownBody.innerHTML = marked.parse(accumulatedText);
                                    }

                                    // 统计最终交互步数并自动收起折叠
                                    const finalStepsCount = data.steps ? data.steps.length : currentStepIdx;
                                    if (finalStepsCount > 0) {
                                        reasoningCard.style.display = 'block';
                                        reasoningTitle.textContent = `🔍 Agent 多跳检索与推理过程 (共 ${finalStepsCount} 次工具交互)`;
                                        stepsBody.style.display = 'none'; // 生成完毕后默认折叠
                                    } else {
                                        reasoningCard.style.display = 'none';
                                    }

                                    // 渲染参考文档来源标签与模型印章
                                    const docs = data.referenced_docs || [];
                                    const modelTag = data.model_name ? `<span class="source-tag" style="background: #f5f3ff; color: #7c3aed; border-color: #ddd6fe; font-weight: 600;">⚡ ${escapeHtml(data.model_name)}</span>` : '';
                                    if (docs.length > 0 || modelTag) {
                                        sourcesDiv.style.display = 'flex';
                                        sourcesDiv.innerHTML = `
                                            <span>${docs.length > 0 ? '参考来源:' : '调用引擎:'}</span>
                                            ${docs.map(d => `<span class="source-tag">📄 《${escapeHtml(d)}》</span>`).join('')}
                                            ${modelTag}
                                        `;
                                    }

                                    loadSessions(false);
                                    scrollToBottom();

                                } else if (currentEvent === 'error') {
                                    statusHint.style.display = 'none';
                                    markdownBody.innerHTML += `<div style="color: #ef4444; font-weight: 600; margin-top: 8px;">❌ ${escapeHtml(data.message || '执行异常')}</div>`;
                                }
                            } catch (e) {
                                console.error("解析流式数据包异常", e, dataJson);
                            }
                        }
                    }
                }
            } catch (err) {
                statusHint.style.display = 'none';
                if (err.name === 'AbortError' || err.message.includes('aborted')) {
                    // 用户主动点击“终止回答”
                    console.log("ℹ️ 问答流已由用户主动终止");
                    markdownBody.innerHTML += `
                        <div style="display: inline-flex; align-items: center; gap: 6px; margin-top: 10px; padding: 4px 10px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; font-size: 0.78rem; color: #64748b; font-weight: 500;">
                            <span>⏹️</span>
                            <span>已终止回答</span>
                        </div>
                    `;
                    // 若有思考步骤，则收起思考卡片
                    if (currentStepIdx > 0 && stepsBody) {
                        stepsBody.style.display = 'none';
                    }
                } else {
                    markdownBody.innerHTML = `<div style="color: #ef4444; font-weight: 600;">❌ 网络请求故障: ${escapeHtml(err.message)}</div>`;
                }
            } finally {
                currentAbortController = null;
                currentActiveStreamReader = null;
                if (btnStop) btnStop.style.display = 'none';
                if (btnSend) {
                    btnSend.style.display = 'flex';
                    btnSend.disabled = false;
                }
                userInput.focus();
            }
        }

        function appendUserMessage(text) {
            const div = document.createElement('div');
            div.className = 'message-row user-row';
            div.innerHTML = `
                <div class="message-content">
                    <div class="user-bubble">${escapeHtml(text)}</div>
                </div>
                <div class="avatar avatar-user">我</div>
            `;
            messagesContainer.appendChild(div);
            scrollToBottom();
        }

        function appendLoadingIndicator() {
            const id = 'loading-' + Date.now();
            const div = document.createElement('div');
            div.className = 'message-row';
            div.id = id;
            div.innerHTML = `
                <div class="avatar avatar-ai">AI</div>
                <div class="message-content">
                    <div class="ai-bubble">
                        <div class="typing-indicator">
                            <span></span><span></span><span></span>
                        </div>
                        <span style="margin-left: 10px; font-size: 0.85rem; color: var(--text-muted);">
                            Agent 正在基于多轮上下文进行多跳穿透检索...
                        </span>
                    </div>
                </div>
            `;
            messagesContainer.appendChild(div);
            scrollToBottom();
            return id;
        }

        function removeLoadingIndicator(id) {
            const el = document.getElementById(id);
            if (el) el.remove();
        }

        function appendAIMessage(answerText, steps = [], referencedDocs = []) {
            const div = document.createElement('div');
            div.className = 'message-row';

            let reasoningHtml = '';
            if (steps && steps.length > 0) {
                const stepsContent = steps.map((s, idx) => `
                    <div class="step-item">
                        <div>
                            <strong>第 ${idx + 1} 步</strong>: 
                            <span class="step-tool-badge">[${escapeHtml(s.tool)}]</span> 
                            <span>${escapeHtml(JSON.stringify(s.input))}</span>
                        </div>
                        <div class="step-obs">输出摘要: ${escapeHtml(s.observation_preview)}</div>
                    </div>
                `).join('');

                reasoningHtml = `
                    <div class="agent-reasoning-card">
                        <div class="agent-reasoning-header" onclick="toggleReasoning(this)">
                            <span>🔍 Agent 多跳检索与推理过程 (共 ${steps.length} 次工具交互)</span>
                            <span>▼</span>
                        </div>
                        <div class="reasoning-steps-body" style="display: none;">
                            ${stepsContent}
                        </div>
                    </div>
                `;
            }

            const renderedMarkdown = marked.parse(answerText);

            let sourcesHtml = '';
            if (referencedDocs && referencedDocs.length > 0) {
                const badges = referencedDocs.map(d => `<span class="source-tag">📄 《${escapeHtml(d)}》</span>`).join('');
                sourcesHtml = `
                    <div class="referenced-sources">
                        <span>参考文档来源:</span>
                        ${badges}
                    </div>
                `;
            }

            div.innerHTML = `
                <div class="avatar avatar-ai">AI</div>
                <div class="message-content">
                    <div class="ai-bubble">
                        ${reasoningHtml}
                        <div class="markdown-body">${renderedMarkdown}</div>
                        ${sourcesHtml}
                    </div>
                </div>
            `;

            messagesContainer.appendChild(div);
            scrollToBottom();
        }

        function toggleReasoning(headerEl) {
            const body = headerEl.nextElementSibling;
            const isHidden = body.style.display === 'none';
            body.style.display = isHidden ? 'flex' : 'none';
            headerEl.querySelector('span:last-child').textContent = isHidden ? '▲' : '▼';
        }

        function scrollToBottom() {
            messagesContainer.scrollTop = messagesContainer.scrollHeight;
        }

        function escapeHtml(str) {
            if (!str) return '';
            return String(str)
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }


// 显式挂载到 window 作用域以支持内联属性回调
window.sendMessage = sendMessage;
