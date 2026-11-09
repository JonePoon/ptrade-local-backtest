<script setup lang="ts">
// 独立转换界面：只操作本次 run 快照，不与原始源码查看混合。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useClipboard } from '@vueuse/core'
import { api, ApiError } from '@/api/client'
import type { StrategyExportDiagnostic, StrategyExportFormat, StrategyExportReport, StrategyExportResp } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Check, Copy, Download, FileJson, Loader2, RefreshCw } from '@lucide/vue'

const props = defineProps<{ name: string | null }>()
const emit = defineEmits<{ (e: 'close'): void }>()
const result = ref<StrategyExportResp | null>(null)
const failureReport = ref<StrategyExportReport | null>(null)
const loading = ref(false)
const error = ref('')
const actionMessage = ref('')
const copied = ref(false)
const refreshVersion = ref(0)
const downloading = ref<StrategyExportFormat | null>(null)
let downloadController: AbortController | null = null
const { copy } = useClipboard({ legacy: true })

const report = computed(() => result.value?.report ?? failureReport.value)
const diagnostics = computed(() => report.value?.diagnostics ?? [])
const errorCount = computed(() => diagnostics.value.filter(d => d.severity === 'error').length)
const warningCount = computed(() => diagnostics.value.filter(d => d.severity === 'warning').length)
const ready = computed(() => result.value?.report.ready === true && Boolean(result.value.source))
const paramsText = computed(() => JSON.stringify(report.value?.params ?? {}, null, 2))
const digest = computed(() => report.value?.export_sha256 || report.value?.source_sha256)

watch(
  () => [props.name, refreshVersion.value] as const,
  async ([name], _previous, onCleanup) => {
    result.value = null
    failureReport.value = null
    error.value = ''
    actionMessage.value = ''
    copied.value = false
    downloadController?.abort()
    downloadController = null
    downloading.value = null
    loading.value = Boolean(name)
    if (!name) return
    const controller = new AbortController()
    onCleanup(() => { controller.abort(); downloadController?.abort() })
    try {
      const response = await api.exportStrategy(name, controller.signal)
      // 切 run 或关闭后不能让旧请求覆盖新对话框。
      if (!controller.signal.aborted) result.value = response
    } catch (e) {
      if (controller.signal.aborted) return
      error.value = '转换加载失败：' + (e instanceof Error ? e.message : String(e))
      failureReport.value = e instanceof ApiError ? e.report ?? null : null
    } finally {
      if (!controller.signal.aborted) loading.value = false
    }
  },
  { immediate: true },
)
onBeforeUnmount(() => downloadController?.abort())

const severityLabel: Record<StrategyExportDiagnostic['severity'], string> = {
  error: '阻塞', warning: '提示', info: '信息',
}
function location(diagnostic: StrategyExportDiagnostic): string {
  const line = diagnostic.line != null ? ':' + diagnostic.line : ''
  const column = diagnostic.column != null ? ':' + diagnostic.column : ''
  return (diagnostic.path ?? '') + line + column
}

async function copySource() {
  if (!ready.value || !result.value) return
  const name = props.name
  error.value = ''
  try {
    await copy(result.value.source)
    if (props.name === name) { copied.value = true; actionMessage.value = '代码已复制' }
  } catch (e) {
    if (props.name === name) error.value = '复制失败：' + (e instanceof Error ? e.message : String(e))
  }
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // 给浏览器下载动作留出读取 Blob 的时间，再释放内存。
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

async function download(format: StrategyExportFormat) {
  const name = props.name
  if (!name || !result.value || downloading.value || (format === 'python' && !ready.value)) return
  const controller = new AbortController()
  downloadController = controller
  downloading.value = format
  error.value = ''
  actionMessage.value = ''
  try {
    const file = await api.downloadExport(name, format, controller.signal)
    if (controller.signal.aborted || props.name !== name) return
    saveBlob(file.blob, file.filename)
    actionMessage.value = '已提交下载：' + file.filename
  } catch (e) {
    if (controller.signal.aborted) return
    error.value = '下载失败：' + (e instanceof Error ? e.message : String(e))
    if (e instanceof ApiError && e.report) {
      failureReport.value = e.report
      if (result.value) result.value = { ...result.value, source: '', report: e.report }
    }
  } finally {
    if (downloadController === controller) {
      downloading.value = null
      downloadController = null
    }
  }
}
</script>

<template>
  <Dialog :open="props.name !== null" @update:open="(value: boolean) => { if (!value) emit('close') }">
    <DialogContent class="max-h-[calc(100dvh-2rem)] w-[calc(100%-1.5rem)] max-w-5xl gap-3 overflow-y-auto p-4 sm:max-w-5xl sm:p-6">
      <DialogHeader class="min-w-0 pr-7 text-left">
        <DialogTitle class="text-base sm:text-lg">PTrade 策略转换</DialogTitle>
        <DialogDescription class="break-all text-xs sm:text-sm">
          从本次回测的策略与参数快照生成单文件 · {{ props.name }}
        </DialogDescription>
      </DialogHeader>

      <div class="flex flex-wrap items-center gap-2">
        <Badge v-if="report" :variant="report.ready ? 'default' : 'destructive'">
          {{ report.ready ? '可下载' : '存在阻塞项' }}
        </Badge>
        <span v-if="report?.target_python" class="text-xs text-muted-foreground">Python {{ report.target_python }}</span>
        <span v-if="report?.validation === 'static_only'" class="text-xs text-muted-foreground">静态转换检查</span>
        <span v-if="result" class="min-w-0 flex-1 truncate font-mono text-xs" :title="result.filename">{{ result.filename }}</span>
        <Button variant="outline" size="xs" class="ml-auto shrink-0 gap-1" :disabled="loading || downloading !== null" @click="refreshVersion++">
          <RefreshCw class="size-3.5" /> 重新加载
        </Button>
      </div>

      <div v-if="loading" class="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground" role="status" aria-live="polite">
        <Loader2 class="size-4 animate-spin" /> 正在转换快照…
      </div>
      <div v-if="error" class="break-words rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive" role="alert">
        {{ error }}
      </div>

      <template v-if="report && !loading">
        <div class="grid min-w-0 gap-3 sm:grid-cols-2">
          <section class="min-w-0 rounded-md border p-3" aria-label="转换诊断">
            <h3 class="mb-2 text-xs font-semibold">转换诊断 · {{ errorCount }} 个阻塞 / {{ warningCount }} 条提示</h3>
            <ol v-if="diagnostics.length" class="max-h-48 space-y-2 overflow-auto text-xs">
              <li v-for="(diagnostic, index) in diagnostics" :key="index" class="break-words border-l-2 pl-2" :class="diagnostic.severity === 'error' ? 'border-destructive text-destructive' : 'border-border text-foreground'">
                <div class="font-medium">{{ severityLabel[diagnostic.severity] }} · {{ diagnostic.code }}</div>
                <div v-if="location(diagnostic)" class="break-all font-mono text-[11px] text-muted-foreground">{{ location(diagnostic) }}</div>
                <div class="mt-0.5">{{ diagnostic.message }}</div>
              </li>
            </ol>
            <p v-else class="text-xs text-muted-foreground">没有转换诊断。</p>
          </section>
          <section class="min-w-0 rounded-md border p-3" aria-label="嵌入参数">
            <h3 class="mb-2 text-xs font-semibold">嵌入参数</h3>
            <pre class="max-h-48 overflow-auto whitespace-pre-wrap break-all font-mono text-xs leading-relaxed">{{ paramsText }}</pre>
          </section>
        </div>

        <section class="min-w-0" aria-label="生产代码预览">
          <div class="mb-2 flex flex-wrap items-center justify-between gap-1 text-xs">
            <h3 class="font-semibold">单文件代码预览</h3>
            <span v-if="digest" class="font-mono text-[10px] text-muted-foreground" :title="digest">SHA-256 {{ digest.slice(0, 16) }}…</span>
          </div>
          <pre class="max-h-[35dvh] overflow-auto whitespace-pre-wrap break-all rounded-md border bg-slate-950 p-3 font-mono text-xs leading-relaxed text-slate-100 sm:whitespace-pre sm:break-normal"><code>{{ result?.source || '（未生成可下载代码）' }}</code></pre>
        </section>
        <p v-if="!report.ready" class="text-xs text-destructive">转换未就绪，生产代码下载和复制已关闭；转换报告可保留诊断与参数。</p>
      </template>

      <div class="flex flex-wrap items-center gap-2 border-t pt-3">
        <Button variant="outline" size="sm" class="gap-1.5" :disabled="!ready || loading || downloading !== null" @click="copySource">
          <Check v-if="copied" class="size-3.5" /><Copy v-else class="size-3.5" /> {{ copied ? '已复制' : '复制代码' }}
        </Button>
        <Button variant="outline" size="sm" class="gap-1.5" :disabled="!result || loading || downloading !== null" @click="download('manifest')">
          <Loader2 v-if="downloading === 'manifest'" class="size-3.5 animate-spin" /><FileJson v-else class="size-3.5" /> 下载报告
        </Button>
        <Button size="sm" class="gap-1.5" :disabled="!ready || loading || downloading !== null" @click="download('python')">
          <Loader2 v-if="downloading === 'python'" class="size-3.5 animate-spin" /><Download v-else class="size-3.5" /> 下载 .py
        </Button>
        <span v-if="actionMessage" class="w-full break-all text-xs text-muted-foreground sm:w-auto" role="status" aria-live="polite">{{ actionMessage }}</span>
      </div>
    </DialogContent>
  </Dialog>
</template>
