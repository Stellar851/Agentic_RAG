<template>
  <div class="fixed bottom-5 right-5 z-50 flex flex-col gap-2 max-w-sm pointer-events-none">
    <div
      v-for="item in toasts"
      :key="item.id"
      class="pointer-events-auto flex items-start gap-2.5 px-3.5 py-2.5 rounded-xl bg-surface border shadow-float text-xs font-medium transition-all"
      :class="{
        'border-emerald-200 text-emerald-900': item.type === 'success',
        'border-amber-200 text-amber-900': item.type === 'warn',
        'border-rose-200 text-rose-900': item.type === 'error',
        'border-line text-ink': item.type === 'info'
      }"
    >
      <CheckCircle2 v-if="item.type === 'success'" class="w-4 h-4 text-emerald-600 shrink-0 mt-0.5" />
      <AlertTriangle v-else-if="item.type === 'warn'" class="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
      <XCircle v-else-if="item.type === 'error'" class="w-4 h-4 text-rose-600 shrink-0 mt-0.5" />
      <Info v-else class="w-4 h-4 text-brand shrink-0 mt-0.5" />
      <span class="leading-relaxed flex-1">{{ item.message }}</span>
      <button
        class="text-muted hover:text-ink ml-1 shrink-0"
        @click="removeToast(item.id)"
      >
        <X class="w-3.5 h-3.5" />
      </button>
    </div>
  </div>
</template>

<script setup>
import { CheckCircle2, AlertTriangle, XCircle, Info, X } from 'lucide-vue-next'
import { useToast } from '../composables/useToast'

const { toasts, removeToast } = useToast()
</script>
