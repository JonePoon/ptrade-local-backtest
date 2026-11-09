<script setup lang="ts">
// 列表首页：统计条 + run 卡片列表（点击卡片进入详情页，不展开）
import { ref } from 'vue'
import { useRunsStore } from '@/stores/runs'
import RunCard from '@/components/RunCard.vue'
import ExportDialog from '@/components/ExportDialog.vue'
import { product } from '@/lib/product'

const store = useRunsStore()
const exportName = ref<string | null>(null)
</script>

<template>
  <main class="mx-auto max-w-6xl px-3 py-4 sm:px-4">
    <section class="mb-4 rounded-xl border border-teal-600/20 bg-teal-600/5 p-4 sm:p-5" aria-label="项目介绍">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0">
          <p class="text-[10px] font-semibold tracking-widest text-teal-700 dark:text-teal-400">LOCAL BACKTEST · v{{ product.version }}</p>
          <h2 class="mt-1 text-lg font-semibold tracking-tight sm:text-xl">本地回测与复盘</h2>
          <p class="mt-2 max-w-2xl text-xs leading-relaxed text-muted-foreground sm:text-sm">查看本地回测记录、指标、交易与日志，按本次回测快照导出 PTrade 单文件。</p>
        </div>
        <a :href="product.repositoryUrl" target="_blank" rel="noopener noreferrer" class="max-w-full break-all rounded-md border bg-background px-3 py-2 text-xs font-medium hover:bg-muted">{{ product.firstDeveloper }} / {{ product.repository }}</a>
      </div>
      <div class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        <span>独立项目 · 第一开发者 {{ product.firstDeveloper }}</span>
        <span>参考 / 复用 ptrade-sim 基础实现 · 保留来源许可</span>
      </div>
    </section>
    <div class="mb-3 flex flex-wrap items-baseline gap-x-2 gap-y-1 text-xs text-muted-foreground">
      <span>
        共 {{ store.runs.length }} 个 run ｜ 完成 {{ store.doneCount }} ｜
        运行中 {{ store.runningCount }} ｜ 中断 {{ store.interruptedCount }}
        <span v-if="store.error" class="ml-2 text-destructive">
          （连接失败：{{ store.error }}）
        </span>
      </span>
      <span v-if="store.lastUpdatedAt" class="ml-auto hidden sm:inline">
        上次更新 {{ new Date(store.lastUpdatedAt).toLocaleTimeString() }}
      </span>
    </div>

    <div v-if="!store.runs.length && !store.error" class="py-20 text-center text-sm text-muted-foreground">
      暂无回测记录，启动回测后此处自动出现。
    </div>

    <div class="flex flex-col gap-2.5">
      <RunCard v-for="r in store.runs" :key="r.name" :run="r" @source="store.sourceName = $event" @export="exportName = $event" />
    </div>
    <ExportDialog :name="exportName" @close="exportName = null" />
  </main>
</template>