<template>
  <div class="h-full overflow-y-auto bg-canvas">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 py-6 space-y-5">
      <!-- 顶部控制中心卡片（清爽双层结构，告别拥挤） -->
      <section class="bg-surface border border-line rounded-2xl p-4 sm:p-5 shadow-card space-y-4">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="space-y-1">
            <div class="flex items-center gap-2">
              <h1 class="text-base font-semibold text-gray-900">Ragas 自动化基准评测中枢</h1>
              <span
                v-if="checkpointStatus.can_resume"
                class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-50 border border-amber-200 text-amber-800 text-[11px] font-medium whitespace-nowrap"
              >
                可续跑缓存: 作答 {{ checkpointStatus.answers_checkpoint_count }} / 裁判 {{ checkpointStatus.judge_checkpoint_count }}
              </span>
            </div>
            <div class="flex flex-wrap items-center gap-1.5 text-xs text-muted">
              <span class="px-2 py-0.5 rounded bg-subtle border border-line font-mono text-[11px]">
                出题: {{ config.generator_model }}
              </span>
              <span>→</span>
              <span class="px-2 py-0.5 rounded bg-subtle border border-line font-mono text-[11px]">
                作答: {{ config.answer_model }} ({{ config.answer_concurrency }}并发)
              </span>
              <span>→</span>
              <span class="px-2 py-0.5 rounded bg-subtle border border-line font-mono text-[11px]">
                重排: {{ config.rerank_model }}
              </span>
              <span>→</span>
              <span class="px-2 py-0.5 rounded bg-subtle border border-line font-mono text-[11px]">
                裁判: {{ config.judge_model }} ({{ config.judge_concurrency }}并发)
              </span>
            </div>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <!-- 范围选择 -->
            <div class="inline-flex items-center bg-subtle p-1 rounded-xl border border-line text-xs">
              <button
                v-for="opt in limitOptions"
                :key="opt.value"
                @click="selectedLimit = opt.value"
                class="px-2.5 py-1 rounded-lg font-medium transition-all whitespace-nowrap"
                :class="selectedLimit === opt.value ? 'bg-surface text-gray-900 shadow-sm' : 'text-muted hover:text-ink'"
              >
                {{ opt.label }}
              </button>
            </div>

            <!-- 断点与复用勾选 -->
            <div class="inline-flex items-center gap-3 px-3 py-1.5 rounded-xl bg-subtle border border-line text-xs text-ink">
              <label class="inline-flex items-center gap-1.5 cursor-pointer whitespace-nowrap">
                <input v-model="resumeFlag" type="checkbox" class="rounded accent-brand" />
                <span>断点续跑</span>
              </label>
              <label class="inline-flex items-center gap-1.5 cursor-pointer whitespace-nowrap">
                <input v-model="reuseAnswersFlag" type="checkbox" class="rounded accent-brand" />
                <span>复用作答重评</span>
              </label>
            </div>

            <button
              @click="exportExcelReport"
              class="inline-flex items-center gap-1.5 px-3 py-2 rounded-xl bg-surface border border-line hover:bg-subtle text-xs font-medium text-ink transition-colors whitespace-nowrap"
            >
              <Download class="w-3.5 h-3.5" />
              <span>导出报表 (XLSX)</span>
            </button>

            <button
              @click="runFullPipeline"
              :disabled="runningPipeline"
              class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-xl bg-brand hover:bg-brand-hover disabled:opacity-60 text-white text-xs font-medium transition-colors shadow-sm whitespace-nowrap"
            >
              <Loader2 v-if="runningPipeline" class="w-3.5 h-3.5 animate-spin" />
              <Zap v-else class="w-3.5 h-3.5" />
              <span>{{ runningPipeline ? '双流水线并发评测中...' : '一键双流水线并发评测' }}</span>
            </button>
          </div>
        </div>

        <!-- 5 阶段切换栏 -->
        <div class="grid grid-cols-2 sm:grid-cols-5 gap-2 pt-2 border-t border-line">
          <button
            v-for="step in stepTabs"
            :key="step.id"
            @click="activeStep = step.id"
            class="flex items-center gap-2.5 px-3 py-2 rounded-xl border text-left transition-all"
            :class="activeStep === step.id
              ? 'bg-brand-soft/70 border-brand/40 text-brand'
              : 'bg-canvas border-line text-muted hover:text-ink hover:bg-subtle'"
          >
            <span
              class="w-5 h-5 rounded-md text-[11px] font-semibold flex items-center justify-center shrink-0"
              :class="activeStep === step.id ? 'bg-brand text-white' : 'bg-surface border border-line text-gray-600'"
            >
              {{ step.id }}
            </span>
            <div class="min-w-0">
              <div class="text-xs font-semibold truncate">{{ step.title }}</div>
              <div class="text-[10px] opacity-80 truncate">{{ step.desc }}</div>
            </div>
          </button>
        </div>
      </section>

      <!-- 配额熔断醒目告警 -->
      <div
        v-if="quotaAbortNotice"
        class="rounded-2xl bg-amber-50 border border-amber-200 px-4 py-3 flex items-start gap-2.5 text-xs text-amber-900"
      >
        <AlertTriangle class="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
        <div class="space-y-1">
          <div class="font-semibold">触发配额熔断保护（已安全保存全部已完成进度）</div>
          <div>{{ quotaAbortNotice }}</div>
        </div>
      </div>

      <!-- 阶段 1：靶向出题 -->
      <section v-show="activeStep === 1" class="bg-surface border border-line rounded-2xl p-5 shadow-card space-y-4">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 class="text-sm font-semibold text-gray-900">阶段一：靶向命题与基准装载</h2>
            <p class="text-xs text-muted mt-0.5">
              采用「全库前后段覆盖 (76%) + 真实双文档跨跳 (24%)」混合出题，由 <strong>{{ selectedGeneratorModel }}</strong> 并发派生考题。
            </p>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <select
              v-model="selectedGeneratorModel"
              class="px-2.5 py-1.5 rounded-xl bg-subtle border border-line text-xs font-mono text-ink focus:outline-none focus:border-brand/60"
              title="选择出题模型"
            >
              <option value="glm-5.2">出题: glm-5.2</option>
              <option value="qwen3.6-plus-2026-04-02">出题: qwen3.6-plus-2026-04-02</option>
              <option value="qwen3.8-max-0902">出题: qwen3.8-max-0902</option>
            </select>
            <div class="inline-flex items-center gap-1 bg-subtle p-1 rounded-xl border border-line text-xs">
              <button
                v-for="cnt in [5, 10, 20, 50, 100]"
                :key="cnt"
                @click="generateCount = cnt"
                class="px-2.5 py-1 rounded-lg font-medium whitespace-nowrap"
                :class="generateCount === cnt ? 'bg-surface text-gray-900 shadow-sm' : 'text-muted'"
              >
                {{ cnt }} 题
              </button>
              <input
                v-model.number="generateCount"
                type="number"
                min="1"
                max="200"
                class="w-14 px-2 py-1 rounded-lg bg-surface border border-line text-center font-mono text-xs"
              />
            </div>

            <label
              class="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-xl bg-subtle border border-line text-xs text-ink cursor-pointer whitespace-nowrap"
              title="勾选后将保留当前已装载题库，仅针对未覆盖文档与跨文档组合增量补齐至目标题数"
            >
              <input v-model="appendExisting" type="checkbox" class="rounded accent-brand" />
              <span>增量补齐已有题库</span>
            </label>

            <button
              @click="generateCases('from-guide')"
              :disabled="generatingCases"
              class="px-3 py-2 rounded-xl bg-surface border border-line hover:bg-subtle text-xs font-medium text-ink whitespace-nowrap"
            >
              载入指南 {{ generateCount }} 题
            </button>
            <button
              @click="generateCases('from-docs')"
              :disabled="generatingCases"
              class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-xl bg-brand hover:bg-brand-hover text-white text-xs font-medium whitespace-nowrap"
            >
              <Loader2 v-if="generatingCases" class="w-3.5 h-3.5 animate-spin" />
              <span>基于知识库出 {{ generateCount }} 题</span>
            </button>
          </div>
        </div>

        <div class="rounded-xl bg-canvas border border-line p-4 text-xs text-muted space-y-2.5">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div>当前已装载题库数：<strong class="text-gray-900 font-mono">{{ casesList.length }}</strong> 题</div>
            <div v-if="generateStatusMsg" class="text-brand font-medium">{{ generateStatusMsg }}</div>
          </div>

          <div v-if="generatingCases || genLogs.length > 0" class="pt-2 border-t border-line/80 space-y-2">
            <div class="flex flex-wrap items-center justify-between gap-2 text-[11px]">
              <span class="text-ink font-medium">{{ genProgressText }}</span>
              <span class="font-mono font-semibold text-gray-900">
                已生成 {{ genGeneratedCases }} / {{ genTargetCount }} 题 ({{ genPercent }}%)
              </span>
            </div>
            <div class="h-2 rounded-full bg-subtle overflow-hidden">
              <div class="h-full bg-brand transition-all duration-300" :style="{ width: genPercent + '%' }"></div>
            </div>
            <div v-if="genLogs.length > 0" class="max-h-48 overflow-y-auto space-y-1 text-[11px] pr-1">
              <div
                v-for="(log, i) in genLogs"
                :key="i"
                class="px-2.5 py-1.5 rounded-lg border"
                :class="log.ok ? 'bg-surface border-line text-gray-700' : 'bg-amber-50 border-amber-200 text-amber-800'"
              >
                {{ log.text }}
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- 阶段 2：题库审题编辑 -->
      <section v-show="activeStep === 2" class="bg-surface border border-line rounded-2xl shadow-card overflow-hidden">
        <div class="px-5 py-4 border-b border-line flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 class="text-sm font-semibold text-gray-900">阶段二：黄金题库审查与在线编辑</h2>
            <p class="text-xs text-muted mt-0.5">支持直接修改提问、标准答案与审核状态，剔除题将自动跳过评测。</p>
          </div>
          <div class="flex items-center gap-2">
            <button
              @click="addEmptyCase"
              class="px-3 py-1.5 rounded-lg bg-surface border border-line hover:bg-subtle text-xs font-medium whitespace-nowrap"
            >
              + 新增题目
            </button>
            <a
              :href="evalUrl('/api/benchmark/export-cases')"
              target="_blank"
              class="px-3 py-1.5 rounded-lg bg-surface border border-line hover:bg-subtle text-xs font-medium whitespace-nowrap"
            >
              导出题库 XLSX
            </a>
            <button
              @click="saveCases"
              class="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-medium whitespace-nowrap"
            >
              保存修改
            </button>
          </div>
        </div>

        <div class="max-h-[520px] overflow-auto">
          <table class="w-full text-left border-collapse text-xs">
            <thead class="sticky top-0 bg-[#F9FAFB] border-b border-line text-muted">
              <tr>
                <th class="py-2.5 px-3 w-12">#</th>
                <th class="py-2.5 px-3 w-36">题型</th>
                <th class="py-2.5 px-3 min-w-[220px]">测试提问 (Query)</th>
                <th class="py-2.5 px-3 min-w-[240px]">标准答案 (Ground Truth)</th>
                <th class="py-2.5 px-3 w-28">审核状态</th>
                <th class="py-2.5 px-3 w-16 text-right">操作</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-line/80">
              <tr v-for="(c, idx) in casesList" :key="idx" class="align-top hover:bg-canvas">
                <td class="py-2.5 px-3 font-mono text-muted">{{ c.id || idx + 1 }}</td>
                <td class="py-2.5 px-3">
                  <input v-model="c.category" class="w-full px-2 py-1 rounded border border-line bg-surface text-xs" />
                </td>
                <td class="py-2.5 px-3">
                  <textarea v-model="c.query" rows="2" class="w-full px-2 py-1 rounded border border-line bg-surface text-xs"></textarea>
                </td>
                <td class="py-2.5 px-3">
                  <textarea v-model="c.ground_truth" rows="2" class="w-full px-2 py-1 rounded border border-line bg-surface text-xs"></textarea>
                </td>
                <td class="py-2.5 px-3">
                  <select v-model="c.review_status" class="w-full px-2 py-1 rounded border border-line bg-surface text-xs">
                    <option value="通过">通过</option>
                    <option value="已修正">已修正</option>
                    <option value="剔除">剔除</option>
                  </select>
                </td>
                <td class="py-2.5 px-3 text-right">
                  <button
                    @click="casesList.splice(idx, 1)"
                    class="p-1 rounded hover:bg-rose-50 text-muted hover:text-rose-600"
                    title="移除"
                  >
                    <Trash2 class="w-3.5 h-3.5" />
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- 阶段 3 & 4：实时双流水线进度与作答列表 -->
      <section v-show="activeStep === 3 || activeStep === 4" class="space-y-4">
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <!-- 作答进度卡 -->
          <div class="bg-surface border border-line rounded-2xl p-4 shadow-card space-y-3">
            <div class="flex items-center justify-between">
              <div class="text-xs font-semibold text-gray-900">
                阶段三：考生系统并发作答 ({{ config.answer_model }})
              </div>
              <button
                @click="runAnswersOnly"
                :disabled="runningAnswers || runningPipeline"
                class="px-2.5 py-1 rounded-lg bg-subtle border border-line hover:bg-gray-200/70 text-[11px] font-medium whitespace-nowrap"
              >
                仅跑作答阶段
              </button>
            </div>
            <div class="space-y-1.5">
              <div class="flex justify-between text-[11px] text-muted">
                <span>{{ answerProgressText }}</span>
                <span class="font-mono font-semibold text-gray-900">{{ answerPercent }}%</span>
              </div>
              <div class="h-2 rounded-full bg-subtle overflow-hidden">
                <div class="h-full bg-brand transition-all duration-300" :style="{ width: answerPercent + '%' }"></div>
              </div>
            </div>
            <div class="max-h-40 overflow-y-auto space-y-1 text-[11px]">
              <div
                v-for="(log, i) in answerLogs"
                :key="i"
                class="px-2.5 py-1.5 rounded-lg border"
                :class="log.ok ? 'bg-canvas border-line text-gray-700' : 'bg-rose-50 border-rose-200 text-rose-800'"
              >
                {{ log.text }}
              </div>
            </div>
          </div>

          <!-- 裁判进度卡 -->
          <div class="bg-surface border border-line rounded-2xl p-4 shadow-card space-y-3">
            <div class="flex items-center justify-between">
              <div class="text-xs font-semibold text-gray-900">
                阶段四：Ragas 独立裁判打分 ({{ config.judge_model }})
              </div>
              <button
                @click="runJudgeOnly"
                :disabled="runningJudge || runningPipeline"
                class="px-2.5 py-1 rounded-lg bg-subtle border border-line hover:bg-gray-200/70 text-[11px] font-medium whitespace-nowrap"
              >
                仅跑裁判打分
              </button>
            </div>
            <div class="space-y-1.5">
              <div class="flex justify-between text-[11px] text-muted">
                <span>{{ judgeProgressText }}</span>
                <span class="font-mono font-semibold text-gray-900">{{ judgePercent }}%</span>
              </div>
              <div class="h-2 rounded-full bg-subtle overflow-hidden">
                <div class="h-full bg-emerald-600 transition-all duration-300" :style="{ width: judgePercent + '%' }"></div>
              </div>
            </div>
            <div class="max-h-40 overflow-y-auto space-y-1 text-[11px]">
              <div
                v-for="(log, i) in judgeLogs"
                :key="i"
                class="px-2.5 py-1.5 rounded-lg border"
                :class="log.ok ? 'bg-canvas border-line text-gray-700' : 'bg-rose-50 border-rose-200 text-rose-800'"
              >
                {{ log.text }}
              </div>
            </div>
          </div>
        </div>

        <!-- 实时作答结果预览表 -->
        <div class="bg-surface border border-line rounded-2xl shadow-card overflow-hidden">
          <div class="px-5 py-3 border-b border-line flex items-center justify-between">
            <span class="text-xs font-semibold text-gray-900">作答实况记录 ({{ answeredCases.length }} 题)</span>
          </div>
          <div class="max-h-[420px] overflow-auto">
            <table class="w-full text-left border-collapse text-xs">
              <thead class="sticky top-0 bg-[#F9FAFB] border-b border-line text-muted">
                <tr>
                  <th class="py-2.5 px-3 w-12">#</th>
                  <th class="py-2.5 px-3 w-56">提问</th>
                  <th class="py-2.5 px-3">系统回答</th>
                  <th class="py-2.5 px-3 w-20 whitespace-nowrap">切片数</th>
                  <th class="py-2.5 px-3 w-20 whitespace-nowrap">耗时</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-line/80">
                <tr v-for="(item, idx) in answeredCases" :key="idx" class="hover:bg-canvas align-top">
                  <td class="py-2.5 px-3 font-mono text-muted">{{ item.id || idx + 1 }}</td>
                  <td class="py-2.5 px-3 text-gray-900 font-medium">{{ item.query }}</td>
                  <td class="py-2.5 px-3 text-gray-700 leading-relaxed line-clamp-3">{{ item.system_answer || item.answer }}</td>
                  <td class="py-2.5 px-3 font-mono whitespace-nowrap">{{ item.contexts_count ?? (item.contexts?.length || 0) }}</td>
                  <td class="py-2.5 px-3 font-mono whitespace-nowrap">{{ item.qa_latency || item.latency || 0 }}s</td>
                </tr>
                <tr v-if="answeredCases.length === 0">
                  <td colspan="5" class="py-10 text-center text-muted">尚未生成作答记录，请点击右上角「一键双流水线并发评测」</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- 阶段 5：全维透析与归因看板 -->
      <section v-show="activeStep === 5" class="space-y-4">
        <div v-if="!latestReport" class="bg-surface border border-line rounded-2xl p-10 text-center text-xs text-muted shadow-card">
          暂无评测报告，请先执行一轮评测或加载最新结果。
        </div>

        <template v-else>
          <!-- 5 维 Ragas 核心指标卡 -->
          <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3.5">
            <div
              v-for="metric in metricCards"
              :key="metric.key"
              class="bg-surface border border-line rounded-2xl p-4 shadow-card space-y-2"
            >
              <div class="flex items-center justify-between text-xs text-muted">
                <span>{{ metric.label }}</span>
                <span
                  class="px-1.5 py-0.5 rounded text-[10px] font-medium"
                  :class="metric.value >= 0.85 ? 'bg-emerald-50 text-emerald-700' : (metric.value >= 0.70 ? 'bg-amber-50 text-amber-700' : 'bg-rose-50 text-rose-700')"
                >
                  {{ metric.value >= 0.85 ? '卓越' : (metric.value >= 0.70 ? '达标' : '需优化') }}
                </span>
              </div>
              <div class="text-2xl font-semibold font-mono text-gray-900">{{ metric.value.toFixed(4) }}</div>
              <div class="h-1.5 rounded-full bg-subtle overflow-hidden">
                <div
                  class="h-full transition-all"
                  :class="metric.value >= 0.85 ? 'bg-emerald-600' : (metric.value >= 0.70 ? 'bg-amber-500' : 'bg-rose-500')"
                  :style="{ width: Math.min(100, metric.value * 100) + '%' }"
                ></div>
              </div>
            </div>
          </div>

          <!-- 宏观汇总条 -->
          <div class="bg-surface border border-line rounded-2xl px-5 py-3.5 shadow-card flex flex-wrap items-center justify-between gap-4 text-xs">
            <div class="flex flex-wrap items-center gap-4">
              <span>总题数: <strong class="font-mono text-gray-900">{{ latestReport.total_cases || 0 }}</strong></span>
              <span>有效样本: <strong class="font-mono text-gray-900">{{ latestReport.valid_cases ?? latestReport.total_cases ?? 0 }}</strong></span>
              <span>通过题数: <strong class="font-mono text-emerald-700">{{ latestReport.passed_cases || 0 }}</strong></span>
              <span>有效通过率: <strong class="font-mono text-brand">{{ ((latestReport.valid_pass_rate ?? latestReport.pass_rate ?? 0) * 100).toFixed(1) }}%</strong></span>
              <span>总耗时: <strong class="font-mono text-gray-900">{{ latestReport.performance?.total_elapsed_seconds ?? latestReport.elapsed_seconds ?? 0 }}s</strong></span>
            </div>
            <div class="text-muted font-mono text-[11px]">
              {{ latestReport.timestamp || '--' }} | {{ latestReport.model_b_system }} ➔ {{ latestReport.model_c_judge }}
            </div>
          </div>

          <!-- 逐题诊断与对比详情 -->
          <div class="bg-surface border border-line rounded-2xl shadow-card overflow-hidden">
            <div class="px-5 py-3.5 border-b border-line flex items-center justify-between">
              <h3 class="text-sm font-semibold text-gray-900">逐题量化指标与归因诊断</h3>
              <span class="text-[11px] text-muted">点击任意行可展开对照详情与优化处方</span>
            </div>
            <div class="max-h-[520px] overflow-auto">
              <table class="w-full text-left border-collapse text-xs">
                <thead class="sticky top-0 bg-[#F9FAFB] border-b border-line text-muted">
                  <tr>
                    <th class="py-2.5 px-3 w-12">#</th>
                    <th class="py-2.5 px-3 w-36">题型</th>
                    <th class="py-2.5 px-3">测试提问</th>
                    <th class="py-2.5 px-3 w-20 whitespace-nowrap">三元组</th>
                    <th class="py-2.5 px-3 w-16 whitespace-nowrap">忠实</th>
                    <th class="py-2.5 px-3 w-16 whitespace-nowrap">相关</th>
                    <th class="py-2.5 px-3 w-16 whitespace-nowrap">精度</th>
                    <th class="py-2.5 px-3 w-16 whitespace-nowrap">召回</th>
                    <th class="py-2.5 px-3 w-20 whitespace-nowrap">判定</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-line/80">
                  <template v-for="(row, idx) in (latestReport.case_results || [])" :key="row.id || idx">
                    <tr
                      @click="toggleRowExpand(row.id || idx)"
                      class="hover:bg-canvas cursor-pointer transition-colors"
                    >
                      <td class="py-2.5 px-3 font-mono text-muted">{{ row.id || idx + 1 }}</td>
                      <td class="py-2.5 px-3 text-muted truncate max-w-[140px]">{{ row.category }}</td>
                      <td class="py-2.5 px-3 text-gray-900 font-medium">{{ row.query }}</td>
                      <td class="py-2.5 px-3 font-mono font-semibold">{{ fmtScore(row.scores?.rag_triad_score) }}</td>
                      <td class="py-2.5 px-3 font-mono">{{ fmtScore(row.scores?.faithfulness) }}</td>
                      <td class="py-2.5 px-3 font-mono">{{ fmtScore(row.scores?.answer_relevance) }}</td>
                      <td class="py-2.5 px-3 font-mono">{{ fmtScore(row.scores?.context_precision) }}</td>
                      <td class="py-2.5 px-3 font-mono">{{ fmtScore(row.scores?.context_recall) }}</td>
                      <td class="py-2.5 px-3 whitespace-nowrap">
                        <span
                          class="inline-flex px-2 py-0.5 rounded-md text-[11px] font-medium"
                          :class="row.is_valid === false
                            ? 'bg-amber-50 text-amber-800 border border-amber-200'
                            : (row.is_passed
                              ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
                              : 'bg-rose-50 text-rose-700 border border-rose-200')"
                        >
                          {{ row.is_valid === false ? '流程故障' : (row.is_passed ? '通过' : '未达标') }}
                        </span>
                      </td>
                    </tr>
                    <tr v-if="expandedRows[row.id || idx]" class="bg-canvas">
                      <td colspan="9" class="p-4 border-t border-line/60">
                        <div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
                          <div class="bg-surface border border-line rounded-xl p-3 space-y-1">
                            <div class="font-semibold text-gray-900">黄金标准答案</div>
                            <div class="text-gray-700 whitespace-pre-wrap leading-relaxed">{{ row.ground_truth }}</div>
                          </div>
                          <div class="bg-surface border border-line rounded-xl p-3 space-y-1">
                            <div class="font-semibold text-gray-900">系统实际回答</div>
                            <div class="text-gray-700 whitespace-pre-wrap leading-relaxed">{{ row.system_answer }}</div>
                          </div>
                          <div class="bg-surface border border-line rounded-xl p-3 space-y-1 md:col-span-2">
                            <div class="font-semibold text-gray-900">归因诊断与优化处方</div>
                            <div class="text-gray-700 whitespace-pre-wrap">{{ (row.diagnoses || []).join('\n') }}</div>
                            <div v-if="(row.prescriptions || []).length" class="text-brand mt-1 whitespace-pre-wrap">
                              建议: {{ row.prescriptions.join('；') }}
                            </div>
                          </div>
                        </div>
                      </td>
                    </tr>
                  </template>
                </tbody>
              </table>
            </div>
          </div>
        </template>
      </section>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref, computed, onMounted } from 'vue'
import { Download, Loader2, Zap, AlertTriangle, Trash2 } from 'lucide-vue-next'
import { evalUrl, requestJson } from '../api/client'
import { streamSSEPost } from '../api/sse'
import { useToast } from '../composables/useToast'

const { showToast } = useToast()

const config = reactive({
  generator_model: 'glm-5.2',
  answer_model: 'qwen3.6-flash-2026-04-16',
  judge_model: 'kimi-k3',
  rerank_model: 'gte-rerank-v2',
  answer_concurrency: 8,
  judge_concurrency: 10
})

const checkpointStatus = reactive({
  answers_checkpoint_count: 0,
  judge_checkpoint_count: 0,
  latest_answers_count: 0,
  can_resume: false,
  can_reuse_answers: false
})

const limitOptions = [
  { label: '前 3 题', value: 3 },
  { label: '前 5 题', value: 5 },
  { label: '全部题目', value: 0 }
]

const stepTabs = [
  { id: 1, title: '自动出题', desc: '基于真实知识库' },
  { id: 2, title: '题库审核', desc: '在线编辑与导出' },
  { id: 3, title: '并发作答', desc: '双层漏斗检索' },
  { id: 4, title: '裁判打分', desc: 'Ragas 量化裁决' },
  { id: 5, title: '诊断看板', desc: '全维指标与处方' }
]

const activeStep = ref(5)
const selectedLimit = ref(0)
const resumeFlag = ref(false)
const reuseAnswersFlag = ref(false)

const casesList = ref([])
const generateCount = ref(10)
const selectedGeneratorModel = ref('glm-5.2')
const appendExisting = ref(false)
const generatingCases = ref(false)
const generateStatusMsg = ref('')
const genPercent = ref(0)
const genGeneratedCases = ref(0)
const genTargetCount = ref(0)
const genProgressText = ref('')
const genLogs = ref([])

const answeredCases = ref([])
const latestReport = ref(null)
const expandedRows = reactive({})

const runningPipeline = ref(false)
const runningAnswers = ref(false)
const runningJudge = ref(false)
const quotaAbortNotice = ref('')

const answerPercent = ref(0)
const answerProgressText = ref('等待启动...')
const answerLogs = ref([])

const judgePercent = ref(0)
const judgeProgressText = ref('等待启动...')
const judgeLogs = ref([])

const metricCards = computed(() => {
  const avg = latestReport.value?.average_scores || {}
  return [
    { key: 'rag_triad_score', label: '三元组综合分', value: Number(avg.rag_triad_score || 0) },
    { key: 'faithfulness', label: '事实忠实度', value: Number(avg.faithfulness || 0) },
    { key: 'answer_relevance', label: '答案相关性', value: Number(avg.answer_relevance || 0) },
    { key: 'context_precision', label: '上下文精确度', value: Number(avg.context_precision || 0) },
    { key: 'context_recall', label: '上下文召回率', value: Number(avg.context_recall ?? 0) }
  ]
})

function fmtScore(val) {
  if (val === null || val === undefined) return 'N/A'
  return Number(val).toFixed(2)
}

function toggleRowExpand(key) {
  expandedRows[key] = !expandedRows[key]
}

async function loadInitialData() {
  await Promise.allSettled([
    loadConfig(),
    loadCheckpointStatus(),
    loadCases(),
    loadLatestAnswers(),
    loadLatestReport()
  ])
  if (!latestReport.value) {
    activeStep.value = 1
  }
}

async function loadConfig() {
  try {
    const res = await requestJson(evalUrl('/api/benchmark/config'))
    if (res.code === 200 && res.data) {
      Object.assign(config, res.data)
      if (res.data.generator_model) {
        selectedGeneratorModel.value = res.data.generator_model
      }
    }
  } catch (err) {
    console.warn('loadConfig failed:', err)
  }
}

async function loadCheckpointStatus() {
  try {
    const res = await requestJson(evalUrl('/api/benchmark/checkpoint-status'))
    if (res.code === 200 && res.data) {
      Object.assign(checkpointStatus, res.data)
    }
  } catch (err) {
    console.warn('loadCheckpointStatus failed:', err)
  }
}

async function loadCases() {
  try {
    const res = await requestJson(evalUrl('/api/benchmark/cases'))
    if (res.code === 200 && Array.isArray(res.data)) {
      casesList.value = res.data
    }
  } catch (err) {
    console.warn('loadCases failed:', err)
  }
}

async function loadLatestAnswers() {
  try {
    const res = await requestJson(evalUrl('/api/benchmark/latest-answers'))
    if (res.code === 200 && Array.isArray(res.data)) {
      answeredCases.value = res.data
    }
  } catch {
    // 暂无作答记录属正常状态
  }
}

async function loadLatestReport() {
  try {
    const res = await requestJson(evalUrl('/api/eval/latest-report'))
    if (res.code === 200 && res.data) {
      latestReport.value = res.data.summary || res.data
    }
  } catch {
    // 暂无报告属正常状态
  }
}

async function generateCases(mode) {
  if (generatingCases.value) return
  generatingCases.value = true
  const targetCnt = Number(generateCount.value || 10)
  genPercent.value = 0
  genGeneratedCases.value = mode === 'from-docs' && appendExisting.value ? casesList.value.length : 0
  genTargetCount.value = targetCnt
  genLogs.value = []

  if (mode === 'from-guide') {
    generateStatusMsg.value = '正在载入基准题目...'
    genProgressText.value = '正在解析基准指南...'
    try {
      const res = await requestJson(evalUrl('/api/benchmark/generate-cases'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode,
          model_a: selectedGeneratorModel.value || config.generator_model,
          case_count: targetCnt
        })
      })
      if (res.code === 200 && Array.isArray(res.data)) {
        casesList.value = res.data
        genPercent.value = 100
        genGeneratedCases.value = res.data.length
        generateStatusMsg.value = res.message || `已就绪 ${res.data.length} 题`
        genProgressText.value = '指南题库装载完成'
        showToast(generateStatusMsg.value, 'success')
        activeStep.value = 2
      }
    } catch (err) {
      showToast(`出题失败: ${err.message}`, 'error')
    } finally {
      generatingCases.value = false
    }
    return
  }

  const activeModel = selectedGeneratorModel.value || config.generator_model
  generateStatusMsg.value = appendExisting.value
    ? `正在保留现有 ${casesList.value.length} 题，由 ${activeModel} 增量补齐至 ${targetCnt} 题...`
    : `正在由 ${activeModel} 并发执行全库前后段 + 跨文档联合出题...`
  genProgressText.value = '正在初始化出题批次...'

  let streamErrMsg = ''
  try {
    await streamSSEPost(
      evalUrl('/api/benchmark/generate-cases-stream'),
      {
        mode,
        model_a: activeModel,
        case_count: targetCnt,
        append_existing: appendExisting.value
      },
      {
        meta: (d) => {
          genTargetCount.value = d.target_count || targetCnt
          genGeneratedCases.value = d.existing_count ?? genGeneratedCases.value
          if (d.is_backfill) {
            genProgressText.value = `第 ${d.round - 1} 轮缺口自动补齐中 (累计总批次 ${d.total_batches})...`
          } else {
            genProgressText.value = `并发池 (${d.workers || 8}并发) · 共 ${d.total_batches || 0} 批 · 可用文档 ${d.available_docs || 0} 篇`
          }
        },
        batch_done: (d) => {
          genTargetCount.value = d.target_count || targetCnt
          genGeneratedCases.value = d.generated_cases ?? genGeneratedCases.value
          genPercent.value = d.total_batches ? Math.min(99, Math.round((d.completed / d.total_batches) * 100)) : 99
          genProgressText.value = `批次 ${d.completed}/${d.total_batches} · 已产出 ${genGeneratedCases.value}/${genTargetCount.value} 题`
          genLogs.value.unshift({
            ok: d.ok !== false,
            text: `[${d.completed}/${d.total_batches}] ${d.ok !== false ? '✅' : '⚠️'} ${d.label} (${d.added > 0 ? '+' + d.added + '题' : '无有效题'})`
          })
        },
        error: (d) => {
          streamErrMsg = d.message || '流式出题异常'
        },
        all_done: (d) => {
          if (Array.isArray(d.data)) {
            casesList.value = d.data
            genGeneratedCases.value = d.data.length
          }
          genPercent.value = 100
          generateStatusMsg.value = d.message || `已就绪 ${casesList.value.length} 题`
          genProgressText.value = `出题完成：共 ${casesList.value.length} 题`
          showToast(generateStatusMsg.value, 'success')
          activeStep.value = 2
        }
      }
    )
    if (streamErrMsg) {
      throw new Error(streamErrMsg)
    }
  } catch (err) {
    generateStatusMsg.value = `出题异常: ${err.message}`
    showToast(`出题失败: ${err.message}`, 'error')
  } finally {
    generatingCases.value = false
  }
}

function addEmptyCase() {
  const nextId = casesList.value.length + 1
  casesList.value.push({
    id: nextId,
    category: '综合业务推理',
    query: '',
    ground_truth: '',
    source_doc: '',
    review_status: '通过'
  })
}

async function saveCases() {
  try {
    const res = await requestJson(evalUrl('/api/benchmark/save-cases'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cases: casesList.value })
    })
    showToast(res.message || '题库保存成功', 'success')
  } catch (err) {
    showToast(`保存失败: ${err.message}`, 'error')
  }
}

async function runFullPipeline() {
  if (runningPipeline.value) return
  runningPipeline.value = true
  quotaAbortNotice.value = ''
  activeStep.value = 3
  answeredCases.value = []
  answerLogs.value = []
  judgeLogs.value = []
  answerPercent.value = 0
  judgePercent.value = 0
  answerProgressText.value = '正在启动双流水线作答池...'
  judgeProgressText.value = '裁判并发池待命中...'

  try {
    await streamSSEPost(
      evalUrl('/api/benchmark/run-pipeline-stream'),
      {
        limit: selectedLimit.value,
        model_b: config.answer_model,
        model_c: config.judge_model,
        resume: resumeFlag.value,
        reuse_answers: reuseAnswersFlag.value
      },
      {
        pipeline_meta: (d) => {
          answerProgressText.value = `作答 ${d.model_b} (${d.answer_concurrency}并发)`
          judgeProgressText.value = `裁判 ${d.model_c} (${d.judge_concurrency}并发)`
        },
        answer_done: (d) => {
          answerPercent.value = d.total ? Math.round((d.current / d.total) * 100) : 100
          answerProgressText.value = `作答进度 ${d.current}/${d.total}`
          answeredCases.value.push(d.case)
          const ok = d.case.success !== false
          answerLogs.value.unshift({
            ok,
            text: `#${d.current} ${d.resumed ? '♻️ 恢复' : (ok ? '✅ 完成' : '⚠️ 异常')} (${d.case.latency || 0}s) · ${d.case.query}`
          })
        },
        judge_done: (d) => {
          judgePercent.value = d.total ? Math.round((d.current / d.total) * 100) : 100
          judgeProgressText.value = `裁判进度 ${d.current}/${d.total}`
          const s = d.case.scores || {}
          const ok = d.case.is_valid !== false
          judgeLogs.value.unshift({
            ok,
            text: ok
              ? `#${d.current} ${d.case.is_passed ? '✅' : '❌'} 三元组 ${(Number(s.rag_triad_score || 0) * 100).toFixed(1)}分 · ${d.case.query}`
              : `#${d.current} ⚠️ 流程故障 (${d.case.error_type || 'unknown'})`
          })
        },
        quota_paused: (d) => {
          quotaAbortNotice.value = d.reason || '模型额度已耗尽，已完成题目已保存至 Checkpoint，充值或换模型后勾选“断点续跑”可继续。'
          showToast(quotaAbortNotice.value, 'warn', 6000)
        },
        all_done: (d) => {
          latestReport.value = d.summary || d
          loadCheckpointStatus()
          if (!latestReport.value.aborted_by_quota) {
            answerPercent.value = 100
            judgePercent.value = 100
            showToast('双流水线全量评测完成！', 'success')
            activeStep.value = 5
          }
        }
      }
    )
  } catch (err) {
    showToast(`流水线运行异常: ${err.message}`, 'error')
  } finally {
    runningPipeline.value = false
  }
}

async function runAnswersOnly() {
  runningAnswers.value = true
  quotaAbortNotice.value = ''
  answeredCases.value = []
  answerLogs.value = []
  answerPercent.value = 0
  try {
    await streamSSEPost(
      evalUrl('/api/benchmark/run-answers-stream'),
      { limit: selectedLimit.value, model_b: config.answer_model, resume: resumeFlag.value },
      {
        case_done: (d) => {
          answerPercent.value = d.total ? Math.round((d.current / d.total) * 100) : 100
          answerProgressText.value = `作答进度 ${d.current}/${d.total}`
          answeredCases.value.push(d.case)
          answerLogs.value.unshift({
            ok: d.case.success !== false,
            text: `#${d.current} 作答就绪 (${d.case.latency || 0}s) · ${d.case.query}`
          })
        },
        quota_paused: (d) => {
          quotaAbortNotice.value = d.reason || '作答额度耗尽，已安全保留进度。'
          showToast(quotaAbortNotice.value, 'warn', 6000)
        },
        all_done: (d) => {
          answeredCases.value = d.data || answeredCases.value
          loadCheckpointStatus()
          showToast('作答阶段完成', 'success')
        }
      }
    )
  } catch (err) {
    showToast(`作答异常: ${err.message}`, 'error')
  } finally {
    runningAnswers.value = false
  }
}

async function runJudgeOnly() {
  if (!answeredCases.value.length) {
    showToast('请先完成作答或加载最新作答记录', 'warn')
    return
  }
  runningJudge.value = true
  quotaAbortNotice.value = ''
  judgeLogs.value = []
  judgePercent.value = 0
  try {
    await streamSSEPost(
      evalUrl('/api/benchmark/run-judge-stream'),
      { answered_cases: answeredCases.value, model_c: config.judge_model, resume: resumeFlag.value },
      {
        case_done: (d) => {
          judgePercent.value = d.total ? Math.round((d.current / d.total) * 100) : 100
          judgeProgressText.value = `裁判进度 ${d.current}/${d.total}`
          const s = d.case.scores || {}
          judgeLogs.value.unshift({
            ok: d.case.is_valid !== false,
            text: `#${d.current} 三元组 ${(Number(s.rag_triad_score || 0) * 100).toFixed(1)}分 · ${d.case.query}`
          })
        },
        quota_paused: (d) => {
          quotaAbortNotice.value = d.reason || '裁判额度耗尽，已安全保留进度。'
          showToast(quotaAbortNotice.value, 'warn', 6000)
        },
        all_done: (d) => {
          latestReport.value = d.summary || d
          loadCheckpointStatus()
          if (!latestReport.value.aborted_by_quota) {
            showToast('裁判打分完成', 'success')
            activeStep.value = 5
          }
        }
      }
    )
  } catch (err) {
    showToast(`裁判异常: ${err.message}`, 'error')
  } finally {
    runningJudge.value = false
  }
}

async function exportExcelReport() {
  if (latestReport.value?.case_results?.length) {
    try {
      await fetch(evalUrl('/api/benchmark/sync-and-export'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ summary: latestReport.value })
      })
    } catch {
      // 兜底直接下载磁盘文件
    }
  }
  try {
    const resp = await fetch(evalUrl(`/api/benchmark/download-report?t=${Date.now()}`))
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`)
    }
    const blob = await resp.blob()
    const blobUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = blobUrl
    a.download = 'Agentic_RAG_基准评测集_带量化指标.xlsx'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(blobUrl), 5000)
    showToast('已导出 21 列 Excel 报表', 'success')
  } catch (err) {
    showToast(`导出报表失败: ${err.message}`, 'error')
  }
}

onMounted(() => {
  loadInitialData()
})
</script>
