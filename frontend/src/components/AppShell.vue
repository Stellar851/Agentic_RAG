<template>
  <div class="h-screen flex flex-col bg-canvas text-ink overflow-hidden">
    <!-- 统一极简顶部导航栏 (Claude / Notion 浅色风格) -->
    <header class="h-14 shrink-0 bg-surface/90 backdrop-blur-md border-b border-line px-4 sm:px-6 flex items-center justify-between gap-4 z-30">
      <!-- 左侧品牌 -->
      <div class="flex items-center gap-2.5 shrink-0">
        <div class="w-8 h-8 rounded-lg bg-gradient-to-br from-brand to-indigo-400 flex items-center justify-center text-white shadow-sm">
          <Sparkles class="w-4 h-4" />
        </div>
        <div class="flex items-center gap-2">
          <span class="font-semibold text-sm tracking-tight text-gray-900 whitespace-nowrap">Agentic RAG</span>
          <span class="hidden sm:inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium bg-subtle text-muted border border-line whitespace-nowrap">
            企业多跳知识工作台
          </span>
        </div>
      </div>

      <!-- 中央三端统一分段导航 -->
      <nav class="flex items-center bg-subtle p-1 rounded-xl border border-line/80 gap-1">
        <RouterLink
          v-for="tab in navTabs"
          :key="tab.to"
          :to="tab.to"
          class="inline-flex items-center gap-1.5 px-3 sm:px-3.5 py-1.5 rounded-lg text-xs font-medium transition-all whitespace-nowrap"
          :class="route.path.startsWith(tab.to)
            ? 'bg-surface text-gray-900 shadow-sm border border-line/70'
            : 'text-muted hover:text-ink hover:bg-white/50'"
        >
          <component :is="tab.icon" class="w-3.5 h-3.5 shrink-0" :class="route.path.startsWith(tab.to) ? 'text-brand' : ''" />
          <span>{{ tab.label }}</span>
        </RouterLink>
      </nav>

      <!-- 右侧微服务健康状态点 -->
      <div class="flex items-center gap-2 shrink-0">
        <button
          @click="checkAllServices"
          class="inline-flex items-center gap-2 px-2.5 py-1 rounded-lg bg-surface border border-line text-[11px] text-muted hover:text-ink hover:border-gray-300 transition-colors whitespace-nowrap"
          title="点击刷新各微服务健康状态"
        >
          <span class="inline-flex items-center gap-1" title="智能体服务 (Port 8000) + Ollama 向量引擎">
            <span class="w-1.5 h-1.5 rounded-full" :class="services.agent ? 'bg-emerald-500' : 'bg-rose-400'"></span>
            <span class="hidden md:inline">问答</span>
          </span>
          <span class="text-gray-300">·</span>
          <span class="inline-flex items-center gap-1" title="数据中台 & ChromaDB (Port 8001)">
            <span class="w-1.5 h-1.5 rounded-full" :class="services.data ? 'bg-emerald-500' : 'bg-rose-400'"></span>
            <span class="hidden md:inline">知识库</span>
          </span>
          <span class="text-gray-300">·</span>
          <span class="inline-flex items-center gap-1" title="Ragas 评测中台 (Port 8002)">
            <span class="w-1.5 h-1.5 rounded-full" :class="services.eval ? 'bg-emerald-500' : 'bg-rose-400'"></span>
            <span class="hidden md:inline">评测</span>
          </span>
          <span class="hidden lg:inline text-[10px] text-gray-400 border-l border-line pl-2 ml-0.5">
            {{ ollamaReady ? 'BGE-M3 就绪' : '向量检测中' }}
          </span>
        </button>
      </div>
    </header>

    <!-- 主工作区内容 -->
    <div class="flex-1 min-h-0 overflow-hidden">
      <slot />
    </div>
  </div>
</template>

<script setup>
import { reactive, ref, onMounted, onUnmounted } from 'vue'
import { useRoute, RouterLink } from 'vue-router'
import { Sparkles, MessageSquare, Database, BarChart3 } from 'lucide-vue-next'
import { agentUrl, dataUrl, evalUrl } from '../api/client'

const route = useRoute()

const navTabs = [
  { to: '/chat', label: '智能问答', icon: MessageSquare },
  { to: '/knowledge', label: '知识中台', icon: Database },
  { to: '/eval', label: '评测中枢', icon: BarChart3 }
]

const services = reactive({
  agent: false,
  data: false,
  eval: false
})
const ollamaReady = ref(false)

let timer = null

async function probe(url) {
  try {
    const res = await fetch(url, { method: 'GET' })
    if (!res.ok) return null
    return await res.json()
  } catch {
    return null
  }
}

async function checkAllServices() {
  const [agentRes, dataRes, evalRes] = await Promise.all([
    probe(agentUrl('/api/status')),
    probe(dataUrl('/api/v1/data/health')),
    probe(evalUrl('/api/eval/health'))
  ])

  services.agent = Boolean(agentRes && agentRes.code === 200)
  services.data = Boolean(dataRes && dataRes.code === 200)
  services.eval = Boolean(evalRes && evalRes.code === 200)
  ollamaReady.value = Boolean(agentRes?.data?.ollama?.connected)
}

onMounted(() => {
  checkAllServices()
  timer = setInterval(checkAllServices, 30000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>
