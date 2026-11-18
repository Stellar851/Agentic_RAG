// 全局状态管理
        let currentStep = 1;
        let viewMode = 'focus'; // 'focus' | 'all'
        let currentCases = [];
        let answeredCases = [];
        let latestJudgeReport = null;

        // =====================================================================
        // 全局评测动态模型配置中心 (与 models.py 动态联动)
        // =====================================================================
        let benchmarkConfig = {
            answer_model: 'qwen3.8-flash',
            judge_model: 'qwen3.8-max-0902',
            rerank_model: 'gte-rerank-v2',
            embedding_model: 'bge-m3'
        };

        async function loadBenchmarkConfig() {
            try {
                const res = await fetch('/api/benchmark/config?t=' + Date.now());
                const json = await res.json();
                if (json.code === 200 && json.data) {
                    benchmarkConfig = json.data;
                    applyBenchmarkConfigToUI(benchmarkConfig);
                }
            } catch (e) {
                console.warn('获取基准评测配置异常:', e);
            }
        }

        function applyBenchmarkConfigToUI(cfg) {
            if (!cfg) return;
            const phJudge = document.getElementById('pipelineHeaderJudge');
            if (phJudge) phJudge.textContent = cfg.judge_model;
            const phAns = document.getElementById('pipelineHeaderAnswer');
            if (phAns) phAns.textContent = cfg.answer_model;
            const phRerank = document.getElementById('pipelineHeaderRerank');
            if (phRerank) phRerank.textContent = cfg.rerank_model;
            const phJudge2 = document.getElementById('pipelineHeaderJudge2');
            if (phJudge2) phJudge2.textContent = cfg.judge_model;

            const tab3Desc = document.getElementById('tabStep3Desc');
            if (tab3Desc) tab3Desc.textContent = `${cfg.answer_model} + 百炼重排`;
            const tab1Desc = document.getElementById('tabStep1Desc');
            if (tab1Desc) tab1Desc.textContent = `${cfg.judge_model} 派生考题`;

            const stage3Sub = document.getElementById('stage3SubtitleModel');
            if (stage3Sub) stage3Sub.textContent = cfg.answer_model;
            const btnAnsTxt = document.getElementById('btnRunAnswersText');
            if (btnAnsTxt) btnAnsTxt.textContent = `🚀 启动 ${cfg.answer_model} 批量并发作答`;
            const btnJudgeTxt = document.getElementById('btnRunJudgeText');
            if (btnJudgeTxt) btnJudgeTxt.textContent = `⚖️ 启动 ${cfg.judge_model} 裁判模型量化打分`;
        }


        let selectedLimit = 0; // 0 表示全部

        // 筛选与排序状态
        let filterStatus = 'all'; // 'all' | 'passed' | 'failed' | 'invalid'
        let filterCategory = 'all';
        let scoreSortOrder = 'none'; // 'none' | 'asc' | 'desc'
        let generateCaseCount = 10; // 阶段一用户自定义出题数量

        // =====================================================================
        // 出题数量选择器联动控制
        // =====================================================================
        function setGenerateCaseCount(count) {
            generateCaseCount = parseInt(count, 10) || 10;
            [5, 10, 15, 20].forEach(n => {
                const pill = document.getElementById('pillCount' + n);
                if (pill) {
                    if (n === generateCaseCount) pill.classList.add('active');
                    else pill.classList.remove('active');
                }
            });
            const customInput = document.getElementById('customCaseCountInput');
            if (customInput && [5, 10, 15, 20].includes(generateCaseCount)) {
                customInput.value = '';
            }
            updateGenerateButtonsText();
        }

        function handleCustomCaseCountInput(val) {
            const num = parseInt(val, 10);
            if (num && num > 0 && num <= 100) {
                generateCaseCount = num;
                [5, 10, 15, 20].forEach(n => {
                    const pill = document.getElementById('pillCount' + n);
                    if (pill) pill.classList.remove('active');
                });
                updateGenerateButtonsText();
            }
        }

        function updateGenerateButtonsText() {
            const btnGuideText = document.getElementById('btnLoadGuideText');
            const btnGenText = document.getElementById('btnGenerateCasesText');
            if (btnGuideText) {
                btnGuideText.textContent = `⚡ 载入基准 ${generateCaseCount} 题`;
            }
            if (btnGenText) {
                const answerModel = (typeof cachedConfig !== 'undefined' && cachedConfig && cachedConfig.judge_model) ? cachedConfig.judge_model : 'qwen3.8-max-0902';
                btnGenText.textContent = `🚀 仅基于知识库出 ${generateCaseCount} 题 (${answerModel})`;
            }
        }


        document.addEventListener('DOMContentLoaded', () => {
            fetchExistingCases();
            loadBenchmarkConfig();
            fetchLatestAnsweredCases();
            fetchLatestEvaluationReport();
        });

        // =====================================================================
        // 流水线选项卡切换
        // =====================================================================
        function switchStep(stepIndex) {
            currentStep = stepIndex;

            // 更新 Stepper Tab 高亮
            for (let i = 1; i <= 5; i++) {
                const tab = document.getElementById(`tabStep${i}`);
                if (tab) {
                    if (i === stepIndex) tab.classList.add('active');
                    else tab.classList.remove('active');
                }
            }

            // 更新 Panel 显隐
            if (viewMode === 'focus') {
                for (let i = 1; i <= 5; i++) {
                    const panel = document.getElementById(`stagePanel${i}`);
                    if (panel) {
                        if (i === stepIndex) panel.classList.add('active');
                        else panel.classList.remove('active');
                    }
                }
            }
        }

        function setViewMode(mode) {
            viewMode = mode;
            const btnFocus = document.getElementById('btnModeFocus');
            const btnAll = document.getElementById('btnModeAll');
            const txt = document.getElementById('currentWorkspaceText');

            if (mode === 'all') {
                document.body.classList.add('mode-all-stages');
                btnFocus.classList.remove('active');
                btnAll.classList.add('active');
                txt.textContent = '全流水线全览模式 (All Pipeline View)';
            } else {
                document.body.classList.remove('mode-all-stages');
                btnAll.classList.remove('active');
                btnFocus.classList.add('active');
                txt.textContent = '阶段聚焦模式 (Focus View)';
                switchStep(currentStep);
            }
        }

        function selectLimit(limit) {
            selectedLimit = limit;
            const b3 = document.getElementById('btnLimit3');
            const b5 = document.getElementById('btnLimit5');
            const ba = document.getElementById('btnLimitAll');

            b3.style.background = 'rgba(255,255,255,0.05)'; b3.style.color = '#cbd5e1';
            b5.style.background = 'rgba(255,255,255,0.05)'; b5.style.color = '#cbd5e1';
            ba.style.background = 'rgba(255,255,255,0.05)'; ba.style.color = '#cbd5e1';

            if (limit === 3) { b3.style.background = 'var(--primary)'; b3.style.color = '#fff'; }
            else if (limit === 5) { b5.style.background = 'var(--primary)'; b5.style.color = '#fff'; }
            else { ba.style.background = 'var(--primary)'; ba.style.color = '#fff'; }
        }

        // =====================================================================
        // 阶段一：出题逻辑
        // =====================================================================
        async function fetchExistingCases() {
            try {
                const res = await fetch('/api/benchmark/cases');
                const data = await res.json();
                if (data.code === 200 && Array.isArray(data.data) && data.data.length > 0) {
                    currentCases = data.data;
                    renderCasesTable();
                    document.getElementById('totalCasesCountBadge').textContent = currentCases.length;
                }
            } catch (e) {
                console.warn('获取既有用例失败:', e);
            }
        }

        async function loadBuiltinGuideCases() {
            const btn = document.getElementById('btnLoadGuide');
            const logBox = document.getElementById('generateLogBox');
            btn.disabled = true;
            logBox.innerHTML = '<div class="log-line-info">⚡ 正在装载精编基准考题库...</div>';

            try {
                const res = await fetch('/api/benchmark/generate-cases', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ mode: 'from-guide', case_count: generateCaseCount })
                });
                const data = await res.json();
                if (data.code === 200) {
                    currentCases = data.data;
                    renderCasesTable();
                    document.getElementById('totalCasesCountBadge').textContent = currentCases.length;
                    logBox.innerHTML += `<div class="log-line-ok">✅ 成功装载 ${currentCases.length} 道精编考题并写入待审核测试集！</div>`;
                    showToast(`成功装载 ${currentCases.length} 道基准考题`);
                    switchStep(2);
                } else {
                    logBox.innerHTML += `<div class="log-line-err">❌ 装载失败: ${data.message}</div>`;
                }
            } catch (err) {
                logBox.innerHTML += `<div class="log-line-err">❌ 请求异常: ${err.message}</div>`;
            } finally {
                btn.disabled = false;
            }
        }

        async function startGenerateCases() {
            const btn = document.getElementById('btnGenerateCases');
            const logBox = document.getElementById('generateLogBox');
            btn.disabled = true;
            logBox.innerHTML = '<div class="log-line-info">🤖 正在调用 qwen3.8-max-0902 深入扫描已入库真实业务文档并自动派生定向题库...</div>';
            logBox.innerHTML += '<div class="log-line-warn">⏳ 覆盖单跳检索、多跳推理、上下文精度及否定超纲四大矩阵，预计耗时 15~35 秒...</div>';

            try {
                const res = await fetch('/api/benchmark/generate-cases', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ mode: 'from-docs', model_a: (typeof cachedConfig !== 'undefined' && cachedConfig && cachedConfig.judge_model) ? cachedConfig.judge_model : 'qwen3.8-max-0902', case_count: generateCaseCount })
                });
                const data = await res.json();
                if (data.code === 200) {
                    currentCases = data.data;
                    renderCasesTable();
                    document.getElementById('totalCasesCountBadge').textContent = currentCases.length;
                    logBox.innerHTML += `<div class="log-line-ok">✅ 出题完成！基于当前知识库物理文档成功派生 ${currentCases.length} 道真实业务考题！</div>`;
                    showToast(`基于实际知识库成功生成 ${currentCases.length} 道测试题`);
                    switchStep(2);
                } else {
                    logBox.innerHTML += `<div class="log-line-err">❌ 出题失败: ${data.message}</div>`;
                }
            } catch (err) {
                logBox.innerHTML += `<div class="log-line-err">❌ 出题异常: ${err.message}</div>`;
            } finally {
                btn.disabled = false;
            }
        }

        // =====================================================================
        // 阶段二：测试用例表格渲染与编辑
        // =====================================================================
        function renderCasesTable() {
            const tbody = document.getElementById('casesTableBody');
            if (!currentCases || currentCases.length === 0) {
                tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 40px;">暂无测试用例</td></tr>`;
                return;
            }

            tbody.innerHTML = currentCases.map((c, idx) => `
                <tr>
                    <td style="font-family: 'JetBrains Mono', monospace; font-weight: 700; color: var(--text-muted);">${c.id || idx + 1}</td>
                    <td>${getCategoryTagHtml(c.category)}</td>
                    <td>
                        <textarea class="edit-cell-input" rows="2" style="width: 100%; background: rgba(0,0,0,0.3); border: 1px solid var(--border-subtle); border-radius: 6px; color: #fff; padding: 6px; font-size: 0.8rem;" onchange="updateCaseField(${idx}, 'query', this.value)">${escapeHtml(c.query || '')}</textarea>
                    </td>
                    <td>
                        <textarea class="edit-cell-input" rows="2" style="width: 100%; background: rgba(0,0,0,0.3); border: 1px solid var(--border-subtle); border-radius: 6px; color: #fff; padding: 6px; font-size: 0.8rem;" onchange="updateCaseField(${idx}, 'ground_truth', this.value)">${escapeHtml(c.reviewed_ground_truth || c.ground_truth || '')}</textarea>
                    </td>
                    <td style="font-size: 0.76rem; color: var(--cyan);">${escapeHtml(c.source_doc || '全局多文档')}</td>
                    <td>
                        <select style="background: var(--bg-surface-elevated); border: 1px solid var(--border-subtle); color: #fff; border-radius: 6px; padding: 4px 6px; font-size: 0.76rem;" onchange="updateCaseField(${idx}, 'review_status', this.value)">
                            <option value="通过" ${c.review_status === '通过' ? 'selected' : ''}>✅ 通过</option>
                            <option value="待修正" ${c.review_status === '待修正' ? 'selected' : ''}>⚠️ 待修正</option>
                            <option value="废弃" ${c.review_status === '废弃' ? 'selected' : ''}>❌ 废弃</option>
                        </select>
                    </td>
                    <td>
                        <button onclick="removeCase(${idx})" style="background: rgba(244,63,94,0.1); border: 1px solid rgba(244,63,94,0.3); color: var(--rose); padding: 4px 8px; border-radius: 6px; cursor: pointer; font-size: 0.72rem;">删除</button>
                    </td>
                </tr>
            `).join('');
        }

        function updateCaseField(idx, field, val) {
            if (currentCases[idx]) {
                currentCases[idx][field] = val;
            }
        }

        function removeCase(idx) {
            currentCases.splice(idx, 1);
            renderCasesTable();
            document.getElementById('totalCasesCountBadge').textContent = currentCases.length;
            showToast('已删除该测试题');
        }

        function addNewEmptyCase() {
            const newId = currentCases.length + 1;
            currentCases.push({
                id: newId,
                category: '多跳关联推理型',
                query: '请输入测试提问...',
                ground_truth: '请输入预期黄金标准答案...',
                source_doc: '手动录入',
                review_status: '通过'
            });
            renderCasesTable();
            document.getElementById('totalCasesCountBadge').textContent = currentCases.length;
            showToast('已新增自定义测试题');
        }

        async function saveCasesToServer() {
            const btn = document.getElementById('btnSaveCases');
            btn.disabled = true;
            try {
                const res = await fetch('/api/benchmark/save-cases', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ cases: currentCases })
                });
                const data = await res.json();
                if (data.code === 200) {
                    showToast('题库修改已成功持久化至 Excel！');
                } else {
                    alert('保存失败: ' + data.message);
                }
            } catch (e) {
                alert('保存异常: ' + e.message);
            } finally {
                btn.disabled = false;
            }
        }

        function exportCasesExcel() {
            window.location.href = '/api/benchmark/export-cases';
        }

        // =====================================================================
        // =====================================================================
        // 通用 SSE POST 流式读取解析器
        // =====================================================================
        async function streamSSEPost(url, payload, handlers) {
            const response = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (!response.ok) {
                const errTxt = await response.text();
                throw new Error(`服务响应错误 [${response.status}]: ${errTxt || response.statusText}`);
            }
            const reader = response.body.getReader();
            const decoder = new TextDecoder('utf-8');
            let buffer = '';

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const blocks = buffer.split('\n\n');
                buffer = blocks.pop(); // 保持未完结部分

                for (const block of blocks) {
                    if (!block.trim()) continue;
                    let event = 'message';
                    let dataStr = '';
                    const lines = block.split('\n');
                    for (const line of lines) {
                        if (line.startsWith('event:')) {
                            event = line.replace('event:', '').trim();
                        } else if (line.startsWith('data:')) {
                            dataStr += line.replace('data:', '').trim();
                        }
                    }
                    if (dataStr) {
                        try {
                            const data = JSON.parse(dataStr);
                            if (handlers[event]) handlers[event](data);
                            if (handlers['all']) handlers['all'](event, data);
                        } catch (e) {
                            console.warn('解析 SSE 帧异常:', e, dataStr);
                        }
                    }
                }
            }
        }

        // =====================================================================
        // 阶段三：考生批量作答 (SSE 实时流式增量推流)
        // =====================================================================
        async function startRunAnswers() {
            const btn = document.getElementById('btnRunAnswers');
            const logBox = document.getElementById('answersLogBox');
            btn.disabled = true;
            answeredCases = []; // 重置作答集
            renderAnswersTable();

            logBox.innerHTML = `
                <div class="progress-bar-wrap">
                    <div class="progress-bar-head">
                        <span id="answersProgressText">🚀 考生系统已启动，正在连线阿里百炼与重排模型...</span>
                        <span id="answersProgressPercent" class="progress-badge">0%</span>
                    </div>
                    <div class="progress-bar-track">
                        <div id="answersProgressBar" class="progress-bar-fill" style="width: 0%;"></div>
                    </div>
                </div>
                <div id="answersStreamLines" class="stream-lines-box"></div>
            `;

            const pBar = document.getElementById('answersProgressBar');
            const pTxt = document.getElementById('answersProgressText');
            const pPct = document.getElementById('answersProgressPercent');
            const sLines = document.getElementById('answersStreamLines');

            try {
                await streamSSEPost('/api/benchmark/run-answers-stream', {
                    limit: selectedLimit,
                    model_b: benchmarkConfig.answer_model || 'qwen3.8-flash'
                }, {
                    progress: (d) => {
                        const pct = Math.round(((d.current - 0.5) / d.total) * 100);
                        pBar.style.width = pct + '%';
                        pPct.textContent = pct + '%';
                        pTxt.innerHTML = `<span>⏳ 正在检索作答 [${d.current}/${d.total}]: <strong>${escapeHtml(d.query.substring(0, 30))}...</strong></span>`;
                        const div = document.createElement('div');
                        div.className = 'stream-item stream-pending';
                        div.innerHTML = `<span class="badge-idx">#${d.current}</span> 检索召回中: ${escapeHtml(d.query)}`;
                        sLines.prepend(div);
                    },
                    case_done: (d) => {
                        const pct = Math.round((d.current / d.total) * 100);
                        pBar.style.width = pct + '%';
                        pPct.textContent = pct + '%';
                        
                        // 增量压入并即时刷新上屏
                        answeredCases.push(d.case);
                        renderAnswersTable();

                        const firstItem = sLines.firstChild;
                        if (firstItem && firstItem.classList.contains('stream-pending')) {
                            firstItem.className = 'stream-item stream-ok';
                            if (d.case.is_valid === false) {
                                firstItem.className = 'stream-item stream-fail';
                                firstItem.innerHTML = `<span class="badge-idx">#${d.current}</span> ⚠️ 作答失败 (${escapeHtml(d.case.error_type || 'agent_error')}) · 本题不计分`;
                            } else {
                                firstItem.innerHTML = `<span class="badge-idx">#${d.current}</span> ✅ 作答就绪 (${d.case.latency}s, 证据切片: ${d.case.contexts_count}条) · 回复: <em>${escapeHtml((d.case.system_answer || '').substring(0, 45))}...</em>`;
                            }
                        }
                    },
                    all_done: (d) => {
                        pBar.style.width = '100%';
                        pPct.textContent = '100%';
                        pTxt.innerHTML = `<span>🎉 全量 ${d.total} 道题目作答完成！真实切片证据已存证。</span>`;
                        answeredCases = d.data || d.cases || (Array.isArray(d) ? d : answeredCases);
                        try { localStorage.setItem('benchmark_answered_cases', JSON.stringify(answeredCases)); } catch (e) {}
                        renderAnswersTable();
                        showToast(`完成 ${answeredCases.length} 道测试题的客观作答！`);
                        setTimeout(() => { switchStep(4); }, 1200);
                    }
                });
            } catch (err) {
                sLines.innerHTML += `<div class="stream-item stream-err">❌ 作答流式执行异常: ${escapeHtml(err.message)}</div>`;
                showToast('作答异常中止: ' + err.message);
            } finally {
                btn.disabled = false;
            }
        }

        async function fetchLatestAnsweredCases() {
            try {
                const res = await fetch('/api/benchmark/latest-answers?t=' + Date.now());
                const data = await res.json();
                if (data.code === 200 && Array.isArray(data.data) && data.data.length > 0) {
                    answeredCases = data.data;
                    try { localStorage.setItem('benchmark_answered_cases', JSON.stringify(answeredCases)); } catch (e) {}
                    renderAnswersTable();
                    return;
                }
            } catch (e) {
                console.warn('获取后端作答快照异常:', e);
            }

            try {
                const local = localStorage.getItem('benchmark_answered_cases');
                if (local) {
                    const parsed = JSON.parse(local);
                    if (Array.isArray(parsed) && parsed.length > 0) {
                        answeredCases = parsed;
                        renderAnswersTable();
                    }
                }
            } catch (e) {
                console.warn('读取本地作答缓存异常:', e);
            }
        }

        function renderAnswersTable() {
            const tbody = document.getElementById('answersTableBody');
            if (!answeredCases || answeredCases.length === 0) {
                tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 40px;">暂无作答凭据</td></tr>`;
                return;
            }

            tbody.innerHTML = answeredCases.map((c, idx) => `
                <tr>
                    <td style="font-family: 'JetBrains Mono', monospace; font-weight: 700; color: var(--text-muted);">${c.id || idx + 1}</td>
                    <td>${getCategoryTagHtml(c.category)}</td>
                    <td style="font-weight: 600; color: #fff;">${escapeHtml(c.query)}</td>
                    <td style="color: #94a3b8; font-size: 0.78rem;">${escapeHtml(c.ground_truth || '')}</td>
                    <td style="color: #cbd5e1; font-size: 0.78rem; background: rgba(0,0,0,0.2); border-radius: 6px; padding: 10px;">${escapeHtml(c.system_answer || '')}</td>
                    <td style="font-family: 'JetBrains Mono', monospace; color: var(--cyan);">${c.contexts_count || 0}</td>
                    <td style="font-family: 'JetBrains Mono', monospace; color: var(--text-muted);">${c.latency || 0}s</td>
                </tr>
            `).join('');
        }

        // =====================================================================
        // 阶段四：裁判模型量化打分 (SSE 实时流式增量推流)
        // =====================================================================
        async function startJudgeEvaluation() {
            if (!answeredCases || answeredCases.length === 0) {
                // 自愈尝试：自动拉取最新的考生作答记录快照
                await fetchLatestAnsweredCases();
            }
            if (!answeredCases || answeredCases.length === 0) {
                alert('请先完成阶段三的考生系统作答，生成客观凭据后再启动裁判！');
                switchStep(3);
                return;
            }

            const btn = document.getElementById('btnRunJudge');
            const logBox = document.getElementById('judgeLogBox');
            btn.disabled = true;

            logBox.innerHTML = `
                <div class="progress-bar-wrap">
                    <div class="progress-bar-head">
                        <span id="judgeProgressText">⚖️ 裁判模型 (qwen3.8-max-0902) 启动！针对每道题进行深层原子陈述检验...</span>
                        <span id="judgeProgressPercent" class="progress-badge">0%</span>
                    </div>
                    <div class="progress-bar-track">
                        <div id="judgeProgressBar" class="progress-bar-fill" style="width: 0%;"></div>
                    </div>
                </div>
                <div id="judgeStreamLines" class="stream-lines-box"></div>
            `;

            const pBar = document.getElementById('judgeProgressBar');
            const pTxt = document.getElementById('judgeProgressText');
            const pPct = document.getElementById('judgeProgressPercent');
            const sLines = document.getElementById('judgeStreamLines');

            try {
                await streamSSEPost('/api/benchmark/run-judge-stream', {
                    answered_cases: answeredCases,
                    model_c: benchmarkConfig.judge_model || 'qwen3.8-max-0902'
                }, {
                    progress: (d) => {
                        const pct = Math.round(((d.current - 0.5) / d.total) * 100);
                        pBar.style.width = pct + '%';
                        pPct.textContent = pct + '%';
                        pTxt.innerHTML = `<span>⏳ 裁判审理中 [${d.current}/${d.total}]: <strong>${escapeHtml(d.query.substring(0, 30))}...</strong></span>`;
                        const div = document.createElement('div');
                        div.className = 'stream-item stream-pending';
                        div.innerHTML = `<span class="badge-idx">#${d.current}</span> 检验原子命题: ${escapeHtml(d.query)}`;
                        sLines.prepend(div);
                    },
                    case_done: (d) => {
                        const pct = Math.round((d.current / d.total) * 100);
                        pBar.style.width = pct + '%';
                        pPct.textContent = pct + '%';
                        
                        const scores = d.case.scores || {};
                        const score = scores.rag_triad_score || 0;
                        const isValid = d.case.is_valid !== false;
                        const isPass = isValid && d.case.is_passed;
                        const diag = (d.case.diagnoses && d.case.diagnoses[0]) ? d.case.diagnoses[0] : '事实高度一致';

                        const firstItem = sLines.firstChild;
                        if (firstItem && firstItem.classList.contains('stream-pending')) {
                            if (!isValid) {
                                const failureLabel = d.case.error_stage === 'judge' ? '裁判失败' : '系统故障';
                                firstItem.className = 'stream-item stream-fail';
                                firstItem.innerHTML = `<span class="badge-idx">#${d.current}</span> ⚠️ ${failureLabel}: <strong>${escapeHtml(d.case.error_type || 'unknown')}</strong> · 本题不计分`;
                            } else {
                                firstItem.className = isPass ? 'stream-item stream-ok' : 'stream-item stream-fail';
                                firstItem.innerHTML = `<span class="badge-idx">#${d.current}</span> ${isPass ? '✅' : '❌'} 综合分: <strong>${(score * 100).toFixed(1)}分</strong> (忠实度: ${((scores.faithfulness || 0)*100).toFixed(0)}% | 相关度: ${((scores.answer_relevance || 0)*100).toFixed(0)}%) · 诊断: <em>${escapeHtml(diag.substring(0, 40))}</em>`;
                            }
                        }
                    },
                    all_done: (d) => {
                        pBar.style.width = '100%';
                        pPct.textContent = '100%';
                        latestJudgeReport = d.summary || d;
                        try { localStorage.setItem('benchmark_latest_judge_report', JSON.stringify(latestJudgeReport)); } catch (e) {}
                        const validPassRate = latestJudgeReport.valid_pass_rate ?? latestJudgeReport.pass_rate ?? 0;
                        const invalidCount = latestJudgeReport.invalid_cases || 0;
                        const avgScores = latestJudgeReport.average_scores || {};
                        const triadScore = avgScores.rag_triad_score || 0;
                        pTxt.innerHTML = `<span>评测完成！有效通过率: ${(validPassRate * 100).toFixed(1)}% | 有效综合分: ${(triadScore * 100).toFixed(1)}分 | 流程故障: ${invalidCount} 题</span>`;
                        renderJudgeDashboard(latestJudgeReport);
                        showToast('评测打分完成，正在呈现全景诊断看板');
                        setTimeout(() => { switchStep(5); }, 1200);
                    }
                });
            } catch (err) {
                sLines.innerHTML += `<div class="stream-item stream-err">❌ 裁判流式打分异常: ${escapeHtml(err.message)}</div>`;
                showToast('裁判执行异常中止: ' + err.message);
            } finally {
                btn.disabled = false;
            }
        }

        // 阶段五：全维透视仪表盘与诊断报表渲染
        // =====================================================================
        async function fetchLatestEvaluationReport() {
            let loaded = false;
            try {
                const res = await fetch('/api/eval/latest-report?t=' + Date.now());
                const data = await res.json();
                if (data.code === 200 && data.data) {
                    const sum = data.data.summary || data.data;
                    if (sum && sum.case_results && sum.case_results.length > 0) {
                        latestJudgeReport = sum;
                        try { localStorage.setItem('benchmark_latest_judge_report', JSON.stringify(latestJudgeReport)); } catch (e) {}
                        renderJudgeDashboard(latestJudgeReport);
                        loaded = true;
                    }
                }
            } catch (e) {
                console.warn('获取最新评测报表异常:', e);
            }

            if (!loaded) {
                try {
                    const local = localStorage.getItem('benchmark_latest_judge_report');
                    if (local) {
                        const sum = JSON.parse(local);
                        if (sum && sum.case_results && sum.case_results.length > 0) {
                            latestJudgeReport = sum;
                            renderJudgeDashboard(latestJudgeReport);
                        }
                    }
                } catch (e) {
                    console.warn('读取本地评测缓存异常:', e);
                }
            }
        }

        function renderJudgeDashboard(sum) {
            if (!sum) return;
            const avg = sum.average_scores || {};
            const triad = Math.round((avg.rag_triad_score || 0) * 1000) / 10;
            const faith = Math.round((avg.faithfulness || 0) * 1000) / 10;
            const rel   = Math.round((avg.answer_relevance || 0) * 1000) / 10;
            const prec  = Math.round((avg.context_precision || 0) * 1000) / 10;
            const rec   = avg.context_recall !== null ? Math.round((avg.context_recall || 0) * 1000) / 10 : 0;
            const passR = Math.round((sum.pass_rate || 0) * 1000) / 10;
            const validCases = sum.valid_cases ?? sum.total_cases ?? 0;
            const validPassR = Math.round(((sum.valid_pass_rate ?? sum.pass_rate) || 0) * 1000) / 10;

            // 填充 KPI 卡片
            document.getElementById('kpiTriadScore').textContent = triad.toFixed(1);
            document.getElementById('kpiTriadBar').style.width = `${Math.min(100, triad)}%`;
            document.getElementById('kpiTotalCases').textContent = sum.total_cases;
            document.getElementById('kpiValidCases').textContent = validCases;
            document.getElementById('kpiValidPassRate').textContent = `${validPassR.toFixed(1)}%`;
            document.getElementById('kpiPassRate').textContent = `${passR.toFixed(1)}%`;

            const triadGrade = document.getElementById('kpiTriadGrade');
            if (triad >= 85) { triadGrade.textContent = 'EXCELLENT'; triadGrade.className = 'kpi-badge badge-faith'; }
            else if (triad >= 70) { triadGrade.textContent = 'GOOD'; triadGrade.className = 'kpi-badge badge-triad'; }
            else { triadGrade.textContent = 'NEEDS OPT'; triadGrade.className = 'kpi-badge badge-prec'; }

            document.getElementById('kpiFaithScore').textContent = faith.toFixed(1);
            document.getElementById('kpiFaithBar').style.width = `${Math.min(100, faith)}%`;

            document.getElementById('kpiRelScore').textContent = rel.toFixed(1);
            document.getElementById('kpiRelBar').style.width = `${Math.min(100, rel)}%`;

            document.getElementById('kpiPrecScore').textContent = prec.toFixed(1);
            document.getElementById('kpiPrecBar').style.width = `${Math.min(100, prec)}%`;

            document.getElementById('kpiRecScore').textContent = rec > 0 ? rec.toFixed(1) : '--';
            document.getElementById('kpiRecBar').style.width = `${Math.min(100, rec)}%`;

            // 渲染系统级诊断横幅
            renderSystemDiagBanner(sum);

            // 渲染表格
            applyJudgeFilters();
        }

        function renderSystemDiagBanner(sum) {
            const banner = document.getElementById('systemDiagBanner');
            const avg = sum.average_scores || {};
            const triad = (avg.rag_triad_score || 0) * 100;
            const passCount = sum.passed_cases || 0;
            const total = sum.total_cases || 0;
            const valid = sum.valid_cases ?? total;
            const invalid = sum.invalid_cases || 0;
            const systemFailed = sum.system_failed_cases || 0;
            const judgeFailed = sum.judge_failed_cases || 0;
            const failureText = invalid > 0
                ? `流程故障 ${invalid} 题（系统 ${systemFailed} / 裁判 ${judgeFailed}），已从质量指标中剔除。`
                : '本轮无流程故障，全部样本均获得有效评分。';

            let bannerHtml = '';
            if (triad >= 80) {
                bannerHtml = `
                    <div style="background: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 12px; padding: 16px 20px; display: flex; align-items: center; justify-content: space-between;">
                        <div style="display: flex; align-items: center; gap: 14px;">
                            <span style="font-size: 24px;">🛡️</span>
                            <div>
                                <div style="font-size: 0.92rem; font-weight: 700; color: #fff;">【全链路评估健康】系统各项指标已达企业生产级标准</div>
                                <div style="font-size: 0.78rem; color: var(--text-secondary); margin-top: 3px;">有效样本质量指标已达到当前阈值，但仍需关注流程稳定性。</div>
                                <div style="font-size: 0.75rem; color: ${invalid > 0 ? 'var(--amber)' : 'var(--text-secondary)'}; margin-top: 5px;">${failureText}</div>
                            </div>
                        </div>
                        <div style="font-family: 'JetBrains Mono', monospace; font-size: 1.1rem; font-weight: 700; color: var(--emerald);">有效通过: ${passCount} / ${valid} 题</div>
                    </div>
                `;
            } else {
                bannerHtml = `
                    <div style="background: rgba(244, 63, 94, 0.08); border: 1px solid rgba(244, 63, 94, 0.3); border-radius: 12px; padding: 16px 20px; display: flex; align-items: center; justify-content: space-between;">
                        <div style="display: flex; align-items: center; gap: 14px;">
                            <span style="font-size: 24px;">⚠️</span>
                            <div>
                                <div style="font-size: 0.92rem; font-weight: 700; color: #fff;">【核心优化预警】当前通过率仍有提升空间</div>
                                <div style="font-size: 0.78rem; color: var(--text-secondary); margin-top: 3px;">部分有效样本因知识库未收录或切片碎片化导致失分，请使用下方【全链路单题 Trace】下钻定位。</div>
                                <div style="font-size: 0.75rem; color: ${invalid > 0 ? 'var(--amber)' : 'var(--text-secondary)'}; margin-top: 5px;">${failureText}</div>
                            </div>
                        </div>
                        <div style="font-family: 'JetBrains Mono', monospace; font-size: 1.1rem; font-weight: 700; color: var(--rose);">有效通过: ${passCount} / ${valid} 题</div>
                    </div>
                `;
            }
            banner.innerHTML = bannerHtml;
            banner.style.display = 'block';
        }

        // =====================================================================
        // 多维过滤器与结果列表渲染
        // =====================================================================
        function setJudgeStatusFilter(st) {
            filterStatus = st;
            document.querySelectorAll('.filter-pill').forEach(btn => btn.classList.remove('active'));
            if (st === 'all') document.getElementById('filterStatusAll').classList.add('active');
            else if (st === 'failed') document.getElementById('filterStatusFailed').classList.add('active');
            else if (st === 'passed') document.getElementById('filterStatusPassed').classList.add('active');
            else if (st === 'invalid') document.getElementById('filterStatusInvalid').classList.add('active');
            applyJudgeFilters();
        }

        function setJudgeCategoryFilter(cat) {
            filterCategory = cat;
            document.querySelectorAll('#filterCatAll, #filterCatFact, #filterCatMulti, #filterCatPrec, #filterCatRefusal').forEach(b => b.classList.remove('active'));
            if (cat === 'all') document.getElementById('filterCatAll').classList.add('active');
            else if (cat === '事实检索') document.getElementById('filterCatFact').classList.add('active');
            else if (cat === '多跳推理') document.getElementById('filterCatMulti').classList.add('active');
            else if (cat === '精度') document.getElementById('filterCatPrec').classList.add('active');
            else if (cat === '超纲') document.getElementById('filterCatRefusal').classList.add('active');
            applyJudgeFilters();
        }

        function toggleScoreSort() {
            const btn = document.getElementById('btnSortScore');
            if (scoreSortOrder === 'none') {
                scoreSortOrder = 'asc';
                btn.innerHTML = '<span>分值排序: 低 ➔ 高 (Bad Cases)</span>';
                btn.classList.add('active');
            } else if (scoreSortOrder === 'asc') {
                scoreSortOrder = 'desc';
                btn.innerHTML = '<span>分值排序: 高 ➔ 低</span>';
                btn.classList.add('active');
            } else {
                scoreSortOrder = 'none';
                btn.innerHTML = '<span>分值排序: 默认</span>';
                btn.classList.remove('active');
            }
            applyJudgeFilters();
        }

        function applyJudgeFilters() {
            if (!latestJudgeReport || !latestJudgeReport.case_results) return;
            const cases = latestJudgeReport.case_results;
            const query = (document.getElementById('judgeSearchInput')?.value || '').trim().toLowerCase();

            // 更新计数器
            const total = cases.length;
            const passed = cases.filter(c => c.is_valid !== false && c.is_passed).length;
            const invalid = cases.filter(c => c.is_valid === false).length;
            const failed = total - passed - invalid;
            document.getElementById('filterCountAll').textContent = total;
            document.getElementById('filterCountPassed').textContent = passed;
            document.getElementById('filterCountFailed').textContent = failed;
            document.getElementById('filterCountInvalid').textContent = invalid;

            let filtered = cases.filter(c => {
                const isValid = c.is_valid !== false;
                if (filterStatus === 'passed' && (!isValid || !c.is_passed)) return false;
                if (filterStatus === 'failed' && (!isValid || c.is_passed)) return false;
                if (filterStatus === 'invalid' && isValid) return false;
                if (filterCategory !== 'all' && !String(c.category || '').includes(filterCategory)) return false;
                if (query) {
                    const text = `${c.query || ''} ${c.ground_truth || ''} ${c.system_answer || ''} ${(c.diagnoses || []).join(' ')}`.toLowerCase();
                    if (!text.includes(query)) return false;
                }
                return true;
            });

            if (scoreSortOrder === 'asc') {
                filtered.sort((a, b) => (a.scores?.rag_triad_score || 0) - (b.scores?.rag_triad_score || 0));
            } else if (scoreSortOrder === 'desc') {
                filtered.sort((a, b) => (b.scores?.rag_triad_score || 0) - (a.scores?.rag_triad_score || 0));
            }

            renderJudgeResultsTable(filtered);
        }

        function renderJudgeResultsTable(cases) {
            const tbody = document.getElementById('judgeTableBody');
            if (!cases || cases.length === 0) {
                tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 40px;">未找到匹配的评测结果</td></tr>`;
                return;
            }

            tbody.innerHTML = cases.map((c, idx) => {
                const s = c.scores || {};
                const triad = ((s.rag_triad_score || 0) * 100).toFixed(1);
                const faith = Math.round((s.faithfulness || 0) * 100);
                const rel   = Math.round((s.answer_relevance || 0) * 100);
                const prec  = Math.round((s.context_precision || 0) * 100);
                const rec   = s.context_recall !== null ? Math.round((s.context_recall || 0) * 100) : null;
                const recStr = rec !== null ? `${rec}%` : '--';
                const isValid = c.is_valid !== false;
                const isPass = isValid && c.is_passed;
                const isJudgeFailure = !isValid && c.error_stage === 'judge';
                const rowClass = isPass ? 'row-passed' : 'row-failed';
                const statusBadge = isValid
                    ? (isPass
                        ? `<span class="status-badge passed">✅ 已通过</span>`
                        : `<span class="status-badge failed">❌ 未达标</span>`)
                    : `<span class="status-badge failed">${isJudgeFailure ? '⚖️ 裁判失败' : '⚠️ 系统故障'}</span>`;
                const scoreText = isValid ? triad : 'N/A';
                const metricHtml = isValid ? `
                            <div class="metric-mini-bar">
                                <div class="mini-bar-row"><span>忠实度:</span> <span>${faith}%</span></div>
                                <div class="mini-track"><div class="mini-fill fill-emerald" style="width: ${faith}%;"></div></div>
                                <div class="mini-bar-row"><span>相关度:</span> <span>${rel}%</span></div>
                                <div class="mini-track"><div class="mini-fill fill-cyan" style="width: ${rel}%;"></div></div>
                                <div class="mini-bar-row"><span>召回率:</span> <span>${recStr}</span></div>
                                <div class="mini-track"><div class="mini-fill fill-purple" style="width: ${rec || 0}%;"></div></div>
                            </div>` : '<span style="color: var(--amber); font-size: 0.78rem;">流程故障，本轮不计分</span>';

                const diagBrief = (c.diagnoses && c.diagnoses.length > 0)
                    ? escapeHtml(c.diagnoses[0])
                    : (isValid ? '运行稳定无异常' : `故障类型: ${c.error_type || 'unknown'}`);

                return `
                    <tr class="${rowClass}">
                        <td style="font-family: 'JetBrains Mono', monospace; font-weight: 700; color: var(--text-muted);">${c.id || idx + 1}</td>
                        <td>${getCategoryTagHtml(c.category)}</td>
                        <td>
                            <div style="font-weight: 600; color: #fff; margin-bottom: 4px;">${escapeHtml(c.query)}</div>
                            <div style="font-size: 0.72rem; color: var(--text-muted);">标准: ${escapeHtml((c.ground_truth || '').substring(0, 60))}...</div>
                        </td>
                        <td>
                            <span style="font-family: 'JetBrains Mono', monospace; font-size: 1.15rem; font-weight: 800; color: ${isPass ? 'var(--emerald)' : (isValid ? 'var(--rose)' : 'var(--amber)')};">${scoreText}</span>
                            <span style="font-size: 0.72rem; color: var(--text-muted);">${isValid ? '/ 100' : ''}</span>
                        </td>
                        <td>${metricHtml}</td>
                        <td>${statusBadge}</td>
                        <td>
                            <div class="diag-brief">${diagBrief}</div>
                        </td>
                        <td style="text-align: center;">
                            <button class="btn-inspect" onclick="openTraceModal(${idx})">
                                <span>🔍 透视详情</span>
                            </button>
                        </td>
                    </tr>
                `;
            }).join('');
        }

        // =====================================================================
        // 全链路单题 Trace & Diff 模态窗
        // =====================================================================
        let currentModalCase = null;

        function openTraceModal(filteredIndex) {
            if (!latestJudgeReport || !latestJudgeReport.case_results) return;
            const cases = latestJudgeReport.case_results;
            // 获取当前过滤后的用例对象
            const caseItem = cases[filteredIndex] || cases[0];
            if (!caseItem) return;
            currentModalCase = caseItem;

            document.getElementById('modalCaseTag').innerHTML = getCategoryTagHtml(caseItem.category);
            document.getElementById('modalCaseTitle').textContent = `测试用例 #${caseItem.id || filteredIndex + 1} 全链路深度溯源`;
            
            const modalValid = caseItem.is_valid !== false;
            const triad = modalValid ? ((caseItem.scores?.rag_triad_score || 0) * 100).toFixed(1) : 'N/A';
            const modalStatus = modalValid
                ? (caseItem.is_passed ? '已通过' : '未通过')
                : (caseItem.error_stage === 'judge' ? '裁判失败 · 未计分' : '系统故障 · 未计分');
            const scoreBadge = document.getElementById('modalScoreBadge');
            scoreBadge.textContent = `${triad}${modalValid ? ' 分' : ''} · ${modalStatus}`;
            scoreBadge.className = `status-badge ${modalValid && caseItem.is_passed ? 'passed' : 'failed'}`;

            document.getElementById('modalQueryText').textContent = caseItem.query || '';
            document.getElementById('modalSourceDoc').textContent = caseItem.source_doc || '知识库多文档联合检索';
            document.getElementById('modalQaLatency').textContent = `${caseItem.qa_latency || '--'}s`;
            document.getElementById('modalJudgeLatency').textContent = `${caseItem.judge_latency || '--'}s`;

            document.getElementById('modalExpectedAnswer').textContent = caseItem.ground_truth || caseItem.raw_ground_truth || '无标准答案标注';
            document.getElementById('modalActualAnswer').textContent = caseItem.system_answer || '未产生回答内容';

            // 诊断与处方
            const diagList = document.getElementById('modalDiagnosesList');
            if (caseItem.diagnoses && caseItem.diagnoses.length > 0) {
                diagList.innerHTML = caseItem.diagnoses.map(d => `<div style="margin-bottom: 6px;">• ${escapeHtml(d)}</div>`).join('');
            } else {
                diagList.innerHTML = '• 裁判模型检验未发现事实偏离或无中生有幻觉。';
            }

            const prescList = document.getElementById('modalPrescriptsList');
            if (caseItem.prescriptions && caseItem.prescriptions.length > 0) {
                prescList.innerHTML = caseItem.prescriptions.map(p => `<div style="margin-bottom: 6px;">✓ ${escapeHtml(p)}</div>`).join('');
            } else {
                prescList.innerHTML = '✓ 系统运行处于指标健康区间，可直接投入生产使用。';
            }

            // 检索切片预览
            const chunksBox = document.getElementById('modalChunksList');
            document.getElementById('modalChunksCountBadge').textContent = `召回切片: ${caseItem.contexts_count || 0} 个`;
            if (caseItem.contexts_preview) {
                chunksBox.innerHTML = `
                    <div class="chunk-item-card" style="font-family: 'JetBrains Mono', monospace; font-size: 0.76rem; color: #cbd5e1; white-space: pre-wrap;">
                        ${escapeHtml(caseItem.contexts_preview)}
                    </div>
                `;
            } else {
                chunksBox.innerHTML = '<div style="color: var(--text-muted); font-size: 0.78rem; padding: 10px 0;">该提问经过第一层初筛距离截断与第二层百炼重排，所有切片均未达标，坚决触发【诚实拒答机制】。</div>';
            }

            document.getElementById('traceModalOverlay').style.display = 'flex';
        }

        function closeTraceModal() {
            document.getElementById('traceModalOverlay').style.display = 'none';
        }

        function closeTraceModalOnBackdrop(e) {
            if (e.target.id === 'traceModalOverlay') {
                closeTraceModal();
            }
        }

        // =====================================================================
        // 一键全自动化运行全量评测 (Run Full Pipeline)
        // =====================================================================
        async function runFullPipelineAutomated() {
            const btn = document.getElementById('btnRunFullPipeline');
            btn.disabled = true;
            btn.innerHTML = '<span>⏳ 正在全自动运行流水线...</span>';

            showToast('已启动全自动化全链路评测流水线');
            switchStep(3);

            // 自动启动作答
            await startRunAnswers();
            // 自动启动裁判打分
            await startJudgeEvaluation();

            btn.disabled = false;
            btn.innerHTML = '<span>⚡ 一键全自动化运行全量评测</span>';
        }

        async function exportEvaluationExcel() {
            const btnDownloadTop = document.getElementById('btnDownloadReportTop');
            const originalTopHtml = btnDownloadTop ? btnDownloadTop.innerHTML : '';
            
            try {
                if (btnDownloadTop) {
                    btnDownloadTop.innerHTML = '<span>⏳ 正在同步生成最新报表...</span>';
                    btnDownloadTop.disabled = true;
                }
                showToast('正在实时编译生成最新 21 列量化评测报表...');

                // 如果当前前端内存中持有最新的评测报告数据，先执行即时同步写盘
                if (latestJudgeReport && latestJudgeReport.case_results && latestJudgeReport.case_results.length > 0) {
                    try {
                        const syncRes = await fetch('/api/benchmark/sync-and-export', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ summary: latestJudgeReport })
                        });
                        const syncData = await syncRes.json();
                        if (syncData.code === 200) {
                            console.log('✅ 前端最新评测数据已成功同步至后端磁盘 Excel');
                        } else {
                            console.warn('⚠️ 同步报表后端提示:', syncData.message);
                        }
                    } catch (syncErr) {
                        console.warn('⚠️ 即时同步接口调用告警 (将直接下载磁盘最新副本):', syncErr);
                    }
                }

                // 采用附加时间戳参数与动态无缓存下载机制，确保 100% 绕过任何本地缓存
                const downloadUrl = `/api/benchmark/download-report?t=${Date.now()}`;
                
                // 采用动态隐藏 a 标签下载，避免页面跳转闪烁
                const a = document.createElement('a');
                a.href = downloadUrl;
                a.download = '';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);

                showToast('✅ 最新 21 列评测报表 (XLSX) 已启动下载');
            } catch (e) {
                console.error('导出 Excel 发生异常:', e);
                window.location.href = `/api/benchmark/download-report?t=${Date.now()}`;
            } finally {
                if (btnDownloadTop) {
                    btnDownloadTop.innerHTML = originalTopHtml;
                    btnDownloadTop.disabled = false;
                }
            }
        }

        // =====================================================================
        // 工具辅助函数
        // =====================================================================
        function getCategoryTagHtml(cat) {
            const c = String(cat || '');
            if (c.includes('多跳') || c.includes('推理')) return `<span class="cat-tag cat-multi-hop">多跳推理</span>`;
            if (c.includes('精度') || c.includes('高噪')) return `<span class="cat-tag cat-precision">高噪辨析</span>`;
            if (c.includes('否定') || c.includes('超纲') || c.includes('边界') || c.includes('拒答')) return `<span class="cat-tag cat-refusal">边界拒答</span>`;
            return `<span class="cat-tag cat-single-hop">事实检索</span>`;
        }

        function escapeHtml(str) {
            if (!str) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }

        function showToast(msg) {
            const toast = document.getElementById('toastBox');
            toast.textContent = msg;
            toast.style.display = 'block';
            setTimeout(() => { toast.style.display = 'none'; }, 3200);
        }
