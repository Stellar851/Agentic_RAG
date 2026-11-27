<template>
  <div class="h-full flex bg-canvas overflow-hidden">
    <!-- 左侧历史会话栏 (Notion 风格可折叠) -->
    <aside
      v-if="sidebarOpen"
      class="w-[264px] shrink-0 bg-surface border-r border-line flex flex-col"
    >
      <div class="p-3 border-b border-line space-y-2.5">
        <button
          @click="createNewChat"
          class="w-full inline-flex items-center justify-center gap-2 px-3 py-2 rounded-xl bg-brand text-white text-xs font-medium hover:bg-brand-hover transition-colors shadow-sm whitespace-nowrap"
        >
          <Plus class="w-3.5 h-3.5" />
          <span>新建对话</span>
        </button>

        <div class="relative">
          <Search class="w-3.5 h-3.5 text-muted absolute left-2.5 top-1/2 -translate-y-1/2" />
          <input
            v-model="sessionQuery"
            type="text"
            placeholder="搜索历史对话..."
            class="w-full pl-8 pr-3 py-1.5 rounded-lg bg-subtle border border-line text-xs text-ink placeholder:text-muted focus:outline-none focus:border-brand/60 focus:bg-surface transition-colors"
          />
        </div>
      </div>

      <div class="px-3.5 pt-2.5 pb-1.5 flex items-center justify-between text-[11px] font-medium text-muted">
        <span>历史会话</span>
        <span class="px-1.5 py-0.5 rounded-md bg-subtle text-gray-600 font-mono text-[10px]">
          {{ filteredSessions.length }}
        </span>
      </div>

      <div class="flex-1 overflow-y-auto px-2 pb-3 space-y-1">
        <div
          v-for="s in filteredSessions"
          :key="s.session_id"
          @click="selectSession(s.session_id)"
          class="group flex items-center justify-between gap-2 px-2.5 py-2 rounded-lg cursor-pointer transition-colors text-xs"
          :class="currentSessionId === s.session_id
            ? 'bg-brand-soft text-brand font-medium'
            : 'text-ink hover:bg-subtle'"
        >
          <div class="flex items-center gap-2 min-w-0">
            <MessageCircle class="w-3.5 h-3.5 shrink-0 opacity-70" />
            <span class="truncate">{{ s.title || '新对话' }}</span>
          </div>
          <button
            @click.stop="removeSession(s.session_id)"
            class="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-rose-50 text-muted hover:text-rose-600 transition-all shrink-0"
            title="删除会话"
          >
            <Trash2 class="w-3.5 h-3.5" />
          </button>
        </div>

        <div v-if="filteredSessions.length === 0" class="text-center py-8 text-xs text-muted">
          暂无匹配会话
        </div>
      </div>
    </aside>

    <!-- 右侧主阅读与交互区 -->
    <main class="flex-1 min-w-0 flex flex-col relative">
      <!-- 顶部轻量控制条 -->
      <div class="h-11 shrink-0 px-4 border-b border-line/70 bg-canvas/80 flex items-center justify-between">
        <div class="flex items-center gap-2 min-w-0">
          <button
            @click="sidebarOpen = !sidebarOpen"
            class="p-1.5 rounded-lg text-muted hover:text-ink hover:bg-subtle transition-colors"
            :title="sidebarOpen ? '收起历史栏' : '展开历史栏'"
          >
            <PanelLeftClose v-if="sidebarOpen" class="w-4 h-4" />
            <PanelLeftOpen v-else class="w-4 h-4" />
          </button>
          <span class="text-xs font-medium text-ink truncate">
            {{ activeSessionTitle }}
          </span>
        </div>

        <div class="text-[11px] text-muted hidden sm:flex items-center gap-1.5 whitespace-nowrap">
          <Layers class="w-3.5 h-3.5 text-brand" />
          <span>多跳 ReAct 推理 · 双层精排漏斗</span>
        </div>
      </div>

      <!-- 消息滚动区 -->
      <div
        ref="messagesBoxRef"
        @click="handleMessageBoxClick"
        class="flex-1 overflow-y-auto px-4 sm:px-6 py-6"
      >
        <div class="max-w-3xl mx-auto">
          <!-- 极简 Claude 风格欢迎页 -->
          <div v-if="messages.length === 0" class="py-10 sm:py-14 space-y-8">
            <div class="space-y-3">
              <div class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-brand-soft text-brand text-xs font-medium">
                <Sparkles class="w-3.5 h-3.5" />
                <span>Agentic RAG 多跳智能问答</span>
              </div>
              <h1 class="text-2xl sm:text-3xl font-semibold tracking-tight text-gray-900">
                有什么我可以帮您从知识库查证的？
              </h1>
              <p class="text-sm text-muted leading-relaxed">
                支持跨文档检索、连续章节精读、多轮上下文追问，以及严格基于证据的忠实作答。
              </p>
            </div>

            <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <button
                v-for="(sample, idx) in sampleQueries"
                :key="idx"
                @click="applySample(sample.query)"
                class="text-left p-4 rounded-2xl bg-surface border border-line hover:border-brand/40 hover:shadow-card transition-all group flex flex-col justify-between gap-3"
              >
                <div class="space-y-1.5">
                  <div class="text-xs font-semibold text-gray-900 group-hover:text-brand transition-colors">
                    {{ sample.title }}
                  </div>
                  <div class="text-xs text-muted leading-relaxed line-clamp-3">
                    {{ sample.query }}
                  </div>
                </div>
                <div class="inline-flex items-center gap-1 text-[11px] font-medium text-brand">
                  <span>立即提问</span>
                  <ArrowRight class="w-3 h-3 transition-transform group-hover:translate-x-0.5" />
                </div>
              </button>
            </div>
          </div>

          <!-- 对话流 -->
          <div v-else class="space-y-6 pb-4">
            <div v-for="(msg, mIdx) in messages" :key="mIdx">
              <!-- 用户提问 -->
              <div v-if="msg.role === 'user'" class="flex justify-end">
                <div class="max-w-[85%] px-4 py-2.5 rounded-2xl rounded-tr-md bg-subtle border border-line text-sm text-gray-900 leading-relaxed whitespace-pre-wrap">
                  {{ msg.content }}
                </div>
              </div>

              <!-- 助手回复 -->
              <div v-else class="flex items-start gap-3">
                <div class="w-7 h-7 rounded-lg bg-brand-soft border border-indigo-200/80 flex items-center justify-center text-brand shrink-0 mt-0.5">
                  <Bot class="w-4 h-4" />
                </div>

                <div class="flex-1 min-w-0 space-y-2.5">
                  <!-- 可折叠 ReAct 多跳思考轨迹 -->
                  <div
                    v-if="msg.steps && msg.steps.length > 0"
                    class="rounded-xl bg-surface border border-line overflow-hidden"
                  >
                    <button
                      @click="msg.stepsExpanded = !msg.stepsExpanded"
                      class="w-full px-3.5 py-2 flex items-center justify-between text-xs text-muted hover:text-ink hover:bg-gray-50/70 transition-colors"
                    >
                      <span class="inline-flex items-center gap-2 font-medium">
                        <Loader2 v-if="msg.streaming && !msg.content" class="w-3.5 h-3.5 animate-spin text-brand" />
                        <GitBranch v-else class="w-3.5 h-3.5 text-brand" />
                        <span>多跳推理与工具轨迹 ({{ msg.steps.length }} 步)</span>
                      </span>
                      <ChevronDown
                        class="w-3.5 h-3.5 transition-transform"
                        :class="msg.stepsExpanded ? 'rotate-180' : ''"
                      />
                    </button>

                    <div v-if="msg.stepsExpanded" class="border-t border-line px-3.5 py-2.5 space-y-2 bg-canvas/70">
                      <div
                        v-for="(step, sIdx) in msg.steps"
                        :key="sIdx"
                        class="text-xs space-y-1 pb-2 border-b border-line/60 last:border-b-0 last:pb-0"
                      >
                        <div class="flex items-center gap-2 flex-wrap">
                          <span class="px-1.5 py-0.5 rounded bg-brand-soft text-brand font-mono text-[11px] font-medium">
                            #{{ sIdx + 1 }} {{ step.tool || 'tool' }}
                          </span>
                          <span v-if="step.input" class="text-muted font-mono text-[11px] truncate max-w-md">
                            {{ step.input }}
                          </span>
                        </div>
                        <div
                          v-if="step.observation_preview || step.observation"
                          class="text-[11px] text-gray-600 bg-surface border border-line/80 rounded-lg px-2.5 py-1.5 leading-relaxed line-clamp-3"
                        >
                          {{ step.observation_preview || step.observation }}
                        </div>
                      </div>
                    </div>
                  </div>

                  <!-- 正文回复 -->
                  <div class="rounded-2xl bg-surface border border-line/80 px-4 py-3.5 shadow-card">
                    <div
                      v-if="msg.content"
                      class="prose-claude"
                      v-html="renderMarkdown(msg.content)"
                    ></div>
                    <div v-else-if="msg.streaming" class="inline-flex items-center gap-2 text-xs text-muted py-1">
                      <Loader2 class="w-3.5 h-3.5 animate-spin text-brand" />
                      <span>正在检索知识库并推理作答...</span>
                    </div>

                    <!-- 引用来源文档标签 -->
                    <div
                      v-if="msg.referenced_docs && msg.referenced_docs.length > 0"
                      class="mt-3 pt-2.5 border-t border-line flex items-center gap-1.5 flex-wrap"
                    >
                      <span class="text-[11px] text-muted mr-1 inline-flex items-center gap-1">
                        <FileText class="w-3 h-3" />
                        引用依据:
                      </span>
                      <span
                        v-for="(doc, dIdx) in msg.referenced_docs"
                        :key="dIdx"
                        class="inline-flex items-center px-2 py-0.5 rounded-md bg-subtle border border-line text-[11px] text-gray-700"
                      >
                        {{ doc }}
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 底部悬浮 Claude 风格输入区 -->
      <div class="shrink-0 px-4 sm:px-6 pb-4 pt-2 bg-gradient-to-t from-canvas via-canvas to-transparent">
        <div class="max-w-3xl mx-auto">
          <div class="bg-surface border border-line rounded-2xl shadow-float p-3 focus-within:border-brand/50 transition-colors">
            <textarea
              ref="inputRef"
              v-model="inputText"
              rows="1"
              @input="autoResizeInput"
              @keydown="handleKeydown"
              placeholder="输入问题，支持多跳推理与多轮追问（Enter 发送，Shift+Enter 换行）"
              class="w-full resize-none bg-transparent text-sm text-ink placeholder:text-muted focus:outline-none max-h-40 leading-relaxed px-1"
            ></textarea>

            <div class="mt-2.5 pt-2 border-t border-line/70 flex items-center justify-between gap-2">
              <!-- 模型热切换选择器 -->
              <div class="relative flex items-center gap-2">
                <button
                  @click="modelMenuOpen = !modelMenuOpen"
                  type="button"
                  class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-subtle hover:bg-gray-200/70 border border-line text-xs font-medium text-ink transition-colors whitespace-nowrap"
                >
                  <Cpu class="w-3.5 h-3.5 text-brand" />
                  <span>{{ activeModelDisplay }}</span>
                  <ChevronDown class="w-3 h-3 text-muted" />
                </button>
                <span class="hidden sm:inline text-[11px] text-muted whitespace-nowrap">
                  Enter 发送 · Shift+Enter 换行
                </span>

                <div
                  v-if="modelMenuOpen"
                  class="absolute bottom-full left-0 mb-2 w-72 rounded-xl bg-surface border border-line shadow-float p-1.5 z-40"
                >
                  <div class="px-2.5 py-1.5 text-[11px] font-medium text-muted border-b border-line/70 mb-1">
                    切换问答推理模型
                  </div>
                  <button
                    v-for="m in modelsList"
                    :key="m.model_name"
                    @click="selectModel(m.model_name)"
                    class="w-full text-left px-2.5 py-2 rounded-lg text-xs transition-colors flex items-center justify-between gap-2"
                    :class="selectedModel === m.model_name ? 'bg-brand-soft text-brand font-medium' : 'hover:bg-subtle text-ink'"
                  >
                    <div class="min-w-0">
                      <div class="truncate">{{ m.name || m.model_name }}</div>
                      <div class="text-[11px] text-muted truncate">{{ m.desc }}</div>
                    </div>
                    <Check v-if="selectedModel === m.model_name" class="w-3.5 h-3.5 shrink-0" />
                  </button>
                </div>
              </div>

              <!-- 发送 / 停止按钮 -->
              <div class="flex items-center gap-2">
                <button
                  v-if="isGenerating"
                  @click="stopGeneration"
                  class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-rose-50 hover:bg-rose-100 text-rose-700 border border-rose-200 text-xs font-medium transition-colors whitespace-nowrap"
                >
                  <Square class="w-3 h-3 fill-current" />
                  <span>停止生成</span>
                </button>
                <button
                  v-else
                  @click="sendMessage"
                  :disabled="!inputText.trim()"
                  class="inline-flex items-center justify-center w-8 h-8 rounded-xl bg-brand text-white hover:bg-brand-hover disabled:opacity-40 disabled:cursor-not-allowed transition-all shadow-sm"
                  title="发送提问"
                >
                  <ArrowUp class="w-4 h-4" />
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted } from 'vue'
import { marked } from 'marked'
import {
  Plus, Search, MessageCircle, Trash2, PanelLeftClose, PanelLeftOpen,
  Layers, Sparkles, ArrowRight, Bot, Loader2, GitBranch, ChevronDown,
  FileText, Cpu, Check, Square, ArrowUp
} from 'lucide-vue-next'
import { agentUrl, requestJson } from '../api/client'
import { streamSSEPost } from '../api/sse'
import { useToast } from '../composables/useToast'

const { showToast } = useToast()

const escapeHtml = (str) =>
  String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')

const renderer = new marked.Renderer()
renderer.code = function (token) {
  const codeText = typeof token === 'object' ? (token.text || '') : String(token || '')
  const lang = typeof token === 'object' ? (token.lang || '') : ''
  return `<pre><button type="button" class="code-copy-btn">复制</button><code class="language-${escapeHtml(lang)}">${escapeHtml(codeText)}</code></pre>`
}
marked.setOptions({ renderer, breaks: true, gfm: true })

const renderMarkdown = (text) => marked.parse(String(text || ''))

const sidebarOpen = ref(true)
const sessions = ref([])
const sessionQuery = ref('')
const currentSessionId = ref('')
const messages = ref([])
const inputText = ref('')
const isGenerating = ref(false)
const modelsList = ref([
  {
    model_name: 'qwen3.6-flash-2026-04-16',
    short_name: 'Qwen 3.6 Flash',
    name: '通义千问 3.6 Flash',
    desc: '极速高配额备选 · 阿里百炼平台提供'
  }
])
const selectedModel = ref('qwen3.6-flash-2026-04-16')
const modelMenuOpen = ref(false)

const messagesBoxRef = ref(null)
const inputRef = ref(null)
let abortCtrl = null

const sampleQueries = [
  {
    title: '多文档方案对比',
    query: '对比知识库中不同文档提及的技术方案与架构差异'
  },
  {
    title: '跨文档接口追溯',
    query: '订单系统调用聚合支付网关 pay_v2 接口的超时时间和重试规则是什么？'
  },
  {
    title: '全局业务规则梳理',
    query: '总结梳理当前知识库涵盖的核心制度规范与关键阈值要求'
  }
]

const filteredSessions = computed(() => {
  const q = sessionQuery.value.trim().toLowerCase()
  if (!q) return sessions.value
  return sessions.value.filter((s) => (s.title || '').toLowerCase().includes(q))
})

const activeSessionTitle = computed(() => {
  const hit = sessions.value.find((s) => s.session_id === currentSessionId.value)
  return hit ? (hit.title || '新对话') : '新对话'
})

const activeModelDisplay = computed(() => {
  const hit = modelsList.value.find((m) => m.model_name === selectedModel.value)
  return hit ? (hit.short_name || hit.name || hit.model_name) : selectedModel.value
})

function scrollToBottom() {
  nextTick(() => {
    const el = messagesBoxRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function autoResizeInput() {
  const el = inputRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 160) + 'px'
}

function handleKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendMessage()
  }
}

async function handleMessageBoxClick(e) {
  const btn = e.target.closest('.code-copy-btn')
  if (!btn) return
  const pre = btn.closest('pre')
  const codeEl = pre?.querySelector('code')
  const text = codeEl?.innerText || ''
  try {
    await navigator.clipboard.writeText(text)
    btn.textContent = '已复制'
    setTimeout(() => {
      btn.textContent = '复制'
    }, 1500)
  } catch {
    showToast('复制失败，请手动选中复制', 'warn')
  }
}

async function loadModels() {
  try {
    const res = await requestJson(agentUrl('/api/models'))
    if (res.code === 200 && res.data?.models?.length) {
      modelsList.value = res.data.models
      if (res.data.default_model) {
        selectedModel.value = res.data.default_model
      }
    }
  } catch (err) {
    console.warn('loadModels failed:', err)
  }
}

async function loadSessions() {
  try {
    const res = await requestJson(agentUrl('/api/sessions'))
    if (res.code === 200 && Array.isArray(res.data)) {
      sessions.value = res.data
    }
  } catch (err) {
    console.warn('loadSessions failed:', err)
  }
}

async function selectSession(sessionId) {
  currentSessionId.value = sessionId
  try {
    const res = await requestJson(agentUrl(`/api/sessions/${sessionId}`))
    if (res.code === 200 && res.data) {
      const msgs = res.data.messages || []
      messages.value = msgs.map((m) => ({
        role: m.role,
        content: m.content || '',
        steps: m.steps || m.steps_trace || [],
        referenced_docs: m.referenced_docs || [],
        stepsExpanded: false,
        streaming: false
      }))
      scrollToBottom()
    }
  } catch (err) {
    showToast(`加载会话失败: ${err.message}`, 'error')
  }
}

function createNewChat() {
  if (isGenerating.value) stopGeneration()
  currentSessionId.value = ''
  messages.value = []
  nextTick(() => inputRef.value?.focus())
}

async function removeSession(sessionId) {
  try {
    await requestJson(agentUrl(`/api/sessions/${sessionId}`), { method: 'DELETE' })
    sessions.value = sessions.value.filter((s) => s.session_id !== sessionId)
    if (currentSessionId.value === sessionId) {
      createNewChat()
    }
    showToast('会话已删除', 'success')
  } catch (err) {
    showToast(`删除失败: ${err.message}`, 'error')
  }
}

function selectModel(modelName) {
  selectedModel.value = modelName
  modelMenuOpen.value = false
}

function applySample(query) {
  inputText.value = query
  sendMessage()
}

function stopGeneration() {
  if (abortCtrl) {
    abortCtrl.abort()
    abortCtrl = null
  }
  isGenerating.value = false
  const last = messages.value[messages.value.length - 1]
  if (last && last.role === 'assistant') {
    last.streaming = false
  }
}

async function sendMessage() {
  const query = inputText.value.trim()
  if (!query || isGenerating.value) return

  inputText.value = ''
  nextTick(autoResizeInput)

  messages.value.push({ role: 'user', content: query })
  const assistantMsg = {
    role: 'assistant',
    content: '',
    steps: [],
    referenced_docs: [],
    stepsExpanded: true,
    streaming: true
  }
  messages.value.push(assistantMsg)
  const reactiveAssistant = messages.value[messages.value.length - 1]
  scrollToBottom()

  isGenerating.value = true
  abortCtrl = new AbortController()

  try {
    await streamSSEPost(
      agentUrl('/api/chat'),
      {
        query,
        session_id: currentSessionId.value || null,
        model_name: selectedModel.value,
        stream: true,
        persist: true
      },
      {
        session_init: (d) => {
          if (d.session_id) currentSessionId.value = d.session_id
        },
        step_start: (d) => {
          reactiveAssistant.steps.push({
            tool: d.tool || 'tool',
            input: d.input || '',
            observation_preview: ''
          })
          scrollToBottom()
        },
        step_end: (d) => {
          const lastStep = reactiveAssistant.steps[reactiveAssistant.steps.length - 1]
          if (lastStep) {
            lastStep.observation_preview = d.observation_preview || ''
          }
          scrollToBottom()
        },
        token: (d) => {
          reactiveAssistant.content += d.delta || ''
          scrollToBottom()
        },
        done: (d) => {
          if (d.answer) reactiveAssistant.content = d.answer
          if (Array.isArray(d.steps)) reactiveAssistant.steps = d.steps
          if (Array.isArray(d.referenced_docs)) reactiveAssistant.referenced_docs = d.referenced_docs
          reactiveAssistant.streaming = false
          reactiveAssistant.stepsExpanded = false
          scrollToBottom()
        },
        error: (d) => {
          reactiveAssistant.streaming = false
          const msg = d.message || d.error || '推理出错'
          reactiveAssistant.content = reactiveAssistant.content || `> 异常提示：${msg}`
          showToast(msg, 'error')
        }
      },
      abortCtrl.signal
    )
    await loadSessions()
  } catch (err) {
    if (err.name === 'AbortError') {
      showToast('已手动停止生成', 'info')
    } else {
      reactiveAssistant.content = reactiveAssistant.content || `请求异常：${err.message}`
      showToast(`对话异常: ${err.message}`, 'error')
    }
  } finally {
    reactiveAssistant.streaming = false
    isGenerating.value = false
    abortCtrl = null
  }
}

onMounted(() => {
  loadModels()
  loadSessions()
})
</script>
