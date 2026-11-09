<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { dataApi } from '@/api/data'
import { binaryStatusLabel, displayLocalPath, hideLocalPaths } from '@/lib/dataDisplay'
import type { DataDataset, DataJob, DataJobStatus, DataOperation, DataOverrides, DataPlan, DataStatus } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { ClipboardList, Database, Loader2, Play, RefreshCw, Square } from '@lucide/vue'

const state = ref<DataStatus | null>(null)
const jobs = ref<DataJob[]>([])
const selectedId = ref<string | null>(null)
const detail = ref<DataJob | null>(null)
const plan = ref<DataPlan | null>(null)
const statusError = ref('')
const actionError = ref('')
const notice = ref('')
const loading = ref(false)
const busy = ref<'plan' | 'start' | 'cancel' | null>(null)
const autoRefresh = ref(true)
const hidePaths = ref(true) // 只影响呈现，默认不暴露本机目录；API/参数保持原值。
const operation = ref<DataOperation>('sync')
const startDate = ref('')
const endDate = ref('')
const symbolsText = ref('')
const datasets = ref<DataDataset[]>([])
let initialized = false
let stopped = false
let timer: ReturnType<typeof setTimeout> | null = null
let pollController: AbortController | null = null
let actionController: AbortController | null = null
let detailController: AbortController | null = null
let requestKey: { signature: string; key: string } | null = null

const options: { id: DataDataset; label: string }[] = [
  { id: 'daily_bars', label: '股票日线' }, { id: 'index_bars', label: '指数日线' },
  { id: 'minute_bars', label: '分钟行情' }, { id: 'minute_bars_5m', label: '5分钟行情' },
  { id: 'corporate_actions', label: '公司行动' }, { id: 'instruments', label: '标的信息' },
  { id: 'trading_calendar', label: '交易日历' }, { id: 'adj_factors', label: '复权因子' },
  { id: 'index_constituents', label: '指数成分' }, { id: 'trading_status', label: '交易状态' },
]
const config = computed(() => state.value?.config ?? state.value?.config_public ?? null)
const active = (job: DataJob | null) => job?.status === 'queued' || job?.status === 'running'
const hasActive = computed(() => jobs.value.some(active))
const selected = computed(() => detail.value ?? jobs.value.find(j => j.id === selectedId.value) ?? null)
const binaries = computed(() => [
  { label: 'Rust 下载器', value: state.value?.binaries?.fetch },
  { label: 'Rust 入库器', value: state.value?.binaries?.store },
])
const labels: Record<DataJobStatus, string> = { queued: '排队中', running: '运行中', succeeded: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消' }
const operationLabels: Record<DataOperation, string> = { sync: '下载并入库', fetch: '仅下载', ingest: '仅入库', verify: '校验数据' }
const percent = computed<number | null>(() => {
  const progress = selected.value?.progress
  const value = typeof progress === 'number' ? progress : progress?.percent
    ?? (progress?.total && progress.current != null ? progress.current / progress.total * 100 : null)
  return value != null && Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null
})
const progressMessage = computed(() => typeof selected.value?.progress === 'object' ? selected.value.progress?.message : '')
const logs = computed(() => Array.isArray(selected.value?.log_tail) ? selected.value.log_tail.join('\n') : selected.value?.log_tail ?? '')
function json(value: unknown) { return JSON.stringify(hidePaths.value ? hideLocalPaths(value ?? {}) : value ?? {}, null, 2) }
function errorText(error: unknown) { return error instanceof Error ? error.message : String(error) }
function badge(status: DataJobStatus) { return status === 'failed' || status === 'partial' ? 'destructive' as const : status === 'succeeded' ? 'default' as const : 'outline' as const }

async function refresh() {
  if (timer) clearTimeout(timer)
  pollController?.abort()
  const controller = new AbortController()
  pollController = controller
  loading.value = true
  try {
    const [status, list] = await Promise.all([dataApi.status(controller.signal), dataApi.jobs(controller.signal)])
    if (controller.signal.aborted) return
    state.value = status
    jobs.value = list.jobs
    statusError.value = ''
    if (!initialized && config.value) {
      startDate.value = config.value.start ?? ''
      endDate.value = config.value.end ?? ''
      symbolsText.value = (config.value.symbols ?? []).join(', ')
      datasets.value = [...(config.value.datasets ?? [])]
      initialized = true
    }
    if (!selectedId.value && list.jobs.length) selectedId.value = list.jobs[0]!.id
    const id = selectedId.value
    if (id) {
      const job = await dataApi.job(id, controller.signal)
      if (!controller.signal.aborted && selectedId.value === id) detail.value = job
    }
  } catch (e) {
    if (!controller.signal.aborted) statusError.value = errorText(e)
  } finally {
    if (pollController === controller && !controller.signal.aborted) {
      loading.value = false
      if (!stopped && autoRefresh.value) timer = setTimeout(refresh, 2000)
    }
  }
}
watch(autoRefresh, enabled => {
  if (timer) clearTimeout(timer)
  if (enabled) refresh()
  else { pollController?.abort(); loading.value = false }
})
onMounted(refresh)
onBeforeUnmount(() => {
  stopped = true
  if (timer) clearTimeout(timer)
  pollController?.abort(); actionController?.abort(); detailController?.abort()
})

function overrides(): DataOverrides {
  if (!datasets.value.length) throw new Error('至少选择一个数据集')
  if (startDate.value && endDate.value && startDate.value > endDate.value) throw new Error('开始日期不得晚于结束日期')
  return {
    ...(startDate.value ? { start: startDate.value } : {}), ...(endDate.value ? { end: endDate.value } : {}),
    ...(symbolsText.value.trim() ? { symbols: symbolsText.value.split(/[\s,;，；]+/).filter(Boolean) } : {}), datasets: [...datasets.value],
  }
}
watch([operation, startDate, endDate, symbolsText, () => datasets.value.join(',')], () => {
  plan.value = null
  if (busy.value === 'plan') { actionController?.abort(); busy.value = null }
})
async function previewPlan() {
  if (busy.value) return
  busy.value = 'plan'; actionError.value = ''; notice.value = ''
  const controller = new AbortController(); actionController = controller
  try { const result = await dataApi.plan(overrides(), controller.signal); if (!controller.signal.aborted) plan.value = result }
  catch (e) { if (!controller.signal.aborted) actionError.value = errorText(e) }
  finally { if (!controller.signal.aborted) busy.value = null }
}
function idempotencyKey() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')
}
async function startJob() {
  if (busy.value || hasActive.value) return
  busy.value = 'start'; actionError.value = ''; notice.value = ''
  const controller = new AbortController(); actionController = controller
  try {
    const values = overrides()
    const signature = JSON.stringify({ operation: operation.value, overrides: values })
    if (requestKey?.signature !== signature) requestKey = { signature, key: idempotencyKey() }
    const job = await dataApi.start(operation.value, values, requestKey.key, controller.signal)
    if (controller.signal.aborted) return
    requestKey = null // 已收到明确结果；后续完成后用户可主动新建任务。
    selectedId.value = job.id; detail.value = job
    jobs.value = [job, ...jobs.value.filter(j => j.id !== job.id)]
    notice.value = '任务已受理：' + job.id
  } catch (e) { if (!controller.signal.aborted) actionError.value = errorText(e) }
  finally { if (!controller.signal.aborted) busy.value = null }
}
async function cancelJob() {
  const id = selected.value?.id
  if (!id || busy.value || !active(selected.value)) return
  busy.value = 'cancel'; actionError.value = ''; notice.value = ''
  const controller = new AbortController(); actionController = controller
  try {
    const job = await dataApi.cancel(id, controller.signal)
    if (controller.signal.aborted) return
    detail.value = job; jobs.value = jobs.value.map(j => j.id === id ? job : j)
    notice.value = '取消请求已提交'
  } catch (e) { if (!controller.signal.aborted) actionError.value = errorText(e) }
  finally { if (!controller.signal.aborted) busy.value = null }
}
async function choose(job: DataJob) {
  selectedId.value = job.id; detail.value = job
  detailController?.abort()
  const controller = new AbortController(); detailController = controller
  try {
    const value = await dataApi.job(job.id, controller.signal)
    if (!controller.signal.aborted && selectedId.value === job.id) detail.value = value
  } catch (e) { if (!controller.signal.aborted) actionError.value = errorText(e) }
}
</script>

<template>
  <main class="mx-auto max-w-7xl space-y-4 px-3 py-4 sm:px-5">
    <div class="flex flex-wrap items-center gap-2">
      <Database class="size-5" /><h2 class="text-lg font-semibold">数据管理</h2>
      <span class="text-xs text-muted-foreground">配置、覆盖与增量任务</span><Badge v-if="config?.provider === 'fixture'" variant="outline">隔离 fixture</Badge>
      <label class="ml-auto flex items-center gap-1.5 text-xs"><input v-model="hidePaths" type="checkbox"> 隐藏本机路径</label>
      <label class="flex items-center gap-1.5 text-xs"><input v-model="autoRefresh" type="checkbox"> 自动刷新</label>
      <Button variant="outline" size="sm" class="gap-1" @click="refresh"><Loader2 v-if="loading" class="size-3.5 animate-spin" /><RefreshCw v-else class="size-3.5" />刷新状态</Button>
    </div>
    <p v-if="config?.provider === 'fixture'" class="rounded-md border border-teal-600/30 bg-teal-600/5 px-3 py-2 text-xs text-muted-foreground" role="note">隔离 fixture · 使用合成数据；留存任务状态与 MOCK 交互不代表真实原生下载或入库结果。</p>
    <p v-if="statusError" class="break-words rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive" role="alert">状态读取失败：{{ statusError }}</p>
    <div class="grid gap-3 lg:grid-cols-3">
      <Card class="min-w-0 p-4">
        <h3 class="mb-2 text-sm font-semibold">公开配置</h3>
        <Badge :variant="config ? 'outline' : 'destructive'">{{ config ? '配置已读取' : '配置不可用' }}</Badge>
        <details v-if="config" class="mt-2"><summary class="cursor-pointer text-xs">查看配置</summary><pre class="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all text-xs">{{ json(config) }}</pre></details>
        <p v-else class="mt-2 text-xs text-muted-foreground">未返回有效配置，启动入口暂不可用。</p>
      </Card>
      <Card class="min-w-0 p-4">
        <h3 class="mb-2 text-sm font-semibold">Rust 工具状态</h3>
        <div v-for="binary in binaries" :key="binary.label" class="mb-2 text-xs last:mb-0">
          <div class="flex items-center justify-between gap-2"><span>{{ binary.label }}</span><Badge variant="outline">{{ binaryStatusLabel(binary.value) }}</Badge></div>
          <div class="mt-1 break-all text-muted-foreground">{{ displayLocalPath(binary.value?.path, hidePaths) }}</div>
          <div class="mt-1">{{ binary.value?.version ? '已返回版本：' + binary.value.version : '运行验证未知' }}</div>
          <div v-if="binary.value?.error" class="mt-1 break-words text-destructive">{{ binary.value.error }}</div>
        </div>
      </Card>
      <Card class="min-w-0 p-4">
        <h3 class="mb-2 text-sm font-semibold">数据库与数据湖</h3>
        <p class="break-all text-xs text-muted-foreground">{{ displayLocalPath(state?.database?.path || config?.db, hidePaths) }}</p>
        <p class="mt-2 text-xs">库：{{ state?.database?.exists === true ? '存在' : state?.database?.exists === false ? '不存在' : '未知' }} · 湖：{{ state?.lake?.exists === true ? '存在' : state?.lake?.exists === false ? '不存在' : '未知' }}</p>
        <p v-if="state?.database?.busy" class="mt-2 text-xs text-muted-foreground">数据库占用中，当前覆盖统计可能不可用。</p>
        <details v-if="state?.lake?.datasets" class="mt-2"><summary class="cursor-pointer text-xs">湖数据集覆盖</summary><pre class="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-all text-xs">{{ json(state.lake.datasets) }}</pre></details>
      </Card>
    </div>
    <Card class="overflow-hidden p-4">
      <h3 class="mb-2 text-sm font-semibold">库表覆盖</h3>
      <div class="overflow-auto"><table v-if="state?.database?.tables?.length" class="w-full whitespace-nowrap text-left text-xs"><thead class="text-muted-foreground"><tr><th class="p-2">表</th><th class="p-2">行数</th><th class="p-2">开始</th><th class="p-2">结束</th></tr></thead><tbody><tr v-for="table in state.database.tables" :key="table.name" class="border-t"><td class="p-2 font-mono">{{ table.name }}</td><td class="p-2">{{ table.rows == null ? '未知' : table.rows.toLocaleString() }}</td><td class="p-2">{{ table.start || '未统计' }}</td><td class="p-2">{{ table.end || '未统计' }}</td></tr></tbody></table><p v-else class="text-xs text-muted-foreground">尚无可用表覆盖统计。</p></div>
    </Card>
    <Card class="p-4">
      <h3 class="mb-3 text-sm font-semibold">增量任务</h3>
      <div class="grid gap-3 sm:grid-cols-3">
        <label class="grid gap-1 text-xs">操作<select v-model="operation" class="h-9 rounded-md border bg-background px-2"><option v-for="(label, value) in operationLabels" :key="value" :value="value">{{ label }}</option></select></label>
        <label class="grid gap-1 text-xs">开始日期<input v-model="startDate" type="date" class="h-9 min-w-0 rounded-md border bg-background px-2"></label>
        <label class="grid gap-1 text-xs">结束日期<input v-model="endDate" type="date" class="h-9 min-w-0 rounded-md border bg-background px-2"></label>
      </div>
      <label class="mt-3 grid gap-1 text-xs">标的代码<textarea v-model="symbolsText" rows="2" placeholder="000001.SZ, 600000.SH" class="rounded-md border bg-background px-2 py-2 font-mono"></textarea><span class="text-muted-foreground">逗号或换行分隔；留空使用配置的标的范围。</span></label>
      <fieldset class="mt-3"><legend class="mb-2 text-xs font-medium">数据集</legend><div class="grid gap-2 sm:grid-cols-4"><label v-for="item in options" :key="item.id" class="flex items-center gap-2 text-xs"><input v-model="datasets" type="checkbox" :value="item.id">{{ item.label }}</label></div></fieldset>
      <div class="mt-4 flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" class="gap-1" :disabled="busy !== null || !config" @click="previewPlan"><Loader2 v-if="busy === 'plan'" class="size-3.5 animate-spin" /><ClipboardList v-else class="size-3.5" />预览计划</Button>
        <Button size="sm" class="gap-1" :disabled="busy !== null || !config || !!statusError || hasActive" @click="startJob"><Loader2 v-if="busy === 'start'" class="size-3.5 animate-spin" /><Play v-else class="size-3.5" />启动任务</Button>
        <span v-if="hasActive" class="text-xs text-muted-foreground">已有数据任务进行中</span>
      </div>
      <p v-if="actionError" class="mt-3 break-words text-sm text-destructive" role="alert">{{ actionError }}</p>
      <p v-if="notice" class="mt-3 break-all text-xs text-muted-foreground" role="status">{{ notice }}</p>
      <details v-if="plan" open class="mt-3 rounded-md border p-3"><summary class="cursor-pointer text-sm">全链路计划预览 · {{ plan.ready === true ? '就绪' : plan.ready === false ? '尚未就绪' : '状态未知' }}</summary><div class="mt-2 text-xs">下载：{{ plan.will_download ? '是' : '否' }} · 写入：{{ plan.will_write ? '是' : '否' }} · 预览不会启动任务，实际执行按所选操作</div><pre class="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all text-xs">{{ json(plan) }}</pre></details>
    </Card>
    <div class="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
      <Card class="min-w-0 p-4"><h3 class="mb-3 text-sm font-semibold">最近增量任务</h3><p v-if="!jobs.length" class="text-xs text-muted-foreground">暂无数据任务。</p><div class="max-h-96 space-y-2 overflow-auto"><button v-for="job in jobs" :key="job.id" class="w-full rounded-md border p-3 text-left text-xs hover:bg-muted/50" :class="selectedId === job.id ? 'border-primary' : ''" @click="choose(job)"><div class="flex flex-wrap items-center justify-between gap-2"><span>{{ operationLabels[job.operation] || job.operation }}</span><Badge :variant="badge(job.status)">{{ labels[job.status] || job.status }}</Badge></div><div class="mt-1 break-all font-mono text-[10px] text-muted-foreground">{{ job.id }}</div><div class="mt-1">{{ job.started_at || '尚未开始' }}</div></button></div></Card>
      <Card class="min-w-0 p-4">
        <div class="mb-3 flex flex-wrap items-center justify-between gap-2"><h3 class="text-sm font-semibold">任务详情</h3><Button variant="outline" size="sm" class="gap-1" :disabled="busy !== null || !active(selected)" @click="cancelJob"><Square class="size-3.5" />取消任务</Button></div>
        <p v-if="!selected" class="text-xs text-muted-foreground">选择任务查看进度、日志与结果。</p>
        <template v-else>
          <div class="flex flex-wrap items-center gap-2"><Badge :variant="badge(selected.status)">{{ labels[selected.status] || selected.status }}</Badge><Badge v-if="selected.report?.synthetic === true" variant="outline">SYNTHETIC 合成状态</Badge><Badge v-if="selected.report?.authority === 'controlled-http-fixture'" variant="outline">MOCK · 未启动原生任务</Badge><span class="text-xs">阶段 {{ selected.phase || '未知' }} · {{ percent == null ? '进度未知' : percent.toFixed(1) + '%' }}</span></div>
          <div class="mt-2 break-all font-mono text-[10px] text-muted-foreground">{{ selected.id }}</div>
          <Progress class="mt-3" :model-value="percent ?? 0" />
          <p class="mt-2 text-xs text-muted-foreground">{{ progressMessage }}</p>
          <p v-if="selected.status === 'partial'" class="mt-3 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs">部分完成（partial），并非完整覆盖；查看覆盖范围与错误记录。</p>
          <pre v-if="selected.errors?.length" class="mt-3 max-h-40 overflow-auto whitespace-pre-wrap break-all text-xs text-destructive">{{ json(selected.errors) }}</pre>
          <details class="mt-3"><summary class="cursor-pointer text-xs">计数与覆盖</summary><pre class="mt-2 max-h-52 overflow-auto whitespace-pre-wrap break-all text-xs">{{ json({ counts: selected.counts, coverage: selected.coverage }) }}</pre></details>
          <h4 class="mb-2 mt-3 text-xs font-semibold">日志</h4><pre class="max-h-60 overflow-auto whitespace-pre-wrap break-all rounded-md bg-slate-950 p-3 text-xs leading-relaxed text-slate-100">{{ logs || '暂无日志' }}</pre>
          <details class="mt-3"><summary class="cursor-pointer text-xs">结果报告</summary><pre class="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all text-xs">{{ selected.report ? json(selected.report) : '暂无结果报告' }}</pre></details>
        </template>
      </Card>
    </div>
  </main>
</template>
