<template>
  <div class="h-full overflow-y-auto lg:overflow-hidden bg-canvas">
    <div class="lg:h-full max-w-7xl mx-auto px-4 sm:px-6 py-4 flex flex-col gap-4">
      <!-- 顶部 5 列单行均衡指标卡 -->
      <section class="shrink-0 grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3.5">
        <div class="bg-surface border border-line rounded-2xl p-3.5 shadow-card">
          <div class="flex items-center justify-between text-xs text-muted">
            <span>已入库文档</span>
            <FileText class="w-4 h-4 text-brand" />
          </div>
          <div class="mt-1.5 text-2xl font-semibold text-gray-900 font-mono">{{ stats.total_documents }}</div>
          <div class="mt-1 text-[11px] text-muted truncate">支持 Markdown / PDF / Word 等</div>
        </div>

        <div class="bg-surface border border-line rounded-2xl p-3.5 shadow-card">
          <div class="flex items-center justify-between text-xs text-muted">
            <span>向量切片总量</span>
            <Layers class="w-4 h-4 text-emerald-600" />
          </div>
          <div class="mt-1.5 text-2xl font-semibold text-emerald-700 font-mono">{{ stats.total_chunks }}</div>
          <div class="mt-1 text-[11px] text-muted truncate">Ollama BGE-M3 · 1024 维</div>
        </div>

        <div class="bg-surface border border-line rounded-2xl p-3.5 shadow-card">
          <div class="flex items-center justify-between text-xs text-muted">
            <span>双层漏斗阈值</span>
            <Filter class="w-4 h-4 text-amber-600" />
          </div>
          <div class="mt-2 text-base font-semibold text-gray-900 font-mono whitespace-nowrap">
            ≤550 <span class="text-gray-300">|</span> ≥0.40
          </div>
          <div class="mt-1 text-[11px] text-muted truncate">gte-rerank-v2 语义精排</div>
        </div>

        <div class="bg-surface border border-line rounded-2xl p-3.5 shadow-card">
          <div class="flex items-center justify-between text-xs text-muted">
            <span>全局会话记忆</span>
            <MessageSquare class="w-4 h-4 text-sky-600" />
          </div>
          <div class="mt-2 text-base font-semibold text-gray-900 font-mono whitespace-nowrap">
            {{ stats.total_sessions }} 会话 / {{ stats.total_messages }} 消息
          </div>
          <div class="mt-1 text-[11px] text-muted truncate">SQLite 统一持久化中枢</div>
        </div>

        <div class="bg-surface border border-line rounded-2xl p-3.5 shadow-card col-span-2 md:col-span-1">
          <div class="flex items-center justify-between text-xs text-muted">
            <span>MCP 服务协议</span>
            <Plug class="w-4 h-4 text-indigo-500" />
          </div>
          <div class="mt-2 inline-flex items-center gap-1.5 text-sm font-semibold text-gray-900">
            <span class="w-2 h-2 rounded-full bg-emerald-500"></span>
            <span>SSE + REST 就绪</span>
          </div>
          <div class="mt-1 text-[11px] text-muted truncate">Port 8001 · 自动降级容灾</div>
        </div>
      </section>

      <!-- 主体双栏：左上传 + 右自适应锁高滚动文档表 -->
      <div class="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-12 gap-4">
        <!-- 左侧上传卡片 -->
        <section class="lg:col-span-4 self-start lg:max-h-full lg:overflow-y-auto bg-surface border border-line rounded-2xl p-5 shadow-card space-y-4">
          <div>
            <h2 class="text-sm font-semibold text-gray-900 flex items-center gap-2">
              <UploadCloud class="w-4 h-4 text-brand" />
              <span>上传新业务文档</span>
            </h2>
            <p class="mt-1 text-xs text-muted">
              支持批量上传，自动完成规则降噪、分块切分与向量入库。
            </p>
          </div>

          <label
            @dragover.prevent="isDragging = true"
            @dragleave.prevent="isDragging = false"
            @drop.prevent="handleDrop"
            class="block border-2 border-dashed rounded-2xl p-6 text-center cursor-pointer transition-all"
            :class="isDragging
              ? 'border-brand bg-brand-soft/60'
              : 'border-line bg-canvas hover:border-brand/50 hover:bg-subtle/60'"
          >
            <input
              type="file"
              multiple
              :disabled="isUploading"
              accept=".pptx,.docx,.pdf,.txt,.md,.json,.csv"
              class="hidden"
              @change="handleFileInput"
            />
            <UploadCloud class="w-9 h-9 text-brand mx-auto mb-2.5" />
            <div class="text-xs font-semibold text-gray-900">
              {{ isUploading ? '正在批量处理文档，请稍候...' : '点击或拖拽文档到此处' }}
            </div>
            <div class="text-[11px] text-muted mt-1">支持 PDF / Word / Markdown / PPTX / TXT / JSON / CSV</div>
          </label>

          <div v-if="uploadMessage" class="px-3.5 py-2.5 rounded-xl bg-brand-soft border border-indigo-200 text-xs text-indigo-900 space-y-2 leading-relaxed">
            <div class="flex items-center justify-between gap-2">
              <span class="truncate">{{ uploadMessage }}</span>
              <span v-if="uploadProgress.total > 0" class="font-mono text-[11px] shrink-0">
                {{ uploadProgress.current }}/{{ uploadProgress.total }}
              </span>
            </div>
            <div v-if="uploadProgress.total > 0" class="h-1.5 rounded-full bg-indigo-100 overflow-hidden">
              <div
                class="h-full bg-brand transition-all duration-300"
                :style="{ width: Math.round((uploadProgress.current / uploadProgress.total) * 100) + '%' }"
              ></div>
            </div>
          </div>

          <!-- 上传结果与重复文档醒目反馈卡片 -->
          <div
            v-if="uploadFeedback"
            class="px-3.5 py-3 rounded-xl border text-xs space-y-1.5"
            :class="{
              'bg-amber-50 border-amber-200 text-amber-900': uploadFeedback.type === 'warn',
              'bg-emerald-50 border-emerald-200 text-emerald-900': uploadFeedback.type === 'success',
              'bg-rose-50 border-rose-200 text-rose-900': uploadFeedback.type === 'error',
              'bg-brand-soft border-indigo-200 text-indigo-900': uploadFeedback.type === 'info'
            }"
          >
            <div class="flex items-center justify-between gap-2 font-semibold">
              <span>{{ uploadFeedback.title }}</span>
              <button
                @click="uploadFeedback = null"
                class="opacity-70 hover:opacity-100"
                title="关闭提示"
              >
                <X class="w-3.5 h-3.5" />
              </button>
            </div>
            <ul v-if="uploadFeedback.items && uploadFeedback.items.length" class="max-h-36 overflow-y-auto pr-1 space-y-1 text-[11px] leading-relaxed">
              <li v-for="(line, idx) in uploadFeedback.items" :key="idx">• {{ line }}</li>
            </ul>
          </div>

          <div class="pt-2 border-t border-line flex items-center justify-between text-xs text-muted">
            <span>中台接口调试</span>
            <a
              :href="dataUrl('/docs')"
              target="_blank"
              class="inline-flex items-center gap-1 text-brand hover:underline font-medium whitespace-nowrap"
            >
              <span>Swagger API</span>
              <ExternalLink class="w-3 h-3" />
            </a>
          </div>
        </section>

        <!-- 右侧文档资产表格（自适应填满一屏剩余高度 + 内部滚动 + 搜索 + 默认时间倒序） -->
        <section class="lg:col-span-8 bg-surface border border-line rounded-2xl shadow-card overflow-hidden flex flex-col lg:h-full min-h-[360px]">
          <div class="shrink-0 px-5 py-3.5 border-b border-line flex flex-wrap items-center justify-between gap-3">
            <div class="flex items-center gap-2">
              <Database class="w-4 h-4 text-brand" />
              <h2 class="text-sm font-semibold text-gray-900">知识库资产清单</h2>
              <span class="px-2 py-0.5 rounded-full bg-subtle border border-line text-[11px] font-mono text-muted">
                {{ filteredDocs.length }} / {{ documents.length }}
              </span>
            </div>

            <div class="flex items-center gap-2">
              <div class="relative">
                <Search class="w-3.5 h-3.5 text-muted absolute left-2.5 top-1/2 -translate-y-1/2" />
                <input
                  v-model="searchKeyword"
                  type="text"
                  placeholder="搜索文档名..."
                  class="w-44 sm:w-56 pl-8 pr-3 py-1.5 rounded-lg bg-canvas border border-line text-xs text-ink placeholder:text-muted focus:outline-none focus:border-brand/60 focus:bg-surface"
                />
              </div>
              <button
                @click="refreshAll"
                class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-surface border border-line hover:bg-subtle text-xs font-medium text-ink transition-colors whitespace-nowrap"
              >
                <RefreshCw class="w-3.5 h-3.5" :class="loadingDocs ? 'animate-spin' : ''" />
                <span>刷新</span>
              </button>
            </div>
          </div>

          <div class="flex-1 min-h-0 overflow-y-auto overflow-x-auto">
            <table class="w-full text-left border-collapse text-xs">
              <thead class="sticky top-0 z-10 bg-[#F9FAFB] border-b border-line text-muted">
                <tr>
                  <th class="py-2.5 px-4 font-medium">文档名称（按最新上传排序）</th>
                  <th class="py-2.5 px-3 font-medium whitespace-nowrap">切片数</th>
                  <th class="py-2.5 px-3 font-medium whitespace-nowrap">大小</th>
                  <th class="py-2.5 px-3 font-medium whitespace-nowrap">入库时间</th>
                  <th class="py-2.5 px-4 font-medium text-right whitespace-nowrap">操作</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-line/80">
                <tr
                  v-for="doc in filteredDocs"
                  :key="doc.doc_name || doc.name"
                  class="hover:bg-canvas transition-colors"
                >
                  <td class="py-3 px-4 font-medium text-gray-900">
                    <div class="flex items-center gap-2 min-w-[200px]">
                      <FileText class="w-4 h-4 text-brand/80 shrink-0" />
                      <span class="break-all">{{ doc.doc_name || doc.name }}</span>
                    </div>
                  </td>
                  <td class="py-3 px-3 whitespace-nowrap">
                    <span class="inline-flex items-center px-2 py-0.5 rounded-md bg-emerald-50 text-emerald-700 border border-emerald-200 font-mono text-[11px] font-medium">
                      {{ doc.chunk_count ?? doc.total_chunks ?? 0 }} 切片
                    </span>
                  </td>
                  <td class="py-3 px-3 text-muted font-mono whitespace-nowrap">
                    {{ doc.file_size_formatted || '--' }}
                  </td>
                  <td class="py-3 px-3 text-muted font-mono whitespace-nowrap">
                    {{ doc.upload_time || doc.created_at || '--' }}
                  </td>
                  <td class="py-3 px-4 text-right whitespace-nowrap">
                    <div class="inline-flex items-center gap-1.5">
                      <button
                        @click="openChunksModal(doc.doc_name || doc.name)"
                        class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-surface border border-line hover:bg-subtle text-xs text-ink transition-colors whitespace-nowrap"
                      >
                        <Eye class="w-3.5 h-3.5 text-brand" />
                        <span>穿透查看</span>
                      </button>
                      <button
                        @click="removeDocument(doc.doc_name || doc.name)"
                        class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-surface border border-line hover:bg-rose-50 hover:border-rose-200 hover:text-rose-700 text-xs text-muted transition-colors whitespace-nowrap"
                      >
                        <Trash2 class="w-3.5 h-3.5" />
                        <span>删除</span>
                      </button>
                    </div>
                  </td>
                </tr>

                <tr v-if="!loadingDocs && filteredDocs.length === 0">
                  <td colspan="5" class="py-12 text-center text-muted">
                    暂无匹配的文档资产
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>

    <!-- 切片穿透查看模态框 -->
    <div
      v-if="chunkModal.open"
      @click.self="chunkModal.open = false"
      class="fixed inset-0 z-50 bg-gray-900/35 backdrop-blur-xs flex items-center justify-center p-4"
    >
      <div class="bg-surface border border-line rounded-2xl shadow-float w-full max-w-3xl max-h-[85vh] flex flex-col overflow-hidden">
        <div class="px-5 py-3.5 border-b border-line flex items-center justify-between gap-3">
          <div class="min-w-0">
            <h3 class="text-sm font-semibold text-gray-900 truncate">
              切片穿透详情 · {{ chunkModal.docName }}
            </h3>
            <p class="text-[11px] text-muted">共 {{ chunkModal.chunks.length }} 个语义切片</p>
          </div>
          <div class="flex items-center gap-2">
            <input
              v-model="chunkModal.filter"
              type="text"
              placeholder="过滤切片关键字..."
              class="w-40 px-2.5 py-1 rounded-lg bg-canvas border border-line text-xs focus:outline-none focus:border-brand/60"
            />
            <button
              @click="chunkModal.open = false"
              class="p-1.5 rounded-lg hover:bg-subtle text-muted hover:text-ink"
            >
              <X class="w-4 h-4" />
            </button>
          </div>
        </div>

        <div class="flex-1 overflow-y-auto p-4 space-y-3 bg-canvas">
          <div v-if="chunkModal.loading" class="py-10 text-center text-xs text-muted">
            正在读取切片向量数据...
          </div>
          <div
            v-for="(chunk, idx) in filteredModalChunks"
            :key="idx"
            class="bg-surface border border-line rounded-xl p-3.5 space-y-2"
          >
            <div class="flex items-center justify-between text-[11px] text-muted">
              <span class="px-2 py-0.5 rounded bg-brand-soft text-brand font-mono font-medium">
                切片 #{{ (chunk.chunk_index ?? idx) + 1 }}
              </span>
              <span class="font-mono">{{ (chunk.text || chunk.content || '').length }} 字符</span>
            </div>
            <pre class="text-xs text-gray-800 whitespace-pre-wrap leading-relaxed font-sans">{{ chunk.text || chunk.content || '' }}</pre>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref, computed, onMounted } from 'vue'
import {
  FileText, Layers, Filter, MessageSquare, Plug, UploadCloud,
  ExternalLink, Database, Search, RefreshCw, Eye, Trash2, X
} from 'lucide-vue-next'
import { dataUrl, requestJson } from '../api/client'
import { useToast } from '../composables/useToast'

const { showToast } = useToast()

const stats = reactive({
  total_documents: 0,
  total_chunks: 0,
  total_sessions: 0,
  total_messages: 0
})

const documents = ref([])
const loadingDocs = ref(false)
const searchKeyword = ref('')
const isDragging = ref(false)
const isUploading = ref(false)
const uploadProgress = reactive({ current: 0, total: 0 })
const uploadMessage = ref('')
const uploadFeedback = ref(null)

const chunkModal = reactive({
  open: false,
  loading: false,
  docName: '',
  chunks: [],
  filter: ''
})

const filteredDocs = computed(() => {
  const kw = searchKeyword.value.trim().toLowerCase()
  if (!kw) return documents.value
  return documents.value.filter((d) =>
    String(d.doc_name || d.name || '').toLowerCase().includes(kw)
  )
})

const filteredModalChunks = computed(() => {
  const kw = chunkModal.filter.trim().toLowerCase()
  if (!kw) return chunkModal.chunks
  return chunkModal.chunks.filter((c) =>
    String(c.text || c.content || '').toLowerCase().includes(kw)
  )
})

async function loadHealth() {
  try {
    const res = await requestJson(dataUrl('/api/v1/data/health'))
    const d = res.data || {}
    stats.total_documents = d.total_documents || 0
    stats.total_chunks = d.total_chunks || 0
    stats.total_sessions = d.total_sessions || 0
    stats.total_messages = d.total_messages || 0
  } catch (err) {
    console.warn('loadHealth failed:', err)
  }
}

async function loadDocuments() {
  loadingDocs.value = true
  try {
    const res = await requestJson(dataUrl('/api/v1/data/documents'))
    const docs = (res.data?.documents || []).slice()
    docs.sort((a, b) => {
      const ta = String(a.upload_time || a.created_at || '')
      const tb = String(b.upload_time || b.created_at || '')
      if (ta !== tb) return tb.localeCompare(ta)
      return String(a.doc_name || a.name || '').localeCompare(String(b.doc_name || b.name || ''))
    })
    documents.value = docs
    stats.total_documents = docs.length
  } catch (err) {
    showToast(`加载文档清单失败: ${err.message}`, 'error')
  } finally {
    loadingDocs.value = false
  }
}

async function refreshAll() {
  await Promise.all([loadHealth(), loadDocuments()])
}

function handleFileInput(e) {
  const fileList = Array.from(e.target.files || [])
  e.target.value = ''
  if (fileList.length > 0) {
    uploadFiles(fileList)
  }
}

function handleDrop(e) {
  isDragging.value = false
  const fileList = Array.from(e.dataTransfer?.files || [])
  if (fileList.length > 0) {
    uploadFiles(fileList)
  }
}

async function uploadFiles(files) {
  const fileList = Array.from(files || [])
  if (fileList.length === 0) return
  if (isUploading.value) {
    showToast('当前已有文档正在上传入库，请稍候完成再继续', 'warn')
    return
  }

  isUploading.value = true
  uploadFeedback.value = null
  uploadProgress.current = 0
  uploadProgress.total = fileList.length

  const existingNames = new Set(documents.value.map((d) => String(d.doc_name || d.name || '')))
  const addedMessages = []
  const updatedMessages = []
  const dupMessages = []
  const errorMessages = []

  try {
    for (let i = 0; i < fileList.length; i++) {
      const file = fileList[i]
      uploadProgress.current = i + 1
      uploadMessage.value = `正在上传并向量化解析 (${i + 1}/${fileList.length}): ${file.name}`
      const formData = new FormData()
      formData.append('file', file)
      formData.append('clean_mode', 'rule')
      try {
        const resp = await fetch(dataUrl('/api/v1/data/upload'), {
          method: 'POST',
          body: formData
        })
        const text = await resp.text()
        let data = {}
        try {
          data = text ? JSON.parse(text) : {}
        } catch {
          throw new Error(text.slice(0, 120) || `HTTP ${resp.status}`)
        }
        if (!resp.ok) {
          throw new Error(data.detail || data.message || `HTTP ${resp.status}`)
        }

        const payload = data.data || {}
        if (payload.is_duplicate) {
          const dupMsg = payload.message || `文档《${file.name}》内容与知识库已有文档重复，已跳过`
          dupMessages.push(dupMsg)
          showToast(dupMsg, 'warn', 5000)
        } else if (existingNames.has(file.name)) {
          const updMsg = `${file.name}（已覆盖更新，${payload.total_chunks ?? 0} 切片）`
          updatedMessages.push(updMsg)
        } else {
          existingNames.add(file.name)
          addedMessages.push(`${file.name}（新增入库，${payload.total_chunks ?? 0} 切片）`)
        }
      } catch (err) {
        const errMsg = `${file.name} 上传失败: ${err.message}`
        errorMessages.push(errMsg)
        showToast(errMsg, 'error', 5000)
      }
    }
  } finally {
    isUploading.value = false
    uploadProgress.current = 0
    uploadProgress.total = 0
    uploadMessage.value = ''
  }

  const allItems = [...dupMessages, ...updatedMessages, ...addedMessages, ...errorMessages]

  if (errorMessages.length > 0 && addedMessages.length === 0 && updatedMessages.length === 0 && dupMessages.length === 0) {
    uploadFeedback.value = { type: 'error', title: `上传失败 (${errorMessages.length} 篇)`, items: allItems }
  } else if (dupMessages.length > 0 && addedMessages.length === 0 && updatedMessages.length === 0 && errorMessages.length === 0) {
    uploadFeedback.value = { type: 'warn', title: `检测到重复文档，已自动跳过 (${dupMessages.length} 篇)`, items: allItems }
  } else if (dupMessages.length > 0 || errorMessages.length > 0) {
    uploadFeedback.value = {
      type: 'warn',
      title: `处理完成：新增 ${addedMessages.length} / 更新 ${updatedMessages.length} / 重复跳过 ${dupMessages.length}`,
      items: allItems
    }
  } else {
    const okTotal = addedMessages.length + updatedMessages.length
    if (okTotal > 0) {
      showToast(`处理完成：新增 ${addedMessages.length} 篇，更新 ${updatedMessages.length} 篇`, 'success')
    }
    uploadFeedback.value = {
      type: 'success',
      title: `成功入库 ${okTotal} 篇文档`,
      items: allItems
    }
  }

  await refreshAll()
}

async function openChunksModal(docName) {
  chunkModal.open = true
  chunkModal.loading = true
  chunkModal.docName = docName
  chunkModal.chunks = []
  chunkModal.filter = ''
  try {
    const res = await requestJson(dataUrl(`/api/v1/data/documents/${encodeURIComponent(docName)}/chunks`))
    chunkModal.chunks = res.data?.chunks || []
  } catch (err) {
    showToast(`读取切片失败: ${err.message}`, 'error')
  } finally {
    chunkModal.loading = false
  }
}

async function removeDocument(docName) {
  if (!window.confirm(`确定从知识库与 ChromaDB 中删除《${docName}》吗？`)) return
  try {
    await requestJson(dataUrl(`/api/v1/data/documents/${encodeURIComponent(docName)}`), {
      method: 'DELETE'
    })
    showToast(`已删除《${docName}》`, 'success')
    await refreshAll()
  } catch (err) {
    showToast(`删除失败: ${err.message}`, 'error')
  }
}

onMounted(() => {
  refreshAll()
})
</script>
