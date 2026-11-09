<script setup lang="ts">
// 主视图外壳：header + 全局源码 Dialog；路由出口渲染首页/详情页
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, RouterView, useRouter } from 'vue-router'
import { Moon, RefreshCw, Sun } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useRunsStore } from '@/stores/runs'
import { applyTheme, getTheme, type Theme } from '@/lib/theme'
import SourceDialog from '@/components/SourceDialog.vue'
import { product } from '@/lib/product'

const store = useRunsStore()
const router = useRouter()

const theme = ref<Theme>(getTheme())
const isDark = computed(() => theme.value === 'dark')
function toggleTheme() {
  theme.value = isDark.value ? 'light' : 'dark'
  applyTheme(theme.value)
}

onMounted(() => store.start())
onBeforeUnmount(() => store.stop())

// 列表、详情与数据页切换后回到顶部；内部路由名称保持兼容。
router.afterEach(() => {
  window.scrollTo({ top: 0, behavior: 'auto' })
})
</script>

<template>
  <div class="min-h-screen bg-muted/40">
    <header class="sticky top-0 z-30 border-b bg-background/95 backdrop-blur">
      <div class="mx-auto max-w-7xl px-3 py-2.5 sm:px-5 sm:py-3">
        <div class="flex items-center gap-2 sm:gap-3">
          <RouterLink
            to="/"
            class="flex min-w-0 flex-1 items-center gap-2.5 rounded-sm transition-colors hover:text-foreground/80 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            :aria-label="product.name + '首页'"
            data-testid="product-brand"
          >
            <span class="flex size-8 shrink-0 items-center justify-center rounded-lg bg-teal-700 text-xl font-semibold text-white sm:size-9" aria-hidden="true">p</span>
            <div class="min-w-0">
              <h1 class="text-sm font-semibold leading-snug tracking-tight sm:text-lg">{{ product.name }}</h1>
              <p class="mt-0.5 text-[10px] text-muted-foreground sm:text-xs">v{{ product.version }} · 第一开发者 {{ product.firstDeveloper }}</p>
            </div>
          </RouterLink>
          <div class="flex shrink-0 items-center gap-1.5">
            <Button
              variant="ghost"
              size="icon-sm"
              :title="isDark ? '切换到浅色' : '切换到深色'"
              :aria-label="isDark ? '切换到浅色' : '切换到深色'"
              @click="toggleTheme"
            >
              <Sun v-if="isDark" class="size-4" />
              <Moon v-else class="size-4" />
            </Button>
            <Button variant="outline" size="sm" class="gap-1.5 px-2 sm:px-3" @click="store.refresh()">
              <RefreshCw class="size-3.5" />刷新
            </Button>
          </div>
        </div>
        <div class="mt-2 flex flex-wrap items-center gap-2">
          <nav class="flex items-center gap-1 text-xs" aria-label="看板导航">
            <RouterLink to="/" class="rounded-md px-2.5 py-1.5 hover:bg-muted" exact-active-class="bg-muted font-semibold">回测记录</RouterLink>
            <RouterLink to="/data" class="rounded-md px-2.5 py-1.5 hover:bg-muted" active-class="bg-muted font-semibold">数据管理</RouterLink>
          </nav>
          <Badge v-if="store.runningCount" variant="destructive" class="gap-1 px-1.5 py-0 text-[10px] sm:px-2.5 sm:text-xs">
            <span class="size-1.5 animate-pulse rounded-full bg-current" />
            {{ store.runningCount }} 个回测进行中
          </Badge>
          <span v-if="store.lastUpdatedAt" class="ml-auto hidden text-xs text-muted-foreground sm:inline">
            每 {{ store.intervalMs / 1000 }}s 自动更新 · 上次 {{ new Date(store.lastUpdatedAt).toLocaleTimeString() }}
          </span>
        </div>
      </div>
    </header>

    <RouterView v-slot="{ Component }">
      <component :is="Component" />
    </RouterView>

    <SourceDialog :name="store.sourceName" @close="store.sourceName = null" />
  </div>
</template>